"""Console entry point: `strix_battery.cli:main` is what the installed script runs.

The star re-export keeps this module a complete import surface, so tooling that
imports the entry point sees the same names the top-level `main` shim exposes.
The loud pin on that surface is
`tests/test_main.py`'s `PackageLayoutTests.test_every_name_the_suite_imports_is_still_exported`,
which fails in CI if a name is ever dropped. `PublicSurfaceTests` pins the
`main` shim's surface, not this module's.
"""

from strix_battery import *  # noqa: F403
from strix_battery import main  # noqa: F401
