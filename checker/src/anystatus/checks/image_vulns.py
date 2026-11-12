"""`image_vulns`: the vulnerabilities in the images of the running containers, found by Trivy, sorted by what the user
can do about them, and shown on a schedule.

Each image of a running container is scanned once (`trivy image --format json --quiet --scanners vuln <image>`, a
list, no shell). Every vulnerability gets a **class**:

- *update*: a newer build of the image's own tag has the fix: pull it.
- *rebuild*: the image is built locally and rebuilding fixes it: its base has a newer build with the fix, or the
  package came in with the image's own layers.
- *waiting*: a fixed version exists, but no build of this tag (or of the base's tag) has it yet.
- *no fix*: no fixed version exists.

Telling *update* from *waiting* needs the newer build: only an image whose tag (or whose base's tag) has a newer
build in the registry (one manifest HEAD) gets a second scan, of that build straight from the registry
(`--image-src remote`). Whether a package came from the base is Trivy's `Layer.DiffID` against the base's layers
(images.py finds the base).

Per class and severity, the config's **schedule** gives a warn and a crit time, counted per CVE and image from when
it was first found (kept in the memory file). A severity a class doesn't list takes the line of the nearest listed
severity below it (LOW < MEDIUM < HIGH < CRITICAL); below the lowest listed one it doesn't count. UNKNOWN (not rated)
is outside that ladder: it counts only when listed itself, and passes nothing on. The card's level is the worst over
all CVEs; the full list, and the schedule as it applies, go on the checker's page `/vulns`.
"""

from __future__ import annotations

import html
import json
import logging
import os
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from anystatus.checks.base import Check, CheckMemory, register
from anystatus.checks.docker_api import (
    DockerClient,
    Endpoint,
    RunningImage,
    docker_endpoint,
    image_info,
    running_images,
)
from anystatus.checks.exec_cmd import run_process
from anystatus.checks.images import ImageStore, LocalImage, display_name, rebuilt, registry_credentials, safe_parse
from anystatus.checks.registry import ImageRef, RegistryClient, RegistryError
from anystatus.errors import mask
from anystatus.judge import lasted
from anystatus.model import CheckFailure, Observation
from anystatus.table import Table
from anystatus.timeutil import format_time, format_wait, parse_time, parse_wait

if TYPE_CHECKING:
    from anystatus.config import CheckSpec

log = logging.getLogger("anystatus")

SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN")
LADDER = ("LOW", "MEDIUM", "HIGH", "CRITICAL")  # lowest first; UNKNOWN (Trivy: not rated) stands outside it
CLASSES = ("update", "rebuild", "waiting", "no_fix")
CLASS_NAMES = {"update": "update", "rebuild": "rebuild", "waiting": "waiting", "no_fix": "no fix"}
CARD_TEXT = {
    "update": "fixed, please update image",
    "rebuild": "fixed, please rebuild image",
    "waiting": "waiting for upstream fix",
    "no_fix": "no fix exists",
}
# Without a schedule: what image_vulns did before classes existed. A fixed CRITICAL is red at once, a fixed HIGH
# amber, whatever can apply the fix; vulnerabilities without a fix don't count.
_AT_ONCE, _NEVER = 0.0, float("inf")
DEFAULT_SCHEDULE: dict[str, dict[str, tuple[float, float]]] = {
    klass: {"CRITICAL": (_AT_ONCE, _AT_ONCE), "HIGH": (_AT_ONCE, _NEVER)} for klass in ("update", "rebuild", "waiting")
}
MAX_OUTPUT = 32 * 1024 * 1024  # bytes of Trivy's JSON kept per image
_LEVEL_RANK = {"ok": 0, "warn": 1, "crit": 2}


def _now() -> datetime:
    return datetime.now(UTC)


Times = tuple[float, float]  # (warn, crit) in seconds; inf is "never"


@dataclass(frozen=True)
class Applied:
    """One severity's line in a class, as it applies: its own, or inherited from the nearest listed one below it."""

    times: Times
    source: str  # the severity whose line it is

    @property
    def counts(self) -> bool:
        """False for `{ warn = "never", crit = "never" }`: "don't care", written out."""
        return not all(t == float("inf") for t in self.times)


