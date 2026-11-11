from __future__ import annotations

import logging
from pathlib import Path

import pytest
from helpers import make_world, parse

from anystatus.errors import describe_exception, mask
from anystatus.judge import judge
from anystatus.model import Failure, Observation, Reading
from anystatus.scheduler import execute
from anystatus.table import ConfigError


def spec_for(tmp_path: Path, function: str, extra: str = ""):  # type: ignore[no-untyped-def]
    text = f'[[check]]\nid = "p"\ntype = "python"\nfunction = "support_checks:{function}"\n{extra}\n'
    return parse(text, tmp_path).checks[0]


def test_a_number_is_compared_with_the_limits(tmp_path: Path) -> None:
    spec = spec_for(tmp_path, "number", "n = 3\nwarn = 2\ncrit = 0.5\nworse = 'below'")
    outcome = execute(spec)
    assert isinstance(outcome, Reading)
    assert judge(spec, outcome).level == "ok"
    low = spec_for(tmp_path, "number", "n = 0.4\nwarn = 2\ncrit = 0.5\nworse = 'below'")
    assert judge(low, execute(low)).level == "crit"


def test_an_observation_decides_itself(tmp_path: Path) -> None:
    spec = spec_for(tmp_path, "decide", "crit = 1")
    core = judge(spec, execute(spec))
    assert (core.level, core.summary, core.value, core.unit) == ("warn", "decided by me", 3, "things")


def test_an_async_function(tmp_path: Path) -> None:
    spec = spec_for(tmp_path, "async_number")
    assert judge(spec, execute(spec)).value == 12.5


def test_an_exception_is_a_failure_with_its_type_not_its_message(tmp_path: Path) -> None:
    spec = spec_for(tmp_path, "boom")
    outcome = execute(spec)
    assert isinstance(outcome, Failure)
    assert outcome.summary == "function raised ValueError"
    assert "hunter2" not in outcome.summary


@pytest.mark.parametrize("function", ["returns_none", "returns_bool", "bad_level"])
def test_wrong_returns_are_failures_or_unknown(tmp_path: Path, function: str) -> None:
    spec = spec_for(tmp_path, function)
    assert judge(spec, execute(spec)).level == "unknown"


def test_config_errors(tmp_path: Path) -> None:
    for function in ("support_checks:nope", "no_such_module:f", "nocolon"):
        text = f'[[check]]\nid = "p"\ntype = "python"\nfunction = "{function}"\n'
        with pytest.raises(ConfigError):
            parse(text, tmp_path)


def test_secret_never_reaches_a_failure_summary_or_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    text = """
[[check]]
id = "p"
type = "python"
function = "support_checks:boom"
key_env = "MY_KEY"
"""
    w = make_world(text, tmp_path, env={"MY_KEY": "hunter2"})
    caplog.set_level(logging.DEBUG, logger="anystatus")
    w.sched.tick()
    # run the real function in this thread, as a worker would, and feed the outcome in
    w.launcher.complete("p", execute(w.config.checks[0]), w.clock)
    w.sched.tick()
    assert "hunter2" not in w.snapshot.json_bytes().decode()
    assert w.result("p")["summary"] == "function raised ValueError"
    assert "hunter2" not in caplog.text
    assert "***" in caplog.text  # the debug detail was logged, masked


def test_masking_and_exception_descriptions() -> None:
    assert (
        mask("login failed for postgresql://u:hunter2@h/db", []) == "login failed for postgresql://u:***@h/db"
    )
    assert mask("token abc123 used", ["abc123"]) == "token *** used"
    assert describe_exception(ConnectionRefusedError("host=db password=hunter2")) == "connection refused"
    assert describe_exception(RuntimeError("password=hunter2")) == "error: RuntimeError"
    assert "hunter2" not in describe_exception(OSError("password=hunter2"))
    assert Observation(level="ok")  # the public type is importable from the package root
