"""`tls_expiry`: days until the certificate a server presents expires (negative when it already has).

The certificate is checked like a browser would: trusted by the system's CAs and valid for the name asked for. One that
isn't is `crit` (a browser refuses it), with a short reason. Not reaching the server at all is a failure (`unknown`).
"""

from __future__ import annotations

import socket
import ssl
from datetime import UTC, datetime, timedelta

from anystatus.checks.base import Check, register
from anystatus.errors import describe_exception
from anystatus.model import CheckFailure, Observation, Reading
from anystatus.table import Table

# OpenSSL's X509_V_ERR_* numbers that get their own wording
_NOT_YET_VALID = 9
_EXPIRED = 10
_HOSTNAME_MISMATCH = (62, 64)  # name, IP address


def _now() -> datetime:
    return datetime.now(UTC)


def trusted_context() -> ssl.SSLContext:
    """How the certificate is judged: the system's trusted CAs, and the name must match. Tests replace this."""
    return ssl.create_default_context()


def _tlv(data: bytes, pos: int) -> tuple[int, int, int]:
    """One DER element at `pos`: (tag, start of its content, end of its content)."""
    tag = data[pos]
    length = data[pos + 1]
    pos += 2
    if length & 0x80:
        count = length & 0x7F
        length = int.from_bytes(data[pos : pos + count], "big")
        pos += count
    if pos + length > len(data):
        raise ValueError("truncated certificate")
    return tag, pos, pos + length


def not_after_from_der(der: bytes) -> datetime:
    """The `notAfter` of a DER certificate. The ssl module only decodes a certificate it has verified, and an expired
    one never is, so this reads just that one field. ValueError when the certificate doesn't look as expected."""
    try:
        _, content, _ = _tlv(der, 0)  # Certificate
        _, pos, _ = _tlv(der, content)  # TBSCertificate
        tag, _, end = _tlv(der, pos)
        if tag == 0xA0:  # the optional version
            pos = end
        for _skip in range(3):  # serial number, signature algorithm, issuer
            _, _, pos = _tlv(der, pos)
        _, validity, _ = _tlv(der, pos)
        _, _, after_not_before = _tlv(der, validity)
        tag, start, stop = _tlv(der, after_not_before)
        text = der[start:stop].decode("ascii")
        if tag == 0x17:  # UTCTime YYMMDDHHMMSSZ
            year = int(text[:2])
            text = ("19" if year >= 50 else "20") + text
        elif tag != 0x18:  # GeneralizedTime YYYYMMDDHHMMSSZ
            raise ValueError("no time where notAfter should be")
        return datetime.strptime(text, "%Y%m%d%H%M%SZ").replace(tzinfo=UTC)
    except (IndexError, UnicodeDecodeError) as exc:
        raise ValueError("unreadable certificate") from exc


def days_between(moment: datetime, now: datetime) -> float:
    return (moment - now) / timedelta(days=1)


@register
class TlsExpiry(Check):
    type_name = "tls_expiry"
    worse = "below"
    default_limits = (21.0, 7.0)
    placeholders = frozenset({"host"})

    def __init__(self, host: str, port: int, servername: str) -> None:
        self.host = host
        self.port = port
        self.servername = servername

    @classmethod
    def from_config(cls, table: Table) -> TlsExpiry:
        host = table.req_str("host")
        port = table.integer("port", 443, minimum=1, maximum=65535)
        servername = table.opt_str("servername", host) or host
        return cls(host, port, servername)

    def _handshake(self, context: ssl.SSLContext, timeout: float) -> ssl.SSLSocket:
        raw = socket.create_connection((self.host, self.port), timeout=timeout)
        try:
            return context.wrap_socket(raw, server_hostname=self.servername)
        except BaseException:
            raw.close()
            raise

    def run(self, timeout: float) -> Observation | Reading:
        try:
            with self._handshake(trusted_context(), timeout) as tls:
                cert = tls.getpeercert()
        except ssl.SSLCertVerificationError as exc:
            return self.refused(exc, timeout)
        except ssl.SSLError as exc:
            raise CheckFailure("TLS handshake failed", detail=f"{type(exc).__name__}: {exc}") from exc
        except OSError as exc:
            raise CheckFailure(describe_exception(exc), detail=f"{type(exc).__name__}: {exc}") from exc
        try:
            not_after = datetime.fromtimestamp(ssl.cert_time_to_seconds(str((cert or {})["notAfter"])), UTC)
        except (KeyError, ValueError) as exc:
            raise CheckFailure("the certificate has no readable expiry date") from exc
        days = days_between(not_after, _now())
        return self.reading(days)

    def reading(self, days: float) -> Reading:
        value = round(days, 1)
        text = f"expires in {value:g} days" if value >= 0 else f"expired {-value:g} days ago"
        return Reading(value=value, unit="d", text=text, fields={"host": self.host})

    def refused(self, exc: ssl.SSLCertVerificationError, timeout: float) -> Observation:
        """The certificate was refused: `crit`, with a reason that fits in a few words."""
        code = getattr(exc, "verify_code", None)
        if code == _EXPIRED:
            return self.expired(timeout)
        if code == _NOT_YET_VALID:
            return Observation(level="crit", summary="certificate not valid yet")
        if code in _HOSTNAME_MISMATCH:
            return Observation(level="crit", summary=f"not valid for {self.servername}")
        reason = str(getattr(exc, "verify_message", "") or "").strip()
        return Observation(level="crit", summary=f"not trusted ({reason})" if reason else "not trusted")

    def expired(self, timeout: float) -> Observation:
        """An expired certificate is never decoded by the ssl module, so ask again without verifying, to say how long ago."""
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        try:
            with self._handshake(context, timeout) as tls:
                der = tls.getpeercert(binary_form=True)
            if der is None:
                raise ValueError("no certificate")
            days = days_between(not_after_from_der(der), _now())
        except (OSError, ValueError):
            return Observation(level="crit", summary="certificate expired")
        return Observation(
            level="crit", summary=f"expired {abs(days):.1f} days ago", value=round(days, 1), unit="d"
        )