def apply_ladder(listed: dict[str, Times]) -> dict[str, Applied]:
    """A class's lines as written -> the line each severity follows. A severity that isn't listed takes the line of
    the nearest listed one below it on LOW < MEDIUM < HIGH < CRITICAL; one below the lowest listed has none (doesn't
    count). UNKNOWN only follows its own line, and never passes it on."""
    applied: dict[str, Applied] = {}
    below: str | None = None
    for severity in LADDER:
        if severity in listed:
            below = severity
        if below is not None:
            applied[severity] = Applied(listed[below], below)
    if "UNKNOWN" in listed:
        applied["UNKNOWN"] = Applied(listed["UNKNOWN"], "UNKNOWN")
    return applied


@dataclass(frozen=True)
class Vuln:
    id: str
    package: str
    severity: str
    installed: str = ""
    fixed: str = ""  # "" when no fixed version exists
    layer: str = ""  # the DiffID of the layer the package came with ("" when Trivy doesn't say)
    status: str = ""  # Trivy's Status: fixed, affected, will_not_fix, fix_deferred, end_of_life, ...
    url: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return self.id, self.package


def parse_trivy(text: str) -> list[Vuln]:
    """Every vulnerability in Trivy's JSON (Results[].Vulnerabilities[]), each (CVE, package, version) once."""
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("not a Trivy report")
    results = data.get("Results")
    found: dict[tuple[str, str, str], Vuln] = {}
    for result in results if isinstance(results, list) else []:
        vulns = result.get("Vulnerabilities") if isinstance(result, dict) else None
        for item in vulns if isinstance(vulns, list) else []:
            if not isinstance(item, dict) or not isinstance(item.get("VulnerabilityID"), str):
                continue
            fixed = item.get("FixedVersion")
            layer = item.get("Layer")
            url = item.get("PrimaryURL")
            vuln = Vuln(
                id=item["VulnerabilityID"],
                package=str(item.get("PkgName", "")),
                severity=str(item.get("Severity", "UNKNOWN")).upper(),
                installed=str(item.get("InstalledVersion", "")),
                fixed=fixed.strip() if isinstance(fixed, str) else "",
                layer=str(layer.get("DiffID", "")) if isinstance(layer, dict) else "",
                status=str(item.get("Status", "")),
                url=url if isinstance(url, str) and url.startswith(("https://", "http://")) else "",
            )
            found.setdefault((vuln.id, vuln.package, vuln.installed), vuln)
    return list(found.values())


@dataclass(frozen=True)
class Finding:
    """One vulnerability in one image, classified and judged: a row of the /vulns page."""

    image: str  # the short name the card uses ("mariadb")
    reference: str  # what was scanned ("mariadb:11.4")
    vuln: Vuln
    klass: str
    first_found: datetime
    level: str  # "ok" (not due yet, or not in the schedule), "warn", "crit"
    scheduled: bool  # the schedule counts this class and severity (its own line or an inherited one, not never/never)


@dataclass(frozen=True)
class Report:
    ts: datetime
    findings: tuple[Finding, ...] = ()
    notes: tuple[str, ...] = ()
    error: str = ""  # the run failed: why


class _Unchecked(Exception):
    """A newer build could not be checked (registry or scan failed): its vulnerabilities count as waiting."""


@dataclass
class _Run:
    """What one run keeps while it goes through the images."""

    store: ImageStore
    registry: RegistryClient
    left: Callable[[], float]
    newer_scans: dict[str, set[tuple[str, str]] | _Unchecked | None] = field(default_factory=dict)
    unchecked: list[str] = field(default_factory=list)


