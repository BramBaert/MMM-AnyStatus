"""`image_vulns`: fixable vulnerabilities in the images of the running containers, found by Trivy.

Runs `trivy image --format json --ignore-unfixed --quiet --scanners vuln [--cache-dir DIR] <image>` for each
distinct image (a list, no shell) and counts only vulnerabilities that have a `FixedVersion`: a base image always
carries "will not fix" CVEs, and counting those would keep the check amber forever. The check decides its own level:
a fixable CRITICAL one is `crit`, any other counted severity (HIGH by default) is `warn`. `severities` chooses which
severities count at all.

Each image's scan gets the time that is left of the run; a timeout kills Trivy. An image that couldn't be scanned
is mentioned in the summary; the check fails only if no image could be scanned.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from dataclasses import dataclass

from anystatus.checks.base import Check, register
from anystatus.checks.docker_api import (
    DockerClient,
    Endpoint,
    RunningImage,
    docker_endpoint,
    image_info,
    running_images,
)
from anystatus.checks.exec_cmd import run_process
from anystatus.errors import mask
from anystatus.model import CheckFailure, Observation
from anystatus.table import Table

log = logging.getLogger("anystatus")

SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN")
DEFAULT_SEVERITIES = ["CRITICAL", "HIGH"]
MAX_OUTPUT = 32 * 1024 * 1024  # bytes of Trivy's JSON kept per image
MAX_IMAGES_SHOWN = 1


@dataclass(frozen=True)
class Vuln:
    id: str
    package: str
    severity: str
    fixed: str


def parse_trivy(text: str) -> list[Vuln]:
    """The fixable vulnerabilities in Trivy's JSON (Results[].Vulnerabilities[] with a FixedVersion), each once."""
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("not a Trivy report")
    results = data.get("Results")
    found: dict[tuple[str, str], Vuln] = {}
    for result in results if isinstance(results, list) else []:
        vulns = result.get("Vulnerabilities") if isinstance(result, dict) else None
        for item in vulns if isinstance(vulns, list) else []:
            if not isinstance(item, dict):
                continue
            fixed, vuln_id = item.get("FixedVersion"), item.get("VulnerabilityID")
            if not isinstance(fixed, str) or not fixed.strip() or not isinstance(vuln_id, str):
                continue
            package = str(item.get("PkgName", ""))
            severity = str(item.get("Severity", "UNKNOWN")).upper()
            found.setdefault((vuln_id, package), Vuln(vuln_id, package, severity, fixed.strip()))
    return list(found.values())


@dataclass(frozen=True)
class _Scan:
    image: str
    vulns: list[Vuln]

    def count(self, severity: str) -> int:
        return sum(1 for v in self.vulns if v.severity == severity)


@register
class ImageVulns(Check):
    type_name = "image_vulns"

    def __init__(
        self,
        endpoint: Endpoint,
        trivy: list[str],
        cache_dir: str | None,
        severities: list[str],
        env_names: list[str],
    ) -> None:
        self.client = DockerClient(endpoint)
        self.trivy = trivy
        self.cache_dir = cache_dir
        self.severities = severities
        self.env_names = env_names

    @classmethod
    def from_config(cls, table: Table) -> ImageVulns:
        for key in ("warn", "crit", "worse"):
            if table.has(key):
                table.error(
                    f"image_vulns decides its own level (fixable CRITICAL: crit, other counted severities: warn): "
                    f"'{key}' is not used; 'severities' chooses what counts"
                )
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
        severities = [s.upper() for s in (table.str_list("severities") or DEFAULT_SEVERITIES)]
        if not severities or any(s not in SEVERITIES for s in severities):
            table.error(f"'severities' must be a list of: {', '.join(SEVERITIES)}")
            severities = list(DEFAULT_SEVERITIES)
        env_names = table.str_list("env") or []
        if not table.has("timeout"):
            table.warning('image_vulns: the default timeout of 10 s is too short for Trivy: set timeout = "15m"')
        return cls(endpoint, trivy, cache_dir, severities, env_names)

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

        images = running_images(self.client, left())
        if not images:
            return Observation(level="ok", summary="no running containers", value=0)
        scans: list[_Scan] = []
        failed: list[str] = []
        timed_out = False
        first_failure = "trivy failed"
        for image in images:
            try:
                target = self._target(image, left())
                scans.append(_Scan(_short(target), self._scan(target, left())))
            except CheckFailure as exc:
                failed.append(image.reference or image.image_id[:19])
                first_failure = exc.summary
                if exc.summary.startswith("timed out"):
                    timed_out = True
                    failed.extend(i.reference or i.image_id[:19] for i in images[images.index(image) + 1 :])
                    break
        if not scans:
            raise CheckFailure(first_failure if timed_out or len(images) == 1 else f"trivy failed: {first_failure}")

        counted = set(self.severities)
        per_image = [_Scan(s.image, [v for v in s.vulns if v.severity in counted]) for s in scans]
        total = sum(len(s.vulns) for s in per_image)
        affected = sorted(
            (s for s in per_image if s.vulns),
            key=lambda s: ([-s.count(sev) for sev in SEVERITIES], -len(s.vulns), s.image),
        )
        if not total:
            level = "ok"
            noun = "image" if len(scans) == 1 else "images"
            text = f"no fixable vulnerabilities in {len(scans)} {noun}"
        else:
            level = "crit" if "CRITICAL" in counted and any(s.count("CRITICAL") for s in per_image) else "warn"
            worst = affected[0]
            parts = [f"{worst.count(sev)} {sev.lower()}" for sev in SEVERITIES if sev in counted and worst.count(sev)]
            text = f"{worst.image}: {', '.join(parts)}"
            top = next(sev for sev in SEVERITIES if worst.count(sev))
            fixes = {v.fixed for v in worst.vulns if v.severity == top}
            if len(fixes) == 1:
                text += f", fixed in {next(iter(fixes))}"
            if len(affected) > MAX_IMAGES_SHOWN:
                others = len(affected) - MAX_IMAGES_SHOWN
                text += f"; {others} more image{'s' if others != 1 else ''} affected"
        if failed:
            why = "timed out" if timed_out else "trivy failed"
            text += f"; {len(failed)} not scanned ({why})"
        return Observation(level=level, summary=text, value=total)

    def _scan(self, image: str, timeout: float) -> list[Vuln]:
        resolved = shutil.which(self.trivy[0])
        if resolved is None:
            raise CheckFailure("trivy not found")
        command = [resolved, *self.trivy[1:], "image", "--format", "json", "--ignore-unfixed", "--quiet"]
        command += ["--scanners", "vuln"]
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


def _short(reference: str) -> str:
    """The image name for a summary: the last path part without the tag (`ghcr.io/org/app:1.2` -> `app`)."""
    name = reference.split("@", 1)[0].rsplit("/", 1)[-1]
    return name.split(":", 1)[0] or reference
