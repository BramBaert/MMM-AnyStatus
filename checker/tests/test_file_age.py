from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from helpers import parse

from anystatus.checks.file_age import parse_size
from anystatus.judge import judge
from anystatus.model import Failure, Observation, Reading
from anystatus.scheduler import execute
from anystatus.table import ConfigError


def spec_for(tmp_path: Path, path: str, extra: str = ""):  # type: ignore[no-untyped-def]
    text = f'[[check]]\nid = "f"\ntype = "file_age"\npath = "{path}"\n{extra}\n'
    return parse(text, tmp_path).checks[0]


def make(folder: Path, name: str, age: float, size: int = 10) -> Path:
    path = folder / name
    path.write_bytes(b"x" * size)
    moment = time.time() - age
    os.utime(path, (moment, moment))
    return path


def pattern(folder: Path, glob: str) -> str:
    return (folder / glob).as_posix()


def test_the_newest_match_counts_and_its_name_is_a_placeholder(tmp_path: Path) -> None:
    make(tmp_path, "backup-1.tar", age=5 * 86400)
    make(tmp_path, "backup-2.tar", age=3600)
    make(tmp_path, "notes.txt", age=10)  # newer, but not a match
    spec = spec_for(tmp_path, pattern(tmp_path, "backup-*.tar"), 'summary = "{file} is {age} old"')
    outcome = execute(spec)
    assert isinstance(outcome, Reading) and outcome.seconds
    assert 3590 < outcome.value < 3700
    assert judge(spec, outcome).summary == "backup-2.tar is 1.0 h old"


def test_the_default_summary_and_duration_limits(tmp_path: Path) -> None:
    make(tmp_path, "a.bak", age=3 * 86400)
    spec = spec_for(tmp_path, pattern(tmp_path, "*.bak"), 'warn = "2d"\ncrit = "4d"')
    core = judge(spec, execute(spec))
    assert (core.level, core.summary, core.unit) == ("warn", "newest file is 3.0 d old", "d")


def test_min_size_ignores_a_stub(tmp_path: Path) -> None:
    make(tmp_path, "big.tar", age=2 * 86400, size=2000)
    make(tmp_path, "stub.tar", age=60, size=5)  # the newest, but a failed run leaves a stub
    spec = spec_for(tmp_path, pattern(tmp_path, "*.tar"), 'min_size = "1KB"')
    outcome = execute(spec)
    assert isinstance(outcome, Reading) and 2 * 86400 - 60 < outcome.value < 2 * 86400 + 60
    assert spec.check.min_size == 1000  # type: ignore[attr-defined]


def test_nothing_big_enough_is_no_match(tmp_path: Path) -> None:
    make(tmp_path, "stub.tar", age=60, size=5)
    spec = spec_for(tmp_path, pattern(tmp_path, "*.tar"), 'min_size = "1MB"')
    assert execute(spec) == Observation(level="crit", summary="no file matches")


def test_no_match_is_crit(tmp_path: Path) -> None:
    spec = spec_for(tmp_path, pattern(tmp_path, "*.tar"))
    outcome = execute(spec)
    assert outcome == Observation(level="crit", summary="no file matches")
    assert judge(spec, outcome).level == "crit"


def test_directories_do_not_count(tmp_path: Path) -> None:
    (tmp_path / "dir.tar").mkdir()
    spec = spec_for(tmp_path, pattern(tmp_path, "*.tar"))
    assert execute(spec) == Observation(level="crit", summary="no file matches")


def test_a_folder_that_is_not_there_is_no_match(tmp_path: Path) -> None:
    spec = spec_for(tmp_path, pattern(tmp_path / "gone", "*.tar"))
    assert execute(spec) == Observation(level="crit", summary="no file matches")


def test_an_unreadable_folder_is_a_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(path: object) -> list[str]:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "listdir", denied)
    spec = spec_for(tmp_path, pattern(tmp_path, "*.tar"))
    outcome = execute(spec)
    assert isinstance(outcome, Failure) and outcome.summary == "cannot read the folder"
    assert judge(spec, outcome).level == "unknown"


def test_a_relative_path_is_relative_to_the_config_file(tmp_path: Path) -> None:
    make(tmp_path, "rel.bak", age=100)
    spec = spec_for(tmp_path, "*.bak")
    assert isinstance(execute(spec), Reading)


def test_a_future_modification_time_is_age_zero(tmp_path: Path) -> None:
    make(tmp_path, "future.bak", age=-3600)
    outcome = execute(spec_for(tmp_path, pattern(tmp_path, "*.bak")))
    assert isinstance(outcome, Reading) and outcome.value == 0


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 0), (1500, 1500), ("1500", 1500), ("1KB", 1000), ("1MB", 1_000_000), ("1.5 GB", 1_500_000_000),
     ("2tb", 2 * 10**12), ("10B", 10)],
)  # fmt: skip
def test_sizes(value: object, expected: int) -> None:
    assert parse_size(value) == expected


@pytest.mark.parametrize("value", ["", "MB", "1 MiB", "1XB", "-1", -5, True, 1.5, [1]])
def test_bad_sizes(value: object) -> None:
    with pytest.raises(ValueError, match="size"):
        parse_size(value)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('min_size = "lots"', "'min_size': not a size"),
        ("min_size = 1.5", "'min_size': not a size"),
        ("", "missing 'path'"),
        ('path = "x"\nwarn = "soon"', "'warn'"),
    ],
)
def test_config_errors(tmp_path: Path, body: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse(f'[[check]]\nid = "f"\ntype = "file_age"\n{body}\n', tmp_path)
