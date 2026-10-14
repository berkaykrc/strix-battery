#!/usr/bin/env python3
"""Compatibility shim. The implementation moved to the `strix_battery` package.

Kept so `uv run python main.py --json` keeps working, as the README documents,
and so an existing `import main` does not break. Nothing should be added here;
the real module is `strix_battery`.
"""

import sys

from strix_battery import *  # noqa: F403
from strix_battery import _run_qt_app_with_sigint_shutdown  # noqa: F401
from strix_battery import main as _main

if __name__ == "__main__":
    sys.exit(_main())
