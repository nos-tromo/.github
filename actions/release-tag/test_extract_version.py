import pytest
from extract_version import extract, is_increase, main


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return str(p)


def test_extract_pyproject(tmp_path):
    f = _write(tmp_path, "pyproject.toml", '[project]\nname = "x"\nversion = "1.2.3"\n')
    assert extract(f, "pyproject") == "1.2.3"


def test_extract_plain_first_nonempty(tmp_path):
    f = _write(tmp_path, "VERSION", "\n  0.1.0  \nignored\n")
    assert extract(f, "plain") == "0.1.0"


def test_extract_pyproject_missing_version(tmp_path):
    f = _write(tmp_path, "pyproject.toml", '[project]\nname = "x"\n')
    with pytest.raises(ValueError):
        extract(f, "pyproject")


def test_extract_rejects_non_semver(tmp_path):
    f = _write(tmp_path, "VERSION", "1.2\n")
    with pytest.raises(ValueError):
        extract(f, "plain")


def test_extract_unknown_source(tmp_path):
    f = _write(tmp_path, "VERSION", "1.2.3\n")
    with pytest.raises(ValueError):
        extract(f, "bogus")


def test_cli_extract(tmp_path, capsys):
    f = _write(tmp_path, "pyproject.toml", '[project]\nversion = "3.4.5"\n')
    rc = main(["extract", "--file", f, "--source", "pyproject"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "3.4.5"


def test_is_increase():
    assert is_increase("1.1.0", "1.0.1")
    assert is_increase("2.0.0", "1.9.9")
    assert not is_increase("1.0.1", "1.0.1")   # equal is not an increase
    assert not is_increase("1.0.0", "1.0.1")   # downgrade


def test_cli_check_increase():
    assert main(["check-increase", "--new", "1.1.0", "--latest", "1.0.0"]) == 0
    assert main(["check-increase", "--new", "1.0.0", "--latest", "1.0.0"]) == 1