@register
class ImageVulns(Check):
    type_name = "image_vulns"

    def __init__(
        self,
        endpoint: Endpoint,
        trivy: list[str],
        cache_dir: str | None,
        schedule: dict[str, dict[str, Times]],
        env_names: list[str],
        credentials: dict[str, str],
    ) -> None:
        self.client = DockerClient(endpoint)
        self.trivy = trivy
        self.cache_dir = cache_dir
        self.listed = schedule  # as written in the config
        self.schedule = {klass: apply_ladder(schedule.get(klass, {})) for klass in CLASSES}  # as it applies
        self.env_names = env_names
        self.credentials = credentials
        self.now: Callable[[], datetime] = _now
        self.memory: CheckMemory | None = None
        self.found: dict[str, datetime] = {}  # "reference|CVE" -> first found
        self.report: Report | None = None  # the last run's full list, for the /vulns page

    @classmethod
    def from_config(cls, table: Table) -> ImageVulns:
        for key in ("warn", "crit", "worse"):
            if table.has(key):
                table.error(f"image_vulns decides its own level from its 'schedule': '{key}' is not used")
        if table.has("severities"):
            table.raw("severities")
            table.error("'severities' has been replaced by 'schedule': per class, the severities that count and when")
        endpoint = docker_endpoint(table)
        trivy: list[str] = ["trivy"]
        if table.has("trivy"):
            given = table.raw("trivy")
            if isinstance(given, str) and given.strip():
                trivy = [given]
            elif isinstance(given, list) and given and all(isinstance(a, str) and a for a in given):
                trivy = list(given)
            else:
                table.error('\'trivy\' must be a command or path, or a list such as ["docker", "run", ...]')
        if shutil.which(trivy[0]) is None:
            table.error(f"'trivy': {trivy[0]!r} was not found (install Trivy, or give its path in 'trivy')")
        cache_dir = table.opt_str("cache_dir")
        schedule = read_schedule(table)
        env_names = table.str_list("env") or []
        if not table.has("timeout"):
            table.warning('image_vulns: the default timeout of 10 s is too short for Trivy: set timeout = "15m"')
        return cls(endpoint, trivy, cache_dir, schedule, env_names, registry_credentials(table))

    # memory

    def start(self, memory: CheckMemory) -> None:
        self.memory = memory
        stored = (memory.data or {}).get("found")
        if isinstance(stored, dict):
            for key, text in stored.items():
                try:
                    self.found[str(key)] = parse_time(str(text))
                except ValueError:
                    continue

    def _remember(self, found: dict[str, datetime]) -> None:
        self.found = found
        if self.memory is not None:
            self.memory.save({"found": {key: format_time(moment) for key, moment in sorted(found.items())}})

    # the run

    def _environment(self) -> dict[str, str]:
        """Only the listed variables that are set (for example HOME, DOCKER_HOST): nothing else, PATH included."""
        return {name: os.environ[name] for name in self.env_names if name in os.environ}

    def _target(self, image: RunningImage, left: float) -> str:
        if image.reference:
            return image.reference
        tags, _ = image_info(self.client, image.image_id, left)
        return tags[0] if tags else image.image_id

    def run(self, timeout: float) -> Observation:
        deadline = time.monotonic() + timeout

        def left() -> float:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CheckFailure(f"timed out after {timeout:g} s")
            return remaining

        now = self.now()
        try:
            return self._run(now, left, deadline)
        except CheckFailure as exc:
            self.report = Report(now, error=exc.summary)
            raise

    def _run(self, now: datetime, left: Callable[[], float], deadline: float) -> Observation:
        images = running_images(self.client, left())
        if not images:
            self.report = Report(now)
            self._remember({})
            return Observation(level="ok", summary="no running containers", value=0)
        run = _Run(ImageStore(self.client, left), RegistryClient(self.credentials, deadline), left)
        scanned: list[tuple[str, list[Vuln], dict[tuple[str, str], str]]] = []  # (reference, vulns, class per key)
        failed: list[str] = []
        timed_out = False
        first_failure = "trivy failed"
        for index, image in enumerate(images):
            target = image.reference or image.image_id[:19]
            try:
                target = self._target(image, left())
                vulns = self._scan(target, left())
                scanned.append((target, vulns, self._classify(image, target, vulns, run)))
            except CheckFailure as exc:
                failed.append(target)  # what was scanned (and its dates are kept under), as far as it got
                first_failure = exc.summary
                if exc.summary.startswith("timed out"):
                    timed_out = True
                    failed.extend(i.reference or i.image_id[:19] for i in images[index + 1 :])
                    break
        if not scanned:
            raise CheckFailure(first_failure if timed_out or len(images) == 1 else f"trivy failed: {first_failure}")

        # First found, per CVE and image. A CVE that is gone from a scanned image is forgotten (if it comes back, it
        # is new); the dates of an image that couldn't be scanned this time are kept.
        found: dict[str, datetime] = {}
        for reference, vulns, _ in scanned:
            for vuln in vulns:
                key = f"{reference}|{vuln.id}"
                found[key] = self.found.get(key, now)
        for key, moment in self.found.items():
            if key.rsplit("|", 1)[0] in failed:
                found.setdefault(key, moment)
        self._remember(found)

        findings: list[Finding] = []
        for reference, vulns, classes in scanned:
            for vuln in vulns:
                klass = classes[vuln.key]
                first = found[f"{reference}|{vuln.id}"]
                line = self.schedule[klass].get(vuln.severity)
                level = "ok"
                if line is not None:
                    warn, crit = line.times
                    level = "crit" if lasted(first, crit, now) else "warn" if lasted(first, warn, now) else "ok"
                counts = line is not None and line.counts
                findings.append(Finding(_short(reference), reference, vuln, klass, first, level, counts))

        notes: list[str] = []
        if run.unchecked:
            notes.append(f"newer build not checked: {', '.join(sorted(set(run.unchecked)))}")
        if failed:
            notes.append(f"{len(failed)} not scanned ({'timed out' if timed_out else 'trivy failed'})")
        self.report = Report(now, tuple(sorted(findings, key=_row_order)), tuple(notes))
        return self._card(findings, len(scanned), notes)

    def _card(self, findings: list[Finding], images: int, notes: list[str]) -> Observation:
        due = [f for f in findings if f.level != "ok"]
        value = len({(f.reference, f.vuln.id) for f in due})
        if not due:
            noun = "image" if images == 1 else "images"
            text = f"nothing due in {images} {noun}"
        else:
            level = max((f.level for f in due), key=_LEVEL_RANK.__getitem__)
            top = [f for f in due if f.level == level]
            severity = min((f.vuln.severity for f in top), key=_severity_rank)
            groups: dict[tuple[str, str], set[str]] = {}
            for f in top:
                if f.vuln.severity == severity:
                    groups.setdefault((f.image, f.klass), set()).add(f.vuln.id)
            (image, klass), cves = min(groups.items(), key=lambda g: (-len(g[1]), CLASSES.index(g[0][1]), g[0][0]))
            text = f"{image}: {len(cves)} {severity.lower()}, {CARD_TEXT[klass]}"
        # A blind spot belongs on the card; "newer build not checked" goes to the page only (the card stays short).
        text += "".join(f"; {n}" for n in notes if "not scanned" in n)
        return Observation(level="ok" if not due else level, summary=text, value=value)

    def _classify(self, image: RunningImage, target: str, vulns: list[Vuln], run: _Run) -> dict[tuple[str, str], str]:
        """The class of each (CVE, package) of one image."""
        classes = {v.key: "no_fix" for v in vulns if not v.fixed}
        fixable = [v for v in vulns if v.fixed]
        if not fixable:
            return classes
        ref = safe_parse(target)
        if ref is None or ref.digest is not None or ref.tag is None:
            # Started by id or pinned by digest: there is no "newer build of the same tag" to update to.
            classes.update({v.key: "waiting" for v in fixable})
            return classes
        local = run.store.inspect(image.image_id)
        if local.from_registry(ref):
            newer = self._newer(ref, local, run, _short(target))
            classes.update({v.key: "update" if newer is not None and v.key not in newer else "waiting" for v in fixable})
            return classes
        # Built here: what came with the base waits for (or is fixed by) a newer base; the rest is in the user's hands.
        base = run.store.find_base(local)
        base_layers = set(base.image.layers) if base is not None else set()
        from_base = [v for v in fixable if v.layer and v.layer in base_layers]
        classes.update({v.key: "rebuild" for v in fixable if v not in from_base})
        if from_base and base is not None:
            newer = self._newer(base.ref, base.image, run, _short(target))
            classes.update({v.key: "rebuild" if newer is not None and v.key not in newer else "waiting" for v in from_base})
        return classes

    def _newer(self, ref: ImageRef, local: LocalImage, run: _Run, shown: str) -> set[tuple[str, str]] | None:
        """The (CVE, package) pairs in the newer build of `ref`, None when the registry has no newer build. Each tag
        is asked about and scanned once per run. A registry or scan that fails is noted (the image's fixable
        vulnerabilities then count as waiting)."""
        name = display_name(ref)
        if name not in run.newer_scans:
            try:
                if rebuilt(run.registry, ref, local):
                    run.newer_scans[name] = {v.key for v in self._scan(name, run.left(), remote=True)}
                else:
                    run.newer_scans[name] = None
            except (RegistryError, CheckFailure) as exc:
                log.info("image_vulns: newer build of %s not checked: %s", name, getattr(exc, "summary", type(exc).__name__))
                run.newer_scans[name] = _Unchecked()
        result = run.newer_scans[name]
        if isinstance(result, _Unchecked):
            run.unchecked.append(shown)
            return None
        return result

    def _scan(self, image: str, timeout: float, remote: bool = False) -> list[Vuln]:
        resolved = shutil.which(self.trivy[0])
        if resolved is None:
            raise CheckFailure("trivy not found")
        command = [resolved, *self.trivy[1:], "image"]
        if remote:
            command += ["--image-src", "remote"]  # the newer build in the registry, not the local one
        command += ["--format", "json", "--quiet", "--scanners", "vuln"]
        if self.cache_dir:
            command += ["--cache-dir", self.cache_dir]
        command.append(image)
        env = self._environment()
        secrets = [v for v in env.values() if v]
        done = run_process(command, env, timeout, MAX_OUTPUT, secrets)
        if done.stderr:
            log.debug("image_vulns: trivy stderr: %s", done.stderr)
        if done.code != 0:
            raise CheckFailure(f"trivy exit code {done.code}", detail=mask(f"stderr: {done.stderr}", secrets))
        if done.truncated:
            raise CheckFailure("trivy's output is too large")
        try:
            return parse_trivy(done.stdout)
        except ValueError as exc:
            raise CheckFailure("trivy's output is not JSON") from exc


