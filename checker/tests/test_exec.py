from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from helpers import parse

from anystatus.checks.exec_cmd import MAX_OUTPUT, parse_plugin_output
from anystatus.judge import judge
from anystatus.model import CheckFailure, Failure, Observation
from anystatus.scheduler import execute
from anystatus.table import ConfigError

# Windows programs need SYSTEMROOT; it is only passed because the tests list it (Linux: not set, so skipped).
BASE_ENV = ["SYSTEMROOT"]


def spec_for(tmp_path: Path, script: str, *, args: tuple[str, ...] = (), extra: str = "", env: list[str] | None = None):  # type: ignore[no-untyped-def]
    path = tmp_path / "plugin.py"
    path.write_text(script, encoding="utf-8")
    command = json.dumps([sys.executable, str(path), *args])
    env_names = json.dumps(BASE_ENV + (env or []))
    text = f'[[check]]\nid = "x"\ntype = "exec"\ncommand = {command}\nenv = {env_names}\ntimeout = "20s"\n{extra}\n'
    return parse(text, tmp_path).checks[0]


def exit_with(code: int, line: str) -> str:
    return f"import sys\nprint({line!r})\nsys.exit({code})\n"


@pytest.mark.parametrize(("code", "level"), [(0, "ok"), (1, "warn"), (2, "crit"), (3, "unknown")])
def test_exit_codes_are_levels(tmp_path: Path, code: int, level: str) -> None:
    spec = spec_for(tmp_path, exit_with(code, "SERVICE: all there is to say | time=1.5s;1;2;0"))
    outcome = execute(spec)
    assert outcome == Observation(level=level, summary="SERVICE: all there is to say", value=1.5, unit="s")
    assert judge(spec, outcome).level == level


def test_the_value_has_no_limits_applied(tmp_path: Path) -> None:
    spec = spec_for(tmp_path, exit_with(0, "OK | load=99%;1;2"))
    core = judge(spec, execute(spec))
    assert (core.level, core.value, core.unit) == ("ok", 99.0, "%")  # the plugin's own thresholds win


@pytest.mark.parametrize("code", [4, 127, 255])
def test_another_exit_code_is_a_failure(tmp_path: Path, code: int) -> None:
    spec = spec_for(tmp_path, exit_with(code, "CRITICAL - looks like a level"))
    outcome = execute(spec)
    assert isinstance(outcome, Failure) and outcome.summary == f"exit code {code}"
    assert judge(spec, outcome).level == "unknown"


def test_a_summary_template_applies(tmp_path: Path) -> None:
    spec = spec_for(tmp_path, exit_with(0, "fine | t=0.25s"), extra='summary = "took {value} {unit}"')
    assert judge(spec, execute(spec)).summary == "took 0.25 s"


def test_no_output_at_all(tmp_path: Path) -> None:
    ok = execute(spec_for(tmp_path, "import sys\nsys.exit(0)\n"))
    crit = execute(spec_for(tmp_path, "import sys\nsys.exit(2)\n"))
    assert ok == Observation(level="ok")
    assert isinstance(crit, Observation) and crit.summary == "no output (exit code 2)"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("OK - up", ("OK - up", None, None)),
        ("OK|time=0.12s;1;2", ("OK", 0.12, "s")),
        ("OK | size=12345B", ("OK", 12345.0, "B")),
        ("OK | pct=87.5%;80;90;0;100 other=1", ("OK", 87.5, "%")),
        ("OK | 'free space'=1.5GB;;;;", ("OK", 1.5, "GB")),
        ("OK | count=7", ("OK", 7.0, None)),
        ("OK | neg=-3.5C", ("OK", -3.5, "C")),
        ("OK | lat=1e3ms", ("OK", 1000.0, "ms")),
        ("OK | broken=U", ("OK", None, None)),
        ("OK | not perfdata at all", ("OK", None, None)),
        ("OK |", ("OK", None, None)),
        ("first | a=1s\nsecond | b=2s\nmore", ("first", 1.0, "s")),
        ("\nsecond line only", ("", None, None)),
        ("", ("", None, None)),
    ],
)
def test_parsing_plugin_output(text: str, expected: tuple[str, float | None, str | None]) -> None:
    assert parse_plugin_output(text) == expected


def test_arguments_reach_the_program_without_a_shell(tmp_path: Path) -> None:
    script = "import sys\nprint(sys.argv[1])\n"
    nasty = "a; echo b && c > d $HOME `x` 'q'"
    outcome = execute(spec_for(tmp_path, script, args=(nasty,)))
    assert isinstance(outcome, Observation) and outcome.summary == nasty


def test_stderr_goes_to_the_debug_log_masked_and_never_into_the_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("ANYSTATUS_TEST_TOKEN", "hunter2-token")
    script = (
        "import os, sys\nsys.stderr.write('trouble with ' + os.environ['ANYSTATUS_TEST_TOKEN'] + '\\n')\nprint('WARNING: degraded')\nsys.exit(1)\n"
    )
    spec = spec_for(tmp_path, script, env=["ANYSTATUS_TEST_TOKEN"])
    with caplog.at_level(logging.DEBUG, logger="anystatus"):
        outcome = execute(spec)
    assert outcome == Observation(level="warn", summary="WARNING: degraded")
    assert "trouble with ***" in caplog.text
    assert "hunter2" not in caplog.text


