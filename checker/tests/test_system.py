from __future__ import annotations

from pathlib import Path

import pytest
from helpers import parse

from anystatus.checks import system
from anystatus.judge import judge
from anystatus.model import Failure, Reading
from anystatus.scheduler import execute
from anystatus.table import ConfigError

MEMINFO = "MemTotal:        8000000 kB\nMemFree:          100000 kB\nMemAvailable:    2000000 kB\nBuffers: 5 kB\n"


@pytest.fixture(autouse=True)
def on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system, "_is_linux", lambda: True)


def put(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="ascii")


def spec_for(tmp_path: Path, measure: str, extra: str = "", root: Path | None = None):  # type: ignore[no-untyped-def]
    text = f'[[check]]\nid = "s"\ntype = "system"\nmeasure = "{measure}"\n{extra}\n'
    spec = parse(text, tmp_path).checks[0]
    spec.check.root = root or tmp_path / "fake"  # type: ignore[attr-defined]
    return spec


def test_temperature_is_the_highest_zone(tmp_path: Path) -> None:
    root = tmp_path / "fake"
    put(root, "sys/class/thermal/thermal_zone0/temp", "45000\n")
    put(root, "sys/class/thermal/thermal_zone1/temp", "61500\n")
    put(root, "sys/class/thermal/cooling_device0/temp", "99000\n")  # not a thermal zone
    spec = spec_for(tmp_path, "temperature", "warn = 60\ncrit = 80")
    outcome = execute(spec)
    assert outcome == Reading(value=61.5, unit="°C", text="61.5 °C (thermal_zone1)", fields={"zone": "thermal_zone1"})
    core = judge(spec, outcome)
    assert (core.level, core.summary, core.unit) == ("warn", "61.5 °C (thermal_zone1)", "°C")


def test_temperature_of_a_named_zone(tmp_path: Path) -> None:
    root = tmp_path / "fake"
    put(root, "sys/class/thermal/thermal_zone0/temp", "45000")
    put(root, "sys/class/thermal/thermal_zone1/temp", "61500")
    spec = spec_for(tmp_path, "temperature", 'zone = "thermal_zone0"\nsummary = "{zone}: {value}{unit}"')
    assert judge(spec, execute(spec)).summary == "thermal_zone0: 45°C"


def test_a_zone_that_cannot_be_read_is_skipped(tmp_path: Path) -> None:
    root = tmp_path / "fake"
    put(root, "sys/class/thermal/thermal_zone0/temp", "Invalid argument")
    put(root, "sys/class/thermal/thermal_zone1/temp", "38000")
    outcome = execute(spec_for(tmp_path, "temperature"))
    assert isinstance(outcome, Reading) and outcome.value == 38.0


def test_a_missing_zone_or_sensor_is_a_failure(tmp_path: Path) -> None:
    spec = spec_for(tmp_path, "temperature")
    assert execute(spec) == Failure("no temperature sensor", None)  # no /sys at all
    put(tmp_path / "fake", "sys/class/thermal/thermal_zone0/temp", "40000")
    named = spec_for(tmp_path, "temperature", 'zone = "thermal_zone9"')
    outcome = execute(named)
    assert isinstance(outcome, Failure) and outcome.summary == "no thermal zone thermal_zone9"
    assert judge(named, outcome).level == "unknown"


def test_load_is_the_one_minute_average_per_cpu(tmp_path: Path) -> None:
    root = tmp_path / "fake"
    put(root, "proc/loadavg", "2.00 1.50 1.00 1/200 12345\n")
    put(root, "proc/cpuinfo", "".join(f"processor\t: {i}\nmodel name\t: x\n\n" for i in range(4)))
    spec = spec_for(tmp_path, "load", "warn = 0.5\ncrit = 1")
    outcome = execute(spec)
    assert isinstance(outcome, Reading) and outcome.value == 0.5
    assert outcome.text == "load 0.5 per CPU (2 on 4)"
    assert judge(spec, outcome).level == "warn"


def test_load_without_cpuinfo_uses_the_cpu_count_of_this_machine(tmp_path: Path) -> None:
    put(tmp_path / "fake", "proc/loadavg", "1.00 0 0 1/1 1\n")
    outcome = execute(spec_for(tmp_path, "load"))
    assert isinstance(outcome, Reading) and outcome.value > 0


def test_memory_used_percent_from_meminfo(tmp_path: Path) -> None:
    put(tmp_path / "fake", "proc/meminfo", MEMINFO)
    spec = spec_for(tmp_path, "memory", "warn = 70\ncrit = 90")
    outcome = execute(spec)
    assert isinstance(outcome, Reading) and outcome.value == 75.0 and outcome.unit == "%"
    assert outcome.text == "75 % of 8.2 GB used"
    assert judge(spec, outcome).level == "warn"


