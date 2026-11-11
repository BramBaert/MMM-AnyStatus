"""A custom check for the `python` type: the function gets the check's config table and returns a number
(compared with warn/crit) or `anystatus.Observation(...)` to decide the level itself."""

import shutil
from typing import Any


def disk_used_percent(cfg: dict[str, Any]) -> float:
    usage = shutil.disk_usage(cfg.get("path", "."))
    return round(usage.used / usage.total * 100, 1)
