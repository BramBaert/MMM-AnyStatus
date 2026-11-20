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

A CVE in an image can be **accepted** (`accept`) through an end date: it is still scanned, classified and dated, but it
doesn't count, and /vulns shows why. The decision is made where a finding's level is: in `_run`.
"""

from __future__ import annotations

import contextlib
import html
import json
import logging
import os
import shutil
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
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
from anystatus.checks.images import ImageStore, display_name, is_ignored, rebuilt, registry_credentials, safe_parse
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
# One Trivy at a time in this process, for every image_vulns check: two that both find the database out of date
# download it into the same cache at once, and one of them crashes (SIGBUS in bbolt; Trivy 0.76.1, 2 of 2 tries).
# Another process sharing the cache is not covered.
_TRIVY_LOCK = threading.Lock()
_LEVEL_RANK = {"ok": 0, "warn": 1, "crit": 2}
MAX_NAMES = 3  # images named on the card (their entries are long); the rest is counted, every one is on /vulns


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
class Accept:
    """One `accept` entry: this CVE, in this image, is seen and not counted through the day `until` (UTC)."""

    cve: str
    image: str
    until: date
    why: str


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
    accepted: Accept | None = None  # the entry that holds it back for now (its level is then "ok")


@dataclass(frozen=True)
class Report:
    ts: datetime
    findings: tuple[Finding, ...] = ()
    notes: tuple[str, ...] = ()
    error: str = ""  # the run failed: why
    matched: frozenset[int] = frozenset()  # the `accept` entries (by position) that matched a finding, expired or not


class _Unchecked(Exception):
    """A newer build could not be checked (registry or scan failed): its vulnerabilities count as waiting."""


@dataclass
class _Run:
    """What one run keeps while it goes through the images."""

    store: ImageStore
    registry: RegistryClient
    left: Callable[[], float]
    newer_scans: dict[str, set[tuple[str, str]] | _Unchecked] = field(default_factory=dict)  # tag -> its registry build's CVEs
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
        ignore: list[str],
        accept: list[Accept],
        credentials: dict[str, str],
    ) -> None:
        self.client = DockerClient(endpoint)
        # Trivy reads the local images from the same Docker as the check, so one checker can scan several hosts
        self.docker_host = f"unix://{endpoint.unix_path}" if endpoint.unix_path else f"tcp://{endpoint.host}:{endpoint.port}"
        self.trivy = trivy
        self.cache_dir = cache_dir
        self.listed = schedule  # as written in the config
        self.schedule = {klass: apply_ladder(schedule.get(klass, {})) for klass in CLASSES}  # as it applies
        self.env_names = env_names
        self.ignore = {i.lower() for i in ignore}
        self.accept = accept
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
        ignore = table.str_list("ignore") or []
        accept = read_accept(table)
        if "DOCKER_HOST" in env_names:
            table.error("'env': DOCKER_HOST comes from the check's 'docker' key: remove it from 'env'")
        if not table.has("timeout"):
            table.warning('image_vulns: the default timeout of 10 s is too short for Trivy: set timeout = "15m"')
        return cls(endpoint, trivy, cache_dir, schedule, env_names, ignore, accept, registry_credentials(table))

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
        """Only the listed variables that are set (for example HOME): nothing else, PATH included."""
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
        scanned: list[tuple[RunningImage, str, list[Vuln], dict[tuple[str, str], str]]] = []  # (container's image, reference, vulns, class per key)
        failed: list[str] = []
        ignored = 0
        timed_out = False
        first_failure = "trivy failed"
        for index, image in enumerate(images):
            target = image.reference or image.image_id[:19]
            try:
                target = self._target(image, left())
                if is_ignored(self.ignore, image, safe_parse(target)):
                    ignored += 1
                    continue
                vulns = self._scan(target, left())
                scanned.append((image, target, vulns, self._classify(image, target, vulns, run)))
            except CheckFailure as exc:
                failed.append(target)  # what was scanned (and its dates are kept under), as far as it got
                first_failure = exc.summary
                if exc.summary.startswith("timed out"):
                    timed_out = True
                    failed.extend(
                        i.reference or i.image_id[:19] for i in images[index + 1 :] if not is_ignored(self.ignore, i, safe_parse(i.reference))
                    )
                    break
        if not scanned and ignored == len(images):
            self.report = Report(now)
            self._remember({})
            return self._card([], 0, [])
        if not scanned:
            raise CheckFailure(first_failure if timed_out or len(images) == 1 else f"trivy failed: {first_failure}")

        # First found, per CVE and image. A CVE that is gone from a scanned image is forgotten (if it comes back, it
        # is new); the dates of an image that couldn't be scanned this time are kept.
        found: dict[str, datetime] = {}
        for _, reference, vulns, _ in scanned:
            for vuln in vulns:
                key = f"{reference}|{vuln.id}"
                found[key] = self.found.get(key, now)
        for key, moment in self.found.items():
            if key.rsplit("|", 1)[0] in failed:
                found.setdefault(key, moment)
        self._remember(found)

        findings: list[Finding] = []
        matched: set[int] = set()
        for image, reference, vulns, classes in scanned:
            ref = safe_parse(reference)
            for vuln in vulns:
                klass = classes[vuln.key]
                first = found[f"{reference}|{vuln.id}"]
                line = self.schedule[klass].get(vuln.severity)
                level = "ok"
                if line is not None:
                    warn, crit = line.times
                    level = "crit" if lasted(first, crit, now) else "warn" if lasted(first, warn, now) else "ok"
                # The one place a finding's level is decided: an accepted one stays "ok" through its end date. The
                # first-found date above keeps running meanwhile, so it comes back with its real age.
                hits = [i for i, a in enumerate(self.accept) if a.cve.lower() == vuln.id.lower() and is_ignored({a.image.lower()}, image, ref)]
                matched.update(hits)
                accepted = next((self.accept[i] for i in hits if self.accept[i].until >= now.date()), None)
                if accepted is not None:
                    level = "ok"
                counts = line is not None and line.counts
                findings.append(Finding(_short(reference), reference, vuln, klass, first, level, counts, accepted))

        notes: list[str] = []
        if run.unchecked:
            notes.append(f"newer build not checked: {', '.join(sorted(set(run.unchecked)))}")
        if failed:
            notes.append(f"{len(failed)} not scanned ({'timed out' if timed_out else 'trivy failed'})")
        self.report = Report(now, tuple(sorted(findings, key=_row_order)), tuple(notes), matched=frozenset(matched))
        return self._card(findings, len(scanned), notes)

    def _card(self, findings: list[Finding], images: int, notes: list[str]) -> Observation:
        due = [f for f in findings if f.level != "ok"]
        value = len({(f.reference, f.vuln.id) for f in due})
        if not due:
            noun = "image" if images == 1 else "images"
            text = f"nothing due in {images} {noun}"
        else:
            level = max((f.level for f in due), key=_LEVEL_RANK.__getitem__)
            # One entry per image: its biggest group at its own worst level and severity. Worst image first.
            entries: list[tuple[tuple[int, int, int, int, str], str]] = []
            for reference in {f.reference for f in due}:
                mine = [f for f in due if f.reference == reference]
                worst = max((f.level for f in mine), key=_LEVEL_RANK.__getitem__)
                top = [f for f in mine if f.level == worst]
                severity = min((f.vuln.severity for f in top), key=_severity_rank)
                groups: dict[str, set[str]] = {}
                for f in top:
                    if f.vuln.severity == severity:
                        groups.setdefault(f.klass, set()).add(f.vuln.id)
                klass, cves = min(groups.items(), key=lambda g: (-len(g[1]), CLASSES.index(g[0])))
                image = mine[0].image
                order = (-_LEVEL_RANK[worst], _severity_rank(severity), -len(cves), CLASSES.index(klass), image)
                entries.append((order, f"{image}: {len(cves)} {severity.lower()}, {CARD_TEXT[klass]}"))
            entries.sort()
            shown = [entry for _, entry in entries[:MAX_NAMES]]
            if len(entries) > MAX_NAMES:
                rest = len(entries) - MAX_NAMES
                shown.append(f"+{rest} more image{'s' if rest > 1 else ''}")
            text = "\n".join(shown)
        # A blind spot belongs on the card; "newer build not checked" goes to the page only (the card stays short).
        text += "".join(f"\n{n}" for n in notes if "not scanned" in n)
        return Observation(level="ok" if not due else level, summary=text, value=value)

    def _classify(self, image: RunningImage, target: str, vulns: list[Vuln], run: _Run) -> dict[tuple[str, str], str]:
        """The class of each (CVE, package) of one image."""
        classes = {v.key: "no_fix" for v in vulns if not v.fixed}
        fixable = [v for v in vulns if v.fixed]
        if not fixable:
            return classes
        ref = safe_parse(target)
        if ref is None or ref.tag is None:
            # Started by id, or by a digest without a tag: there is no "newer build of the same tag" to update to.
            classes.update({v.key: "waiting" for v in fixable})
            return classes
        local = run.store.inspect(image.image_id)
        if local.from_registry(ref):
            tag = replace(ref, digest=None)  # a pin is judged through its tag; moving the pin is the update
            newer = self._newer(tag, lambda: rebuilt(run.registry, tag, local), run, _short(target))
            classes.update({v.key: "update" if newer is not None and v.key not in newer else "waiting" for v in fixable})
            return classes
        # Built here: what came with the base waits for (or is fixed by) a newer base; the rest is in the user's hands.
        base = run.store.base_of(local)
        base_layers: set[str] = set()
        if base is not None:
            try:
                base_layers = set(base.layers(run.registry))
            except RegistryError as exc:
                # Which layers are the base's is unknown this run: like any registry that didn't answer, waiting (a
                # hiccup must not turn the card red), and the note says why.
                log.info("image_vulns: layers of base %s not read: %s", base.name, exc.summary)
                run.unchecked.append(_short(target))
                classes.update({v.key: "waiting" for v in fixable})
                return classes
        from_base = [v for v in fixable if v.layer and v.layer in base_layers]
        classes.update({v.key: "rebuild" for v in fixable if v not in from_base})
        if from_base and base is not None:
            if base.pinned and base.ref.tag is None:
                classes.update({v.key: "waiting" for v in from_base})  # a digest without a tag has no newer build
            else:
                newer = self._newer(replace(base.ref, digest=None), lambda: base.rebuilt(run.registry), run, _short(target))
                classes.update({v.key: "rebuild" if newer is not None and v.key not in newer else "waiting" for v in from_base})
        return classes

    def _newer(self, ref: ImageRef, is_rebuilt: Callable[[], bool], run: _Run, shown: str) -> set[tuple[str, str]] | None:
        """The (CVE, package) pairs in the newer build of `ref`, None when the registry has no build newer than this
        image (`is_rebuilt`). Whether it is newer depends on each image's own layers, so that is asked per image (the
        registry client asks each tag once per run); the scan of the tag's registry build is per tag, once per run. A
        registry or scan that fails is noted (the image's fixable vulnerabilities then count as waiting)."""
        name = display_name(ref)
        try:
            if not is_rebuilt():
                return None
        except RegistryError as exc:
            log.info("image_vulns: newer build of %s not checked: %s", name, exc.summary)
            run.unchecked.append(shown)
            return None
        if name not in run.newer_scans:
            try:
                run.newer_scans[name] = {v.key for v in self._scan(name, run.left(), remote=True)}
            except CheckFailure as exc:
                log.info("image_vulns: newer build of %s not scanned: %s", name, exc.summary)
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
        started = time.monotonic()
        if not _TRIVY_LOCK.acquire(timeout=max(timeout, 0)):
            raise CheckFailure("timed out waiting for another check's Trivy scan")
        try:
            left = timeout - (time.monotonic() - started)
            if left <= 0:
                raise CheckFailure("timed out waiting for another check's Trivy scan")
            done = run_process(command, {**env, "DOCKER_HOST": self.docker_host}, left, MAX_OUTPUT, secrets)
        finally:
            _TRIVY_LOCK.release()
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


def read_accept(table: Table) -> list[Accept]:
    """`accept = [{ cve, image, until, why }, ...]`: every key of every entry is required. Entries are numbered from 1
    in messages. An `until` that has already passed is a warning, not an error: it is only a reminder to clean up."""
    raw = table.raw("accept")
    if raw is None:
        return []
    keys = {"cve", "image", "until", "why"}
    if not isinstance(raw, list) or not all(isinstance(entry, dict) for entry in raw):
        table.error('\'accept\' must be a list of tables, such as [{ cve = "CVE-2025-1", image = "postgres", until = 2027-02-17, why = "..." }]')
        return []
    accept: list[Accept] = []
    for number, entry in enumerate(raw, 1):
        where = f"'accept' entry {number}"
        problems = [f"missing key '{key}'" for key in sorted(keys - set(entry))] + [f"unknown key '{key}'" for key in sorted(set(entry) - keys)]
        if problems:
            table.error(f"{where}: {', '.join(problems)}")
            continue
        until = entry["until"]
        if isinstance(until, str) and len(until) == 10:
            with contextlib.suppress(ValueError):
                until = date.fromisoformat(until)
        if isinstance(until, datetime) or not isinstance(until, date):
            table.error(f"{where}: 'until' must be a date, such as 2027-02-17 or \"2027-02-17\"")
            continue
        texts = {key: entry[key] for key in ("cve", "image", "why")}
        empty = [key for key, text in texts.items() if not isinstance(text, str) or not text.strip()]
        if empty:
            table.error(f"{where}: {', '.join(f'{key!r}' for key in empty)} must be a non-empty string")
            continue
        accept.append(Accept(texts["cve"].strip(), texts["image"].strip(), until, texts["why"].strip()))
        if until < _now().date():
            table.warning(f"accept entry {number} ({texts['cve']}, {texts['image']}): until {until.isoformat()} has passed: remove it")
    return accept


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
td.accepted { color: var(--muted); white-space: normal; min-width: 16em; }
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


def _accept_table(check: ImageVulns, report: Report | None) -> str:
    """The `accept` entries, with the ones that matched nothing in the last run and the ones that ran out marked."""
    e = html.escape
    today = (report.ts if report is not None else check.now()).date()
    rows = []
    for index, a in enumerate(check.accept):
        marks = []
        if a.until < today:
            marks.append("expired: counts again")
        if report is not None and not report.error and index not in report.matched:
            marks.append("not found in the last scan")
        rows.append(
            f"<tr><td>{e(a.cve)}</td><td>{e(a.image)}</td><td>{e(a.until.isoformat())}</td><td>{e(a.why)}</td><td>{e('; '.join(marks))}</td></tr>"
        )
    return (
        "<p>Accepted (not counted through the end date):</p><div class=scroll><table>"
        "<tr><th>CVE</th><th>Image</th><th>Until</th><th>Why</th><th></th></tr>" + "".join(rows) + "</table></div>"
    )


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
        if check.ignore:
            parts.append(f"<p>Ignores: {e(', '.join(sorted(check.ignore)))}.</p>")
        if check.accept:
            parts.append(_accept_table(check, report))
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
            if f.accepted is not None:
                level_cell = f"<td class=accepted>accepted until {e(f.accepted.until.isoformat())}: {e(f.accepted.why)}</td>"
            else:
                level_cell = f"<td class={f.level}>{e(f.level if f.scheduled else _DASH)}</td>"
            parts.append(
                f"<tr>{level_cell}<td>{e(v.severity.lower())}</td><td>{e(f.reference)}</td>"
                f"<td>{cve}</td><td>{e(v.package)}</td><td>{e(v.installed)}</td><td>{e(v.fixed or _DASH)}</td>"
                f"<td>{e(klass)}</td><td>{e(format_time(f.first_found))}</td></tr>"
            )
        parts.append("</table></div>")
    parts.append("</body></html>")
    return "".join(parts).encode("utf-8")
