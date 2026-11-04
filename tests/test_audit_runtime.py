"""Tests for the canonical audit runtime module (configs/audit/audit_runtime.py)."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
RUNTIME_PATH = ROOT / "configs" / "audit" / "audit_runtime.py"
EVENT_PATH = ROOT / "configs" / "audit" / "audit_event.py"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ar = _load("audit_runtime", RUNTIME_PATH)
ae = _load("audit_event_for_runtime_tests", EVENT_PATH)


# --- purpose header ----------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "   "])
def test_an_absent_or_blank_header_states_no_purpose(value: str | None) -> None:
    assert ar.parse_purpose_header(value) == (None, None)


def test_a_browser_encoded_purpose_is_decoded() -> None:
    # What URLSearchParams({case_ref: "AZ 12/34", reason: "Prüfung des Hinweises"}).toString() sends.
    value = "case_ref=AZ+12%2F34&reason=Pr%C3%BCfung+des+Hinweises"
    assert ar.parse_purpose_header(value) == ("AZ 12/34", "Prüfung des Hinweises")


def test_either_field_alone_is_accepted() -> None:
    assert ar.parse_purpose_header("case_ref=case-0001") == ("case-0001", None)
    assert ar.parse_purpose_header("reason=routine+check") == (None, "routine check")


def test_blank_fields_count_as_absent_and_values_are_trimmed() -> None:
    assert ar.parse_purpose_header("case_ref=&reason=+++spaced+++") == (None, "spaced")


def test_unknown_keys_are_ignored() -> None:
    assert ar.parse_purpose_header("v=2&case_ref=case-0001&note=x") == ("case-0001", None)


@pytest.mark.parametrize(
    "value",
    [
        "case_ref=a&case_ref=b",  # ambiguous
        "reason=%FF",  # not UTF-8
        "reason=Prüfung",  # raw non-ASCII: must be percent-encoded
        "reason=line%0Abreak",  # control character
        "reason=tab%09inside",
        "reason=next%C2%85line",  # C1 control (NEL)
        "reason=a%E2%80%A8b",  # U+2028 line separator
        "case_ref",  # not key=value
        "case_ref=a&&reason=b",  # empty field
        "&".join(f"k{n}=v" for n in range(17)),  # too many fields
    ],
)
def test_malformed_headers_are_rejected(value: str) -> None:
    with pytest.raises(ar.PurposeError):
        ar.parse_purpose_header(value)


def test_field_length_is_bounded_by_the_library_text_cap() -> None:
    assert ar.MAX_PURPOSE_FIELD == ae.MAX_TEXT
    assert ar.parse_purpose_header("reason=" + "x" * ar.MAX_PURPOSE_FIELD) == (None, "x" * ar.MAX_PURPOSE_FIELD)
    with pytest.raises(ar.PurposeError):
        ar.parse_purpose_header("reason=" + "x" * (ar.MAX_PURPOSE_FIELD + 1))


def test_header_length_is_bounded() -> None:
    value = "reason=x&pad=" + "y" * ar.MAX_PURPOSE_HEADER
    with pytest.raises(ar.PurposeError):
        ar.parse_purpose_header(value)


@pytest.mark.parametrize(
    ("case_ref", "reason"),
    [
        ("AZ 12/34", "Prüfung des Hinweises"),
        ("a+b&c=d", "50% sicher; ¿ok? → ja"),
        ("case-0001", None),
        (None, "only a reason"),
        ("emoji \U0001f600", "~*-._"),
    ],
)
def test_encode_and_parse_round_trip(case_ref: str | None, reason: str | None) -> None:
    encoded = ar.encode_purpose(case_ref, reason)
    assert encoded.isascii()
    assert ar.parse_purpose_header(encoded) == (case_ref, reason)


def test_encoding_nothing_gives_an_empty_value() -> None:
    assert ar.encode_purpose(None, "") == ""
    assert ar.parse_purpose_header(ar.encode_purpose(None, None)) == (None, None)


def test_purpose_from_env_reads_and_validates_the_two_variables() -> None:
    env = {"AUDIT_CASE_REF": " case-0001 ", "AUDIT_REASON": "", "OTHER": "x"}
    assert ar.purpose_from_env(env) == ("case-0001", None)
    assert ar.purpose_from_env({}) == (None, None)
    with pytest.raises(ar.PurposeError):
        ar.purpose_from_env({"AUDIT_REASON": "two\nlines"})


# --- required actions, request ids, chunks ----------------------------------

KNOWN = ae.ACTIONS - {"lifecycle"}


@pytest.mark.parametrize("value", [None, "", " , "])
def test_no_required_actions(value: str | None) -> None:
    assert ar.parse_required_actions(value, known=KNOWN) == frozenset()


def test_required_actions_are_parsed_leniently_but_checked_strictly() -> None:
    assert ar.parse_required_actions(" query, Disclose ,", known=KNOWN) == {"query", "disclose"}
    with pytest.raises(ValueError, match="querry"):
        ar.parse_required_actions("query,querry", known=KNOWN)
    with pytest.raises(ValueError, match="lifecycle"):
        ar.parse_required_actions("lifecycle", known=KNOWN)


@pytest.mark.parametrize(
    "value",
    ["3f8a1c2e-0b4d-4e6f-9a7b-1c2d3e4f5a6b", "abc", "req.1:2_3-4", "a" * 128],
)
def test_sane_request_ids_are_kept(value: str) -> None:
    assert ar.clean_request_id(value) == value


@pytest.mark.parametrize("value", [None, "", "a" * 129, "has space", "-leading", 'quote"', "line\nbreak", "ümlaut"])
def test_other_request_ids_are_dropped(value: str | None) -> None:
    assert ar.clean_request_id(value) is None


def test_id_chunks_split_at_the_library_cap() -> None:
    assert ar.MAX_IDS_PER_EVENT == ae.MAX_IDS
    ids = [f"id-{n}" for n in range(450)]
    chunks = ar.id_chunks(ids)
    assert [len(chunk) for chunk in chunks] == [200, 200, 50]
    assert [item for chunk in chunks for item in chunk] == ids
    assert ar.id_chunks([]) == []
    assert ar.id_chunks(["a", "b", "c"], size=2) == [["a", "b"], ["c"]]
    with pytest.raises(ValueError, match="at least 1"):
        ar.id_chunks(ids, size=0)


# --- container stream ---------------------------------------------------------


def test_the_stream_writes_only_on_flush(tmp_path: Path) -> None:
    target = tmp_path / "out.log"
    target.touch()
    stream = ar.ContainerAuditStream(str(target), str(tmp_path / "lock"))
    assert stream.write("first line\n") == len("first line\n")
    assert target.read_text(encoding="utf-8") == ""
    stream.flush()
    stream.write("zweite Zeile\n")
    stream.flush()
    stream.flush()  # nothing pending: no-op
    assert target.read_text(encoding="utf-8") == "first line\nzweite Zeile\n"
    stream.close()


def test_close_flushes_once_and_later_writes_fail(tmp_path: Path) -> None:
    target = tmp_path / "out.log"
    target.touch()
    stream = ar.ContainerAuditStream(str(target), str(tmp_path / "lock"))
    stream.write("pending\n")
    stream.close()
    stream.close()
    assert stream.closed
    assert target.read_text(encoding="utf-8") == "pending\n"
    with pytest.raises(ValueError, match="closed"):
        stream.write("late\n")


def test_the_lock_file_is_private(tmp_path: Path) -> None:
    target = tmp_path / "out.log"
    target.touch()
    lock = tmp_path / "lock"
    ar.ContainerAuditStream(str(target), str(lock)).close()
    assert stat.S_IMODE(lock.stat().st_mode) & 0o077 == 0


def test_an_unreachable_target_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        ar.ContainerAuditStream(str(tmp_path / "missing" / "out.log"), str(tmp_path / "lock"))
    target = tmp_path / "out.log"
    target.touch()
    with pytest.raises(OSError):
        ar.ContainerAuditStream(str(target), str(tmp_path / "missing" / "lock"))


_WRITER = """
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("audit_runtime", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
stream = module.ContainerAuditStream(sys.argv[2], sys.argv[3])
writer = int(sys.argv[4])
for n in range(int(sys.argv[5])):
    stream.write(json.dumps({"writer": writer, "n": n, "pad": "x" * 20000}) + "\\n")
    stream.flush()
stream.close()
"""


def test_concurrent_writers_never_interleave_long_lines(tmp_path: Path) -> None:
    # Two processes share one pipe, each through its own stream, like the
    # long-running server and a command started with `docker compose exec`.
    # Lines of 20 kB exceed the atomic pipe write size many times over.
    read_fd, write_fd = os.pipe()
    chunks: list[bytes] = []

    def drain() -> None:
        with os.fdopen(read_fd, "rb") as pipe:
            while block := pipe.read(65536):
                chunks.append(block)

    reader = threading.Thread(target=drain)
    reader.start()
    lines_per_writer = 40
    procs = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                _WRITER,
                str(RUNTIME_PATH),
                f"/dev/fd/{write_fd}",
                str(tmp_path / "lock"),
                str(writer),
                str(lines_per_writer),
            ],
            pass_fds=(write_fd,),
        )
        for writer in (1, 2)
    ]
    os.close(write_fd)
    for proc in procs:
        assert proc.wait(timeout=60) == 0
    reader.join(timeout=60)
    lines = b"".join(chunks).decode("utf-8").split("\n")
    assert lines[-1] == ""
    records = [json.loads(line) for line in lines[:-1]]
    assert len(records) == 2 * lines_per_writer
    assert all(len(record["pad"]) == 20000 for record in records)
    for writer in (1, 2):
        assert [r["n"] for r in records if r["writer"] == writer] == list(range(lines_per_writer))


def test_an_emitter_chain_through_the_stream_verifies(tmp_path: Path) -> None:
    target = tmp_path / "out.log"
    target.touch()
    stream = ar.open_audit_stream("container", path=str(target), lock_path=str(tmp_path / "lock"))
    assert stream is not None
    emitter = ae.AuditEmitter("example-app", "1.2.3", stream=stream, heartbeat_interval_s=0)
    emitter.start()
    case_ref, reason = ar.parse_purpose_header(ar.encode_purpose("case-0001", "routine check"))
    emitter.emit(
        "query",
        "search.run",
        ae.Actor(user="alice.example", via="edge"),
        purpose=ae.Purpose(case_ref=case_ref, reason=reason),
        request_id=ar.clean_request_id("3f8a1c2e-0b4d-4e6f-9a7b-1c2d3e4f5a6b"),
        target=ae.Target(kind="document", ids=["doc-1"], count=1),
    )
    emitter.close()
    stream.close()
    events = [json.loads(line) for line in target.read_text(encoding="utf-8").split("\n") if line]
    assert [e["operation"] for e in events] == ["start", "search.run", "shutdown"]
    assert events[1]["purpose"] == {"case_ref": "case-0001", "reason": "routine check"}
    report = ae.verify_chain(events)
    assert report.ok, report.problems


@pytest.mark.parametrize("value", [None, "", "stdout", " STDOUT "])
def test_stdout_mode_leaves_the_emitter_default(value: str | None) -> None:
    assert ar.open_audit_stream(value) is None


def test_an_unknown_output_mode_is_refused() -> None:
    with pytest.raises(ValueError, match="AUDIT_OUTPUT"):
        ar.open_audit_stream("file")


def test_container_mode_is_case_insensitive(tmp_path: Path) -> None:
    target = tmp_path / "out.log"
    target.touch()
    stream = ar.open_audit_stream(" Container ", path=str(target), lock_path=str(tmp_path / "lock"))
    assert isinstance(stream, ar.ContainerAuditStream)
    stream.close()


def test_a_symlinked_lock_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "out.log"
    target.touch()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.touch()
    lock = tmp_path / "lock"
    lock.symlink_to(elsewhere)
    with pytest.raises(OSError):
        ar.ContainerAuditStream(str(target), str(lock))


def test_a_lock_others_can_write_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "out.log"
    target.touch()
    lock = tmp_path / "lock"
    lock.touch()
    lock.chmod(0o666)
    with pytest.raises(PermissionError, match="private regular file"):
        ar.ContainerAuditStream(str(target), str(lock))


def test_a_lock_of_another_user_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "out.log"
    target.touch()
    lock = tmp_path / "lock"
    lock.touch(mode=0o600)
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(lock).st_uid + 1)
    with pytest.raises(PermissionError, match="private regular file"):
        ar.ContainerAuditStream(str(target), str(lock))


def test_a_held_lock_fails_the_write_after_the_timeout(tmp_path: Path) -> None:
    import fcntl

    target = tmp_path / "out.log"
    target.touch()
    lock = tmp_path / "lock"
    stream = ar.ContainerAuditStream(str(target), str(lock), lock_timeout_s=0.05)
    holder = os.open(lock, os.O_RDWR)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        stream.write("blocked line\n")
        with pytest.raises(TimeoutError):
            stream.flush()
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)
    stream.write("next line\n")
    stream.flush()
    stream.close()
    # The timed-out line was dropped, never written later.
    assert target.read_text(encoding="utf-8") == "next line\n"


def test_an_emitter_reports_a_lock_timeout_as_a_write_error(tmp_path: Path) -> None:
    import fcntl

    target = tmp_path / "out.log"
    target.touch()
    lock = tmp_path / "lock"
    stream = ar.ContainerAuditStream(str(target), str(lock), lock_timeout_s=0.05)
    emitter = ae.AuditEmitter("example-app", "1.2.3", stream=stream, heartbeat_interval_s=0)
    emitter.start()
    holder = os.open(lock, os.O_RDWR)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        with pytest.raises(ae.AuditWriteError):
            emitter.emit("query", "search.run", ae.Actor(user="alice.example", via="edge"))
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)
    emitter.emit("query", "search.run", ae.Actor(user="alice.example", via="edge"))
    emitter.close()
    stream.close()
    events = [json.loads(line) for line in target.read_text(encoding="utf-8").split("\n") if line]
    assert [e["seq"] for e in events] == [0, 1, 2]
    assert ae.verify_chain(events).ok
