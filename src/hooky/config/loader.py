"""Config loading.

Load order in Sprint 1:

1. If a YAML path is supplied, parse it.
2. Otherwise return defaults.

Env-var overlay arrives in a later sprint (driven by ``pydantic-settings``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from hooky.config.schema import HookyConfig


def load_config(path: str | Path | None = None) -> HookyConfig:
    """Load Hooky config from a YAML file, or return defaults.

    Args:
        path: Optional path to a YAML config file. ``None`` returns defaults.

    Returns:
        Parsed :class:`HookyConfig` instance.

    Raises:
        FileNotFoundError: If ``path`` is supplied but does not exist.
        ValueError: If the YAML root is not a mapping.
        pydantic.ValidationError: If the config violates the schema.
    """
    if path is None:
        return HookyConfig()

    cfg_path = Path(path)
    if not cfg_path.is_file():
        raise FileNotFoundError(f"Config file not found: {cfg_path}")

    with cfg_path.open("r", encoding="utf-8") as fh:
        raw: Any = yaml.safe_load(fh) or {}

    if not isinstance(raw, dict):
        raise ValueError(f"Config root must be a mapping, got {type(raw).__name__}")

    return HookyConfig.model_validate(raw)
