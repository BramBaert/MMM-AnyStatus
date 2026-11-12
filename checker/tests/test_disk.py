from __future__ import annotations

import shutil
from pathlib import Path
from typing import NamedTuple

import pytest
from helpers import parse

from anystatus.judge import judge
from anystatus.model import Failure, Reading
from anystatus.scheduler import execute
from anystatus.table import ConfigError

GB = 1000**3


class Usage(NamedTuple):
    total: int
    used: int
    free: int


def spec_for(tmp_path: Path, extra: str = "", path: str = "."):  # type: ignore[no-untyped-def]
    text = f'[[check]]\nid = "d"\ntype = "disk"\npath = "{path}"\n{extra}\n'
    return parse(text, tmp_path).checks[0]


def fake(monkeypatch: pytest.MonkeyPatch, total_gb: float, used_gb: float, free_gb: float) -> list[str]:
    asked: list[str] = []

    def disk_usage(path: str) -> Usage:
        asked.append(str(path))
        return Usage(int(total_gb * GB), int(used_gb * GB), int(free_gb * GB))

    monkeypatch.setattr(shutil, "disk_usage", disk_usage)
    return asked


@pytest.mark.parametrize(
    ("used", "free", "level"),
    [
        (50, 50, "ok"),
        (84, 16, "ok"),
        (85, 15, "warn"),
        (94.9, 5.1, "warn"),
        (95, 5, "crit"),
        (100, 0, "crit"),
    ],
)
def test_used_percent_with_the_default_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, used: float, free: float, level: str) -> None:
    fake(monkeypatch, used + free, used, free)
    spec = spec_for(tmp_path)
    outcome = execute(spec)
    assert isinstance(outcome, Reading) and outcome.unit == "%"
    assert outcome.value == pytest.approx(used, abs=0.05)
    assert judge(spec, outcome).level == level


def test_the_summary_and_the_free_placeholder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake(monkeypatch, 1000, 900, 100)
    assert judge(spec_for(tmp_path), execute(spec_for(tmp_path))).summary == "90 % used, 100 GB free"
    spec = spec_for(tmp_path, 'summary = "{value}{unit} full, {free} GB left"')
    assert judge(spec, execute(spec)).summary == "90% full, 100 GB left"


def test_reserved_blocks_do_not_hide_a_full_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake(monkeypatch, 100, 95, 0)  # 5 % of the disk is reserved for root: for everyone else it is full
    spec = spec_for(tmp_path)
    assert execute(spec) == Reading(value=100.0, unit="%", text="100 % used, 0 GB free", fields={"free": "0"})


def test_own_limits_replace_the_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake(monkeypatch, 100, 60, 40)
    spec = spec_for(tmp_path, "warn = 50\ncrit = 70")
    assert judge(spec, execute(spec)).level == "warn"


def test_measure_free_is_gigabytes_worse_below_with_no_default_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake(monkeypatch, 1000, 999, 1)
    spec = spec_for(tmp_path, 'measure = "free"')
    outcome = execute(spec)
    assert outcome == Reading(value=1.0, unit="GB", text="1 GB free", fields={"free": "1"})
    assert judge(spec, outcome).level == "ok"  # no default limits, even for 1 GB left
    assert spec.limits.worse == "below"
    assert (spec.limits.warn, spec.limits.crit) == (None, None)


@pytest.mark.parametrize(("free", "level"), [(50, "ok"), (20, "warn"), (10, "warn"), (5, "crit"), (1, "crit")])
def test_measure_free_limits_are_reached_downwards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, free: float, level: str) -> None:
    fake(monkeypatch, 1000, 1000 - free, free)
    spec = spec_for(tmp_path, 'measure = "free"\nwarn = 20\ncrit = 5')
    assert judge(spec, execute(spec)).level == level


def test_the_path_is_relative_to_the_config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asked = fake(monkeypatch, 100, 10, 90)
    execute(spec_for(tmp_path, path="data"))
    assert asked == [str(tmp_path / "data")]


def test_a_missing_path_is_a_failure(tmp_path: Path) -> None:
    spec = spec_for(tmp_path, path="no/such/place")
    outcome = execute(spec)
    assert isinstance(outcome, Failure) and outcome.summary == "path not found"
    assert judge(spec, outcome).level == "unknown"


def test_other_errors_are_a_failure_without_the_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(path: str) -> Usage:
        raise PermissionError(13, "secret detail")

    monkeypatch.setattr(shutil, "disk_usage", broken)
    outcome = execute(spec_for(tmp_path))
    assert isinstance(outcome, Failure) and "secret" not in outcome.summary


def test_a_filesystem_without_a_size_is_a_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake(monkeypatch, 0, 0, 0)
    assert isinstance(execute(spec_for(tmp_path)), Failure)


def test_a_real_disk(tmp_path: Path) -> None:
    outcome = execute(spec_for(tmp_path))
    assert isinstance(outcome, Reading) and 0 <= outcome.value <= 100


@pytest.mark.parametrize(
    ("body", "message"),
    [('measure = "used"', "missing 'path'"), ('path = "."\nmeasure = "full"', "'measure' must be one of")],
)
def test_config_errors(tmp_path: Path, body: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse(f'[[check]]\nid = "d"\ntype = "disk"\n{body}\n', tmp_path)
