"""Tests for the canonical audit-event module (configs/audit/audit_event.py)."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import threading
import time
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "configs" / "audit" / "audit_event.py"
SCHEMA_PATH = ROOT / "configs" / "audit" / "audit-event.schema.json"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("audit_event", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["audit_event"] = module
    spec.loader.exec_module(module)
    return module


ae = _load()


def _lines(buffer: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in buffer.getvalue().splitlines()]


def _emitter(buffer: io.StringIO, **kwargs: object) -> object:
    emitter = ae.AuditEmitter("example-app", "1.2.3", stream=buffer, heartbeat_interval_s=0, **kwargs)
    emitter.start()
    return emitter


def _chain(n: int = 3) -> list[dict[str, object]]:
    buffer = io.StringIO()
    emitter = _emitter(buffer)
    for i in range(n):
        emitter.emit(
            "query",
            "tool.example",
            ae.Actor(user="jane.doe", via="edge"),
            request_id=f"req-{i}",
            target=ae.Target(kind="entity", ids=[f"e{i}"], count=1),
        )
    emitter.close()
    return _lines(buffer)


def test_a_chain_starts_at_genesis_and_ends_with_shutdown() -> None:
    events = _chain()
    assert events[0]["operation"] == "start"
    assert events[0]["seq"] == 0
    assert events[0]["prev_hash"] == ae.GENESIS_HASH
    assert events[-1]["operation"] == "shutdown"
    assert [event["seq"] for event in events] == list(range(len(events)))
    for before, after in zip(events, events[1:], strict=False):
        assert after["prev_hash"] == before["hash"]
    report = ae.verify_chain(events)
    assert report.ok, report.problems
    head = next(iter(report.heads.values()))
    assert head.closed is True
    assert head.seq == len(events) - 1


def test_every_line_is_canonical_json_with_the_audit_marker() -> None:
    buffer = io.StringIO()
    emitter = _emitter(buffer)
    emitter.emit("query", "search", ae.Actor(user="jäne", via="edge"), criteria={"q": "Grüße", "b": 1, "a": 2})
    for line in buffer.getvalue().splitlines():
        event = json.loads(line)
        assert line == ae.canonical_json(event)
        assert event["stream"] == "audit"
        assert event["hash"] == ae.event_hash(event)
    assert "Grüße" in buffer.getvalue()


def test_an_altered_field_breaks_the_hash() -> None:
    events = _chain()
    events[2]["actor"] = {"user": "mallory", "via": "edge"}
    kinds = [problem.kind for problem in ae.verify_chain(events).problems]
    assert kinds == ["bad_hash"]


def test_a_rewritten_event_breaks_the_next_link() -> None:
    events = _chain()
    events[2]["actor"] = {"user": "mallory", "via": "edge"}
    events[2]["hash"] = ae.event_hash(events[2])
    kinds = [problem.kind for problem in ae.verify_chain(events).problems]
    assert kinds == ["broken_link"]


def test_a_deleted_event_is_a_gap() -> None:
    events = _chain()
    del events[2]
    problems = ae.verify_chain(events).problems
    assert [problem.kind for problem in problems] == ["seq_gap"]
    assert problems[0].seq == 3


def test_a_duplicated_event_is_a_repeat() -> None:
    events = _chain()
    events.insert(3, dict(events[2]))
    kinds = [problem.kind for problem in ae.verify_chain(events).problems]
    assert kinds == ["seq_repeat"]


def test_order_of_arrival_does_not_matter() -> None:
    events = _chain()
    assert ae.verify_chain(list(reversed(events))).ok


def test_a_window_needs_its_checkpoint() -> None:
    events = _chain(6)
    first, second = events[:4], events[4:]
    assert [problem.kind for problem in ae.verify_chain(second).problems] == ["no_start"]
    report = ae.verify_chain(first)
    assert report.ok
    assert ae.verify_chain(second, checkpoints=report.heads).ok


def test_chains_of_several_instances_are_checked_separately() -> None:
    assert ae.verify_chain(_chain() + _chain()).ok


def test_a_malformed_line_is_a_schema_problem() -> None:
    events = _chain()
    del events[1]["actor"]
    problems = ae.verify_chain(events).problems
    assert problems[0].kind == "schema"


def test_problem_details_never_carry_event_content() -> None:
    events = _chain()
    events[2]["criteria"] = {"q": "secret-search-term"}
    for problem in ae.verify_chain(events).problems:
        assert "secret-search-term" not in problem.detail


def test_long_values_are_capped_and_marked() -> None:
    buffer = io.StringIO()
    emitter = _emitter(buffer)
    event = emitter.emit(
        "query",
        "x" * 1000,
        ae.Actor(user="u" * 1000, via="edge"),
        criteria={f"k{i}": "v" * 1000 for i in range(100)},
        target=ae.Target(kind="document", ids=[str(i) for i in range(ae.MAX_IDS + 5)], count=ae.MAX_IDS + 5),
    )
    assert len(event["operation"]) == ae.MAX_TEXT
    assert str(event["operation"]).endswith(ae.TRUNCATION_MARK)
    assert len(event["actor"]["user"]) == ae.MAX_TEXT
    assert len(event["criteria"]) == ae.MAX_CRITERIA_KEYS
    assert all(len(value) <= ae.MAX_TEXT for value in event["criteria"].values())
    assert len(event["target"]["ids"]) == ae.MAX_IDS
    assert event["target"]["ids_truncated"] is True
    assert event["target"]["count"] == ae.MAX_IDS + 5
    assert ae.verify_chain(_lines(buffer)).ok


def test_optional_sections_are_written_only_when_given() -> None:
    buffer = io.StringIO()
    emitter = _emitter(buffer)
    bare = emitter.emit("delete", "collection.purge", ae.Actor(user="jane", via="edge"))
    for key in ("request_id", "purpose", "target", "criteria", "recipient", "ai", "parent_event_id"):
        assert key not in bare
    full = emitter.emit(
        "disclose",
        "export.csv",
        ae.Actor(user="admin", via="edge", on_behalf_of="jane"),
        purpose=ae.Purpose(case_ref="REF-0001", reason="example review"),
        recipient="download",
        ai=ae.AiCall(model_alias="chat", revision="abc123", output_sha256=ae.sha256_text("answer")),
        parent_event_id=str(bare["event_id"]),
    )
    assert full["purpose"] == {"case_ref": "REF-0001", "reason": "example review"}
    assert full["actor"]["on_behalf_of"] == "jane"
    assert full["ai"]["output_sha256"] == ae.sha256_text("answer")


def test_event_ids_are_uuid7_and_time_ordered() -> None:
    first = ae.new_event_id()
    time.sleep(0.002)
    second = ae.new_event_id()
    for value in (first, second):
        assert value[14] == "7"
        assert value[19] in "89ab"
        assert len(value) == 36
    assert first < second


@pytest.mark.parametrize(
    ("action", "outcome"),
    [("read", "ok"), ("query", "maybe")],
)
def test_unknown_action_or_outcome_is_refused(action: str, outcome: str) -> None:
    emitter = _emitter(io.StringIO())
    with pytest.raises(ValueError):
        emitter.emit(action, "x", ae.Actor(user=None, via="system"), outcome=outcome)


def test_unknown_actor_via_is_refused() -> None:
    emitter = _emitter(io.StringIO())
    with pytest.raises(ValueError):
        emitter.emit("query", "x", ae.Actor(user="jane", via="browser"))


def test_lifecycle_misuse_is_refused() -> None:
    emitter = ae.AuditEmitter("example-app", "1.2.3", stream=io.StringIO(), heartbeat_interval_s=0)
    with pytest.raises(RuntimeError):
        emitter.emit("query", "x", ae.Actor(user=None, via="system"))
    emitter.start()
    with pytest.raises(RuntimeError):
        emitter.start()
    emitter.close()
    emitter.close()
    with pytest.raises(RuntimeError):
        emitter.emit("query", "x", ae.Actor(user=None, via="system"))


def test_a_failed_write_raises_and_does_not_advance_the_chain() -> None:
    class Broken(io.StringIO):
        fail = False

        def write(self, text: str) -> int:
            if self.fail:
                raise OSError("disk full")
            return super().write(text)

    buffer = Broken()
    emitter = _emitter(buffer)
    buffer.fail = True
    with pytest.raises(ae.AuditWriteError):
        emitter.emit("query", "x", ae.Actor(user=None, via="system"))
    buffer.fail = False
    emitter.emit("query", "y", ae.Actor(user=None, via="system"))
    assert ae.verify_chain(_lines(buffer)).ok


def test_the_heartbeat_keeps_the_chain_moving() -> None:
    buffer = io.StringIO()
    emitter = ae.AuditEmitter("example-app", "1.2.3", stream=buffer, heartbeat_interval_s=0.02)
    emitter.start()
    time.sleep(0.15)
    emitter.close()
    events = _lines(buffer)
    beats = [event for event in events if event["operation"] == "heartbeat"]
    assert len(beats) >= 2
    assert events[-1]["operation"] == "shutdown"
    assert ae.verify_chain(events).ok


def test_concurrent_emits_keep_one_consistent_chain() -> None:
    buffer = io.StringIO()
    emitter = _emitter(buffer)

    def work(n: int) -> None:
        for i in range(50):
            emitter.emit("query", f"t{n}.{i}", ae.Actor(user=f"user{n}", via="edge"))

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    emitter.close()
    events = _lines(buffer)
    assert len(events) == 1 + 8 * 50 + 1
    assert ae.verify_chain(events).ok


def test_emitted_events_match_the_published_schema() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    buffer = io.StringIO()
    emitter = _emitter(buffer)
    emitter.emit(
        "disclose",
        "export.csv",
        ae.Actor(user="admin", via="edge", on_behalf_of="jane"),
        request_id="0b5f0e6e-0000-4000-8000-000000000000",
        purpose=ae.Purpose(case_ref="REF-0001"),
        target=ae.Target(kind="collection", ids=["c1"], count=1, container="owner-a"),
        criteria={"format": "csv", "nested": {"a": [1, 2]}},
        recipient="download",
        ai=ae.AiCall(model_alias="chat", sources=["n1"], input_tokens=3, output_tokens=4),
    )
    emitter.close()
    validator = jsonschema.Draft202012Validator(schema)
    for event in _lines(buffer):
        validator.validate(event)
