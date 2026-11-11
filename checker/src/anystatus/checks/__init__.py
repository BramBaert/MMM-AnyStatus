"""Check types. Importing this package registers them all."""

from anystatus.checks import ping, python_fn, sql_age  # noqa: F401  (registration)
from anystatus.checks.base import REGISTRY, SECTIONS, Check, CheckMemory, register, register_section

__all__ = ["REGISTRY", "SECTIONS", "Check", "CheckMemory", "register", "register_section"]