def read_schedule(table: Table) -> dict[str, dict[str, tuple[float, float]]]:
    """`[check.schedule.<class>]` tables of `SEVERITY = { warn = "...", crit = "..." }`, as written. Both times are
    written out ("never" skips a level). Which line a severity that isn't listed follows: apply_ladder. Without
    `schedule`: DEFAULT_SCHEDULE."""
    raw = table.raw("schedule")
    if raw is None:
        return DEFAULT_SCHEDULE
    if not isinstance(raw, dict):
        table.error("'schedule' must be a table: [check.schedule.update], [check.schedule.rebuild], ...")
        return DEFAULT_SCHEDULE
    schedule: dict[str, dict[str, tuple[float, float]]] = {}
    for klass, severities in raw.items():
        if klass not in CLASSES:
            table.error(f"'schedule': unknown class '{klass}' (use {', '.join(CLASSES)})")
            continue
        if not isinstance(severities, dict):
            table.error(f'\'schedule.{klass}\' must be a table, such as CRITICAL = {{ warn = "0d", crit = "3d" }}')
            continue
        entries: dict[str, tuple[float, float]] = {}
        for severity, times in severities.items():
            where = f"'schedule.{klass}.{severity}'"
            if severity not in SEVERITIES:
                table.error(f"{where}: unknown severity (use {', '.join(SEVERITIES)}, in capitals)")
                continue
            if not isinstance(times, dict) or set(times) != {"warn", "crit"}:
                table.error(f'{where} must give both warn and crit, such as {{ warn = "0d", crit = "never" }}: write "never" to skip a level')
                continue
            try:
                entries[severity] = (parse_wait(times["warn"]), parse_wait(times["crit"]))
            except ValueError as exc:
                table.error(f"{where}: {exc}")
        schedule[klass] = entries
    return schedule


