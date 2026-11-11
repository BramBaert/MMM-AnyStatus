"""Turning exceptions into failure summaries that can't leak a secret, and masking secrets in log text."""

from __future__ import annotations

import re
import socket
from collections.abc import Iterable

_URL_PASSWORD = re.compile(r"(://[^/\s:@]*:)[^@\s/]+@")


def mask(text: str, secrets: Iterable[str]) -> str:
    """Replace known secrets, and any password inside a URL, with ***."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return _URL_PASSWORD.sub(r"\1***@", text)


def describe_exception(exc: BaseException) -> str:
    """A short summary for a failed check, built from the exception's type, never from its message
    (message text can contain connection strings or passwords)."""
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused"
    if isinstance(exc, TimeoutError | socket.timeout):
        return "timed out"
    if isinstance(exc, socket.gaierror):
        return "host name not found"
    if isinstance(exc, ConnectionError):
        return "connection lost"
    name = type(exc).__name__
    # Database drivers put an error number first (pymysql: OperationalError(2003, ...)): the number is safe.
    if exc.args and isinstance(exc.args[0], int) and not isinstance(exc.args[0], bool):
        return f"{name} {exc.args[0]}"
    return f"error: {name}"
