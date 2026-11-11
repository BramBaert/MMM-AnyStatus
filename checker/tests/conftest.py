from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from helpers import ONE_CHECK, World, make_world


@pytest.fixture
def one_check(tmp_path: Path) -> Callable[..., World]:
    def make(extra: str = "", **kwargs: Any) -> World:
        return make_world(ONE_CHECK.format(extra=extra), tmp_path, **kwargs)

    return make
