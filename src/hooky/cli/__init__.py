"""Hooky command-line interface.

The ``hooky`` console script (declared in pyproject.toml) points at
:func:`hooky.cli.main.cli`.
"""

from hooky.cli.main import cli

__all__ = ["cli"]