def cgroup(root: Path, limit: str, current: int, inactive: int) -> None:
    put(root, "sys/fs/cgroup/memory.max", limit + "\n")
    put(root, "sys/fs/cgroup/memory.current", f"{current}\n")
    put(root, "sys/fs/cgroup/memory.stat", f"anon 1\ninactive_file {inactive}\nactive_file 5\n")


def test_a_cgroup_memory_limit_replaces_the_machines_memory(tmp_path: Path) -> None:
    root = tmp_path / "fake"
    put(root, "proc/meminfo", MEMINFO)  # the host: 75 % used
    cgroup(root, "1000000000", current=600_000_000, inactive=100_000_000)  # the container: 500 MB of 1 GB
    spec = spec_for(tmp_path, "memory")
    outcome = execute(spec)
    assert isinstance(outcome, Reading) and outcome.value == 50.0
    assert outcome.text == "50 % of 1.0 GB used, container limit"


@pytest.mark.parametrize("limit", ["max", "99999999999999", "0", "garbage"])
def test_a_limit_that_does_not_apply_is_ignored(tmp_path: Path, limit: str) -> None:
    root = tmp_path / "fake"
    put(root, "proc/meminfo", MEMINFO)
    cgroup(root, limit, current=1, inactive=0)
    outcome = execute(spec_for(tmp_path, "memory"))
    assert isinstance(outcome, Reading) and outcome.value == 75.0


def test_memory_without_the_figures_is_a_failure(tmp_path: Path) -> None:
    assert isinstance(execute(spec_for(tmp_path, "memory")), Failure)
    put(tmp_path / "fake", "proc/meminfo", "MemTotal: 100 kB\n")
    outcome = execute(spec_for(tmp_path, "memory"))
    assert isinstance(outcome, Failure) and outcome.summary == "cannot read the memory figures"


def test_uptime_is_a_duration(tmp_path: Path) -> None:
    put(tmp_path / "fake", "proc/uptime", "190800.55 700000.00\n")
    spec = spec_for(tmp_path, "uptime", 'warn = "30d"\nworse = "above"')
    outcome = execute(spec)
    assert isinstance(outcome, Reading) and outcome.seconds and outcome.value == pytest.approx(190800.55)
    core = judge(spec, outcome)
    assert (core.level, core.summary, core.value, core.unit) == ("ok", "up 2.2 d", 2.2, "d")


def test_uptime_limits_may_be_durations_but_other_measures_take_numbers(tmp_path: Path) -> None:
    put(tmp_path / "fake", "proc/uptime", "100.0 1.0\n")
    spec = spec_for(tmp_path, "uptime", 'warn = "1h"\ncrit = "1m"\nworse = "below"')
    assert spec.limits.warn == 3600 and spec.limits.crit == 60
    assert judge(spec, execute(spec)).level == "warn"  # a machine that rebooted 100 s ago
    with pytest.raises(ConfigError, match="'warn' must be a number"):
        parse('[[check]]\nid = "s"\ntype = "system"\nmeasure = "load"\nwarn = "1h"\n', tmp_path)


def test_no_default_limits_and_higher_is_worse(tmp_path: Path) -> None:
    spec = spec_for(tmp_path, "temperature")
    assert (spec.limits.warn, spec.limits.crit, spec.limits.worse) == (None, None, "above")


def test_a_missing_file_is_a_failure(tmp_path: Path) -> None:
    for measure in ("load", "uptime"):
        outcome = execute(spec_for(tmp_path, measure))
        assert isinstance(outcome, Failure) and outcome.summary.startswith("cannot read")


def test_garbage_in_a_file_is_a_failure(tmp_path: Path) -> None:
    put(tmp_path / "fake", "proc/uptime", "soon\n")
    assert isinstance(execute(spec_for(tmp_path, "uptime")), Failure)


def test_not_linux_is_a_failure_not_a_config_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system, "_is_linux", lambda: False)
    put(tmp_path / "fake", "proc/uptime", "100.0 1.0\n")
    spec = spec_for(tmp_path, "uptime")  # the config loads
    outcome = execute(spec)
    assert outcome == Failure("system checks only work on Linux", None)
    assert judge(spec, outcome).level == "unknown"


def test_on_this_machine_without_a_fake_root(tmp_path: Path) -> None:
    spec = parse('[[check]]\nid = "s"\ntype = "system"\nmeasure = "load"\n', tmp_path).checks[0]
    outcome = execute(spec)  # a real /proc on Linux; on Windows and macOS the `only on Linux` failure
    assert isinstance(outcome, Reading | Failure)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('measure = "cpu"', "'measure' must be one of"),
        ('measure = "load"\nzone = "thermal_zone0"', "'zone' is only for"),
        ('measure = "temperature"\nzone = "../etc"', "'zone' is only for"),
    ],
)
def test_config_errors(tmp_path: Path, body: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse(f'[[check]]\nid = "s"\ntype = "system"\n{body}\n', tmp_path)