def test_stderr_of_a_failing_plugin_stays_out_of_the_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANYSTATUS_TEST_TOKEN", "hunter2-token")
    script = "import sys\nsys.stderr.write('password is hunter2-token')\nsys.exit(9)\n"
    outcome = execute(spec_for(tmp_path, script, env=["ANYSTATUS_TEST_TOKEN"]))
    assert isinstance(outcome, Failure)
    assert outcome.summary == "exit code 9"
    assert "hunter2" not in outcome.summary


def test_only_the_listed_environment_variables_are_passed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANYSTATUS_TEST_A", "alpha")
    monkeypatch.setenv("ANYSTATUS_TEST_B", "bravo")
    script = "import json, os\nprint(json.dumps(dict(os.environ)))\n"
    spec = spec_for(tmp_path, script, env=["ANYSTATUS_TEST_A", "ANYSTATUS_TEST_NOT_SET"])
    outcome = execute(spec)
    assert isinstance(outcome, Observation) and outcome.summary is not None
    seen = json.loads(outcome.summary)
    assert seen["ANYSTATUS_TEST_A"] == "alpha"
    assert "ANYSTATUS_TEST_B" not in seen  # set here, not listed
    assert "ANYSTATUS_TEST_NOT_SET" not in seen  # listed, not set
    assert "PATH" not in seen and "HOME" not in seen and "USERPROFILE" not in seen  # nothing else, PATH included


def test_path_is_passed_only_when_listed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", os.environ.get("PATH", "") or "x")
    script = "import os\nprint('PATH' in os.environ)\n"
    assert execute(spec_for(tmp_path, script)) == Observation(level="ok", summary="False")
    assert execute(spec_for(tmp_path, script, env=["PATH"])) == Observation(level="ok", summary="True")


def alive(pid: int) -> bool:
    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, check=False).stdout
        return str(pid) in out.split()
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_timeout_kills_the_process(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    script = f"import os, time\nopen({str(pid_file)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)\nprint('too late')\n"
    spec = spec_for(tmp_path, script)
    started = time.monotonic()
    with pytest.raises(CheckFailure) as caught:
        spec.check.run(2.0)  # the scheduler passes spec.timeout; 20 s would make this test slow
    assert caught.value.summary == "timed out after 2 s"
    assert time.monotonic() - started < 15
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not alive(pid), "the process is still running"


def test_a_timeout_through_the_scheduler_is_a_failure(tmp_path: Path) -> None:
    command = json.dumps([sys.executable, "-c", "import time; time.sleep(60)"])
    text = f'[[check]]\nid = "x"\ntype = "exec"\ncommand = {command}\nenv = {json.dumps(BASE_ENV)}\ntimeout = "1s"\n'
    spec = parse(text, tmp_path).checks[0]
    outcome = execute(spec)
    assert outcome == Failure("timed out after 1 s")
    assert judge(spec, outcome).level == "unknown"


def test_the_output_is_capped(tmp_path: Path) -> None:
    script = "import sys\nsys.stdout.write('x' * 3_000_000)\nsys.stderr.write('y' * 3_000_000)\n"
    spec = spec_for(tmp_path, script)
    started = time.monotonic()
    outcome = spec.check.run(20.0)
    assert time.monotonic() - started < 15  # a plugin that talks a lot is neither stuck nor read to the end
    assert isinstance(outcome, Observation) and outcome.summary is not None
    assert len(outcome.summary) == MAX_OUTPUT


def test_a_missing_program_is_a_failure(tmp_path: Path) -> None:
    spec = parse('[[check]]\nid = "x"\ntype = "exec"\ncommand = ["/no/such/anystatus-plugin"]\n', tmp_path).checks[0]
    outcome = execute(spec)
    assert isinstance(outcome, Failure) and outcome.summary == "command not found"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('command = "/usr/lib/nagios/plugins/check_x -w 5"', "must be a list.*without a shell"),
        ("command = []", "'command' must be a non-empty list"),
        ("command = [1, 2]", "'command' must be a non-empty list"),
        ('command = [""]', "empty program name"),
        ('command = ["x"]\nwarn = 5', "exec takes the plugin's own thresholds: 'warn' is not used"),
        ('command = ["x"]\ncrit = 5', "'crit' is not used"),
        ('command = ["x"]\nworse = "below"', "'worse' is not used"),
        ('command = ["x"]\nenv = "PATH"', "'env' must be a list"),
        ('command = ["x"]\nenv = [1]', "'env' must be a list"),
        ("", "missing 'command'"),
    ],
)
def test_config_errors(tmp_path: Path, body: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse(f'[[check]]\nid = "x"\ntype = "exec"\n{body}\n', tmp_path)