def _short(reference: str) -> str:
    """The image name for a summary: the last path part without the tag (`ghcr.io/org/app:1.2` -> `app`)."""
    name = reference.split("@", 1)[0].rsplit("/", 1)[-1]
    return name.split(":", 1)[0] or reference


def _severity_rank(severity: str) -> int:
    return SEVERITIES.index(severity) if severity in SEVERITIES else len(SEVERITIES)


def _row_order(f: Finding) -> tuple[Any, ...]:
    return (-_LEVEL_RANK[f.level], not f.scheduled, _severity_rank(f.vuln.severity), f.image, f.vuln.id, f.vuln.package)


# ---------------------------------------------------------------------------------------------------- the /vulns page

_DASH = chr(0x2013)  # an en dash: an empty cell
_PAGE_STYLE = """
:root { --bg: #fff; --fg: #1d1d1f; --muted: #6e6e73; --line: #e3e3e6; --crit: #c62828; --warn: #a15c00; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #161618; --fg: #ececf0; --muted: #9a9aa2; --line: #2e2e33; --crit: #ff6b6b; --warn: #f0b44c; }
}
body { background: var(--bg); color: var(--fg); font: 14px/1.4 system-ui, sans-serif; margin: 16px; }
h1 { font-size: 20px; } h2 { font-size: 16px; margin-top: 28px; }
p { color: var(--muted); margin: 4px 0; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; margin-top: 8px; }
th, td { text-align: left; padding: 4px 10px 4px 0; border-bottom: 1px solid var(--line); white-space: nowrap; }
th { color: var(--muted); font-weight: 600; }
td.crit { color: var(--crit); font-weight: 600; } td.warn { color: var(--warn); font-weight: 600; }
td.ok { color: var(--muted); }
a { color: inherit; }
"""


