"""Entry point for ``python -m hooky``.

Delegates to the Click CLI (same callable as the ``hooky`` console script).
"""

from __future__ import annotations

from hooky.cli.main import cli

if __name__ == "__main__":
    cli()
