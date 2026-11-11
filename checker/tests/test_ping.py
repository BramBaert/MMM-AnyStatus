"""ping output parsing, with captured outputs (Linux iputils, BusyBox, Windows English) and Dutch Windows output
written after the format of the English one (not captured from a machine: see the report)."""

from __future__ import annotations

import pytest

from anystatus.checks.ping import Ping, parse_ping_output
from anystatus.model import CheckFailure, Observation, Reading

IPUTILS = """PING localhost (127.0.0.1) 56(84) bytes of data.
64 bytes from localhost (127.0.0.1): icmp_seq=1 ttl=64 time=0.045 ms
64 bytes from localhost (127.0.0.1): icmp_seq=2 ttl=64 time=0.052 ms
64 bytes from localhost (127.0.0.1): icmp_seq=3 ttl=64 time=0.049 ms

--- localhost ping statistics ---
3 packets transmitted, 3 received, 0% packet loss, time 2043ms
rtt min/avg/max/mdev = 0.045/0.049/0.052/0.003 ms
"""

IPUTILS_PARTIAL = """PING 192.0.2.1 (192.0.2.1) 56(84) bytes of data.
64 bytes from 192.0.2.1: icmp_seq=1 ttl=57 time=12.1 ms
64 bytes from 192.0.2.1: icmp_seq=3 ttl=57 time=14.3 ms

--- 192.0.2.1 ping statistics ---
3 packets transmitted, 2 received, 33.3333% packet loss, time 2003ms
rtt min/avg/max/mdev = 12.100/13.200/14.300/1.100 ms
"""

IPUTILS_NONE = """PING 192.0.2.1 (192.0.2.1) 56(84) bytes of data.

--- 192.0.2.1 ping statistics ---
3 packets transmitted, 0 received, 100% packet loss, time 2049ms
"""

IPUTILS_BAD_HOST = "ping: nonexistent.example: Name or service not known\n"

BUSYBOX = """PING localhost (127.0.0.1): 56 data bytes
64 bytes from 127.0.0.1: seq=0 ttl=64 time=0.077 ms
64 bytes from 127.0.0.1: seq=1 ttl=64 time=0.098 ms
64 bytes from 127.0.0.1: seq=2 ttl=64 time=0.123 ms

--- localhost ping statistics ---
3 packets transmitted, 3 packets received, 0% packet loss
round-trip min/avg/max = 0.077/0.099/0.123 ms
"""

BUSYBOX_NONE = """PING 192.0.2.1 (192.0.2.1): 56 data bytes

--- 192.0.2.1 ping statistics ---
3 packets transmitted, 0 packets received, 100% packet loss
"""

WINDOWS = """
Pinging example.org [192.0.2.7] with 32 bytes of data:
Reply from 192.0.2.7: bytes=32 time=12ms TTL=117
Reply from 192.0.2.7: bytes=32 time=14ms TTL=117
Reply from 192.0.2.7: bytes=32 time=13ms TTL=117

Ping statistics for 192.0.2.7:
    Packets: Sent = 3, Received = 3, Lost = 0 (0% loss),
Approximate round trip times in milli-seconds:
    Minimum = 12ms, Maximum = 14ms, Average = 13ms
"""

WINDOWS_LOCALHOST = """
Pinging localhost [::1] with 32 bytes of data:
Reply from ::1: time<1ms
Reply from ::1: time<1ms
Reply from ::1: time<1ms

Ping statistics for ::1:
    Packets: Sent = 3, Received = 3, Lost = 0 (0% loss),
Approximate round trip times in milli-seconds:
    Minimum = 0ms, Maximum = 0ms, Average = 0ms
"""

WINDOWS_TIMEOUT = """
Pinging 192.0.2.1 with 32 bytes of data:
Request timed out.
Request timed out.
Request timed out.

Ping statistics for 192.0.2.1:
    Packets: Sent = 3, Received = 0, Lost = 3 (100% loss),
"""

