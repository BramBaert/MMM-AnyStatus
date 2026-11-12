from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from anystatus.cli import main

SCHEMA = json.loads((Path(__file__).parents[2] / "schema" / "results-1.schema.json").read_text(encoding="utf-8"))

GOOD = """
[checker]
name = "cli-test"
[[check]]
id = "one"
type = "python"
function = "support_checks:number"
n = 2
[[check]]
id = "two"
type = "python"
function = "support_checks:decide"
"""


def test_validate_ok(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "c.toml"
    path.write_text(GOOD, encoding="utf-8")
    assert main(["validate", str(path)]) == 0
    assert "2 check(s)" in capsys.readouterr().out


def test_validate_lists_every_problem(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "c.toml"
    path.write_text(
        GOOD.replace('name = "cli-test"', 'colour = "red"').replace('type = "python"\nfunction = "support_checks:decide"', 'type = "nope"'),
        encoding="utf-8",
    )
    assert main(["validate", str(path)]) == 2
    err = capsys.readouterr().err
    assert "colour" in err and "unknown type 'nope'" in err


def test_a_missing_file_and_bad_toml(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", str(tmp_path / "missing.toml")]) == 2
    bad = tmp_path / "bad.toml"
    bad.write_text("[[check\n", encoding="utf-8")
    assert main(["validate", str(bad)]) == 2
    assert "not valid TOML" in capsys.readouterr().err


def test_once_prints_a_valid_document(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "c.toml"
    path.write_text(GOOD, encoding="utf-8")
    assert main(["once", str(path)]) == 0
    doc = json.loads(capsys.readouterr().out)
    jsonschema.validate(doc, SCHEMA)
    assert [r["id"] for r in doc["results"]] == ["one", "two"]
    assert not (tmp_path / "anystatus.sqlite").exists()  # once leaves no memory behind


def test_once_with_id(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "c.toml"
    path.write_text(GOOD, encoding="utf-8")
    assert main(["once", str(path), "--id", "two"]) == 0
    assert [r["id"] for r in json.loads(capsys.readouterr().out)["results"]] == ["two"]
    assert main(["once", str(path), "--id", "zzz"]) == 2