_PAGE_SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN")


def _schedule_cell(line: Applied | None, severity: str) -> str:
    if line is None:
        return f"{_DASH} not counted"
    if not line.counts:
        text = "never (not counted)"
    else:
        warn, crit = (("at once" if t == 0 else format_wait(t)) for t in line.times)
        text = f"amber {warn}, red {crit}"
    return text if line.source == severity else f"as {line.source}: {text}"


def _schedule_table(check: ImageVulns) -> str:
    """The schedule as it applies, per class and severity, inherited lines marked ("as MEDIUM: ..."), so the rule
    that fills the gaps is never hidden. Times count from when a CVE was first found in an image."""
    e = html.escape
    rows = [
        "<p>Schedule as it applies (from first found; a severity that isn't listed follows the nearest listed one "
        "below it, UNKNOWN only its own line):</p>",
        "<div class=scroll><table><tr><th>Class</th>" + "".join(f"<th>{s.lower()}</th>" for s in _PAGE_SEVERITIES) + "</tr>",
    ]
    for klass in CLASSES:
        lines = check.schedule[klass]
        cells = "".join(f"<td>{e(_schedule_cell(lines.get(s), s))}</td>" for s in _PAGE_SEVERITIES)
        rows.append(f"<tr><td>{e(CLASS_NAMES[klass])}</td>{cells}</tr>")
    rows.append("</table></div>")
    return "".join(rows)


def render_vulns_page(source: str, specs: Sequence[CheckSpec]) -> bytes:
    """The full list per image_vulns check: image, CVE, package, installed and fixed version, class, first found."""
    e = html.escape
    parts = [
        "<!doctype html><html lang=en><head><meta charset=utf-8>",
        '<meta name=viewport content="width=device-width, initial-scale=1">',
        f"<title>{e(source)}: vulnerabilities</title><style>{_PAGE_STYLE}</style></head><body>",
        f"<h1>{e(source)}: vulnerabilities</h1>",
        f"<p>Level: what the schedule makes of it now ({_DASH} means its class and severity aren't in the schedule). First found: per CVE and image.</p>",
    ]
    for spec in specs:
        check = spec.check
        if not isinstance(check, ImageVulns):
            continue
        report = check.report
        parts.append(f"<h2>{e(spec.label)} <small>({e(spec.id)})</small></h2>")
        parts.append(_schedule_table(check))
        if report is None:
            parts.append("<p>No run yet.</p>")
            continue
        parts.append(f"<p>Scanned {e(format_time(report.ts))}.</p>")
        if report.error:
            parts.append(f"<p>The run failed: {e(report.error)}</p>")
            continue
        for note in report.notes:
            parts.append(f"<p>{e(note)}</p>")
        if not report.findings:
            parts.append("<p>No vulnerabilities found.</p>")
            continue
        parts.append(
            "<div class=scroll><table><tr><th>Level</th><th>Severity</th><th>Image</th><th>CVE</th><th>Package</th>"
            "<th>Installed</th><th>Fixed in</th><th>Class</th><th>First found</th></tr>"
        )
        for f in report.findings:
            v = f.vuln
            cve = f'<a href="{e(v.url)}">{e(v.id)}</a>' if v.url else e(v.id)
            klass = CLASS_NAMES[f.klass]
            if f.klass == "no_fix" and v.status and v.status != "affected":
                klass += f" ({v.status.replace('_', ' ')})"
            level = f.level if f.scheduled else _DASH
            parts.append(
                f"<tr><td class={f.level}>{e(level)}</td><td>{e(v.severity.lower())}</td><td>{e(f.reference)}</td>"
                f"<td>{cve}</td><td>{e(v.package)}</td><td>{e(v.installed)}</td><td>{e(v.fixed or _DASH)}</td>"
                f"<td>{e(klass)}</td><td>{e(format_time(f.first_found))}</td></tr>"
            )
        parts.append("</table></div>")
    parts.append("</body></html>")
    return "".join(parts).encode("utf-8")