WINDOWS_UNREACHABLE = """
Pinging 192.0.2.1 with 32 bytes of data:
Reply from 192.0.2.254: Destination host unreachable.
Reply from 192.0.2.254: Destination host unreachable.
Reply from 192.0.2.254: Destination host unreachable.

Ping statistics for 192.0.2.1:
    Packets: Sent = 3, Received = 3, Lost = 0 (0% loss),
"""

WINDOWS_DUTCH = """
Pingen naar example.org [192.0.2.7] met 32 bytes aan gegevens:
Antwoord van 192.0.2.7: bytes=32 tijd=12ms TTL=117
Antwoord van 192.0.2.7: bytes=32 tijd<1 ms TTL=117
Antwoord van 192.0.2.7: bytes=32 tijd=14ms TTL=117

Ping-statistieken voor 192.0.2.7:
    Pakketten: verzonden = 3, ontvangen = 3, verloren = 0 (0% verlies),
Bij benadering in milliseconden:
    Minimum = 1ms, Maximum = 14ms, Gemiddeld = 9ms
"""

WINDOWS_DUTCH_TIMEOUT = """
Pingen naar 192.0.2.1 met 32 bytes aan gegevens:
Time-out bij opdracht.
Time-out bij opdracht.
Time-out bij opdracht.

Ping-statistieken voor 192.0.2.1:
    Pakketten: verzonden = 3, ontvangen = 0, verloren = 3 (100% verlies),
"""


def interpret(text: str, rc: int = 0, count: int = 3) -> Observation | Reading:
    return Ping("host.example", count).interpret(text, rc)


@pytest.mark.parametrize(
    ("text", "average"),
    [(IPUTILS, 0.0), (BUSYBOX, 0.1), (WINDOWS, 13.0), (WINDOWS_LOCALHOST, 1.0), (WINDOWS_DUTCH, 9.0)],
)
def test_all_answered(text: str, average: float) -> None:
    result = interpret(text)
    assert isinstance(result, Reading)
    assert result.unit == "ms"
    assert result.value == pytest.approx(average, abs=0.06)
    assert "answered" not in result.text
    assert result.fields["answered"] == result.fields["sent"] == "3"


def test_the_average_is_the_mean_of_the_replies() -> None:
    result = interpret(IPUTILS_PARTIAL, rc=0)
    assert isinstance(result, Reading)
    assert result.value == 13.2
    assert result.text == "2/3 answered, 13.2 ms"


@pytest.mark.parametrize(
    "text", [IPUTILS_NONE, BUSYBOX_NONE, WINDOWS_TIMEOUT, WINDOWS_UNREACHABLE, WINDOWS_DUTCH_TIMEOUT]
)
def test_no_answer_is_crit_not_a_failure(text: str) -> None:
    result = interpret(text, rc=1)
    assert result == Observation(level="crit", summary="no answer")


@pytest.mark.parametrize("text", [IPUTILS_BAD_HOST, "", "completely unrelated output\n", "\x00\xff garbage"])
def test_unparseable_output_is_a_failure(text: str) -> None:
    with pytest.raises(CheckFailure):
        interpret(text, rc=2)
    with pytest.raises(CheckFailure):
        interpret(text, rc=0)


def test_parse_counts_sent_from_the_statistics() -> None:
    assert parse_ping_output(IPUTILS_NONE).sent == 3
    assert parse_ping_output(WINDOWS_DUTCH_TIMEOUT).sent == 3
    assert parse_ping_output(BUSYBOX).times == [0.077, 0.098, 0.123]


def test_host_is_validated() -> None:
    from helpers import parse

    from anystatus.table import ConfigError

    for bad in ("-f", "a b", "x;rm"):
        with pytest.raises(ConfigError):
            parse(f'[[check]]\nid = "p"\ntype = "ping"\nhost = "{bad}"\n', __import__("pathlib").Path("."))


def test_command_line_stays_within_the_timeout() -> None:
    cmd = Ping("localhost", 3).command(10)
    assert cmd[0] == "ping" and cmd[-1] == "localhost"
