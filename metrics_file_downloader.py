"""Compatibility entry point for the packaged metrics downloader."""

from __future__ import annotations

import sys

from glassnode_mass_downloader import metrics_file_downloader as _implementation


# Keep existing ``import metrics_file_downloader`` callers pointed at the
# packaged implementation so their patches and imports use one module.
sys.modules[__name__] = _implementation


if __name__ == "__main__":
    raise SystemExit(_implementation.main())
