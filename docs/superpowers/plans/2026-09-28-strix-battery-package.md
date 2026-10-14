# strix_battery Package Conversion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop installing five generic top-level modules (`main`, `battery`, `device`, `icon`, `tray`) into `site-packages` by moving them into a `strix_battery/` package, keeping a thin top-level `main.py` shim so the README's `uv run python main.py --json` keeps working — and fix the PyQt6 startup message so a present-but-broken Qt install is not told to install itself again.

**Staleness note (why this plan was rewritten):** first written 2026-09-28 against `abcb770`, where `main.py` was one 695-line module. The module-split plan (`docs/superpowers/plans/2026-10-10-module-split.md`, commits `594c7b8`..`70c2425`) landed first and split the file into `battery.py` / `device.py` / `icon.py` / `tray.py`, reducing `main.py` to a 177-line entry point. That plan's packaging compromise — `py-modules = ["battery", "device", "icon", "main", "tray"]` — is the P2 defect, multiplied, and its "explicitly rejected" note deferred to this plan's packaging argument. This plan was therefore re-anchored on the current layout at `638c0ea`. Its **Spec (the P2 and P3 review findings) is unchanged and still open.**

**Architecture:** A mechanical move of the current five top-level modules into `strix_battery/`. `git mv` all five: `main.py` becomes `strix_battery/__init__.py` (contents unchanged except the import lines, which become relative, and the shebang plus `__main__` guard, which are dead in a package and drop); the four siblings keep their names. A new `strix_battery/cli.py` re-exports the package surface so `strix_battery.cli:main` works as the console script. A new five-line top-level `main.py` shim re-exports from the package so `python main.py --json` and `import main` keep working. The entry point moves to `strix_battery.cli:main`; `py-modules` becomes `packages = ["strix_battery"]`. No behaviour changes and no renames — the conversion is about the import path, and doing it as renames-plus-shim means a mistake shows up as an import error, not a silently different program. Tests retarget their namespaces to the package in the same mechanical way the module-split plan retargeted `patch("main.X")` → `patch("device.X")`.

**Tech Stack:** Python 3.12, setuptools, `unittest`, uv.

**Spec:** Findings from the code review of 2026-09-28 against commit `abcb770` — the "P2 — `py-modules = ["main"]` installs a generic top-level `main`" and "P3 — `main.py:490` prints the wrong PyQt6 message" items. Re-verified open at `638c0ea`: `pyproject.toml:29` still lists five generic top-level modules, and `tray.py`'s single message still cannot tell a missing PyQt6 from a broken one.

## Global Constraints

- **The conversion is mechanical.** Every `def` in the moved files is byte-identical; the only permitted content edits are the intra-package import lines (`from battery import mv_to_percent` → `from .battery import mv_to_percent`) and, in `__init__.py`, the dropped shebang and `__main__` guard. A reviewer must be able to run `git diff -M` and see renames.
- **Use `git mv` for all five files**, never a copy-and-paste through a code block.
- **All new source and test code is ASCII-only.** If an edit tool cannot match a line because of a glyph, edit around it.
- **Tests are extended, never rewritten.** Permitted edits: the five module import lines become `import main`, `import strix_battery`, `from strix_battery import battery, device, icon, tray` (plus new test classes); patch sites retarget mechanically: `patch("device.` → `patch("strix_battery.device.` (52 sites), `patch("tray.` → `patch("strix_battery.tray.` (4 sites), `patch("main.StrixDevice")` → `patch("strix_battery.StrixDevice")` (1 site). `import main` and `from main import (...)` stay **unchanged** — the shim is what keeps them working, and a test pins it.
- **Gates.** The repo's four gates gate this change, superseding this plan's original single command: `uv run python -m unittest discover -s tests`, `uv run --group dev ruff check .`, `uv run --group dev ruff format --check .`, `uv run --group dev ty check`. Task 1 additionally runs `uv build` and inspects the wheel.
- **Version.** One bump per change, in its first commit (AGENTS.md): `0.3.0` → `0.4.0` (minor — the substance is new functionality: the `strix_battery` import path; the PyQt6 fix rides along). `uv lock` refreshed in the same commit.
- **No new runtime dependencies.** `dependencies` unchanged. `packages` and `py-modules` are mutually exclusive, so `py-modules` is deleted, not left beside `packages`.
- **`main` must remain importable and runnable.** The README documents `uv run python main.py --json`, and CI runs `python -c "import main"`. The shim keeps both working. The suite reaches `main._run_qt_app_with_sigint_shutdown`, an underscore name that a star import does not carry — the shim imports it explicitly.
- **The missing-PyQt6 message stays byte-identical.** Task 2 changes only the present-but-unusable branch.

## Review Focus

Five input classes this scope implies but that no test currently exercises. Each line's test is added in the task that owns the relevant code.

1. **A user who followed the README and runs `uv run python main.py --json`.** The shim must make this work exactly as before. Post-cli-contract-plan it prints the offline JSON and exits `1`; nothing in the suite covers it, because every test imports the module rather than executing the file. → Task 1, Step 1.
2. **The installed `strix-battery` console script.** It now resolves to `strix_battery.cli:main`, a different import path than before. Nothing verifies the entry point resolves at all — a typo there produces a wheel that installs cleanly and then fails on first run. → Task 1, Step 1, and the wheel's `entry_points.txt` in Step 11.
3. **No generic top-level `main` reaches `site-packages`** — the actual P2 proof: the built wheel's namelist contains no top-level `main.py`, and the metadata says `packages = ["strix_battery"]`. The in-repo decoy test proves the shim still wins locally against a decoy `main.py` on `PYTHONPATH`; the wheel check proves the collision with other tools' `main` is gone. → Task 1, Steps 1 and 11.
4. **PyQt6 present but raising `ImportError` at runtime.** Expected: a message that says PyQt6 was found but unusable and names the real cause, not "PyQt6 is required … install it". Today's single message in `tray.py` cannot tell the two apart. → Task 2, Step 1.
5. **PyQt6 missing entirely.** Expected: the existing install-instructions message, byte-identical, and exit `1`. Must not change. → Task 2, Step 1.

---

### Task 1: Move the five top-level modules into `strix_battery/`, keeping `main.py` as a shim

**Files:**
- Create: `strix_battery/__init__.py` (via `git mv main.py strix_battery/__init__.py`, then drop the shebang/guard and relativize the imports)
- Create: `strix_battery/battery.py`, `strix_battery/device.py`, `strix_battery/icon.py`, `strix_battery/tray.py` (via `git mv`, contents unchanged except intra-package imports)
- Create: `strix_battery/cli.py` — re-exports
- Create: `main.py` — the shim, replacing the 177-line entry point
- Modify: `pyproject.toml` — `version` (line 3), `[project.scripts]` (line 13), `[tool.setuptools]` (lines 25-29)
- Modify: `tests/test_main.py` — new `PackageLayoutTests` class, retargeted imports and patch namespaces

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces:
  - `strix_battery/` — every name previously importable from `main` (and the four siblings), unchanged: the `battery` surface (`DEFAULT_DISCHARGE_CURVE`, `DISCHARGE_CURVE_ENV_VAR`, `load_discharge_curve`, `parse_discharge_curve`, `mv_to_percent`), the `device` surface (`StrixDevice`, `offline_message`, `validate_sleep_minutes`, `noise_reduction_state_path`, `build_sleep_packet`, `decode_status`, `ASUS_VID`, `STRIX_PIDS`, `MAX_SLEEP_MINUTES`, `ERROR_OFFLINE`, `ERROR_PERMISSION`, `NOTIFY_TIMEOUT_SECONDS`, `BATTERY_DISPLAY_DEADBAND_PERCENT`, `LOW_BATTERY_NOTIFY_PERCENT`, `LOW_BATTERY_REARM_PERCENT`, `QUERY_PACKET`, the `HIDIOC*`/`OFFSET_*`/`REPLY_MARKER`/`SLEEP_PACKET_COMMAND`/`CHARGING_FLAG`/`NOISE_REDUCTION_SUB_COMMAND` constants), the `icon` surface (`render_tray_icon`, `pil_to_qpixmap`), the `tray` surface (`HEADSET_ICON`, `SLEEP_ICON`, `NOISE_REDUCTION_ICON`, `_run_qt_app_with_sigint_shutdown`, `run_pyqt_tray`, `start_tray`), the entry-point names (`EXIT_OK`, `EXIT_OFFLINE`, `EXIT_USAGE`, `render_json_status`, `render_text_status`, `main`), and the private helpers.
  - `strix_battery/cli.py` — `main` (the entry point) plus every public name above.
  - `main.py` — importable, runnable, re-exports from `strix_battery`, including `_run_qt_app_with_sigint_shutdown`.

**Context you need:** `pyproject.toml:29` is `py-modules = ["battery", "device", "icon", "main", "tray"]`. Setuptools installs each of those as a top-level module into `site-packages`, where they sit on `sys.path` for every Python process on the machine. The module-split plan chose that compromise to ship a wheel whose `main` could import its siblings; it removed the `main`-only breakage and left the collision, now with five names to collide on. The safe way to do this is `git mv`, so git records renames and the diff stays reviewable:

```bash
mkdir -p strix_battery
git mv battery.py device.py icon.py tray.py strix_battery/
git mv main.py strix_battery/__init__.py
```

Then write the three new files and edit only the import lines in the moved ones. The `if __name__ == "__main__": main()` guard at the bottom of the moved `main.py` is now dead code (a package is never `__main__`) and drops; the shim carries the guard.

- [x] **Step 1: Write the failing tests**

First swap the module imports in the test file. Replace the five lines

```python
import battery
import device
import icon
import main
import tray
```

with

```python
import main
import strix_battery
from strix_battery import battery, device, icon, tray
```

(`from main import (...)` keeps its place after these; ruff's import sort is the arbiter of the final order.) These lines fail until the package exists, which is part of the red state.

Then insert this class immediately before `class SleepDurationTests(unittest.TestCase):`:

```python
class PackageLayoutTests(unittest.TestCase):
    """A generic top-level `main` in site-packages is the reason for this change."""

    def test_the_package_is_importable_and_not_a_top_level_module(self):
        self.assertTrue(strix_battery.__file__.endswith("strix_battery/__init__.py"))
        self.assertNotEqual(
            getattr(strix_battery, "__file__", ""),
            main.__file__,
            "the package and the shim must be two files, not an alias",
        )

    def test_the_shim_still_runs_as_a_script(self):
        # The README documents `uv run python main.py --json`.
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "main.py", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(
            result.returncode,
            1,
            f"the shim must still reach main(); stderr was: {result.stderr}",
        )
        self.assertIn("offline", result.stdout)

    def test_the_console_script_entry_point_resolves(self):
        import importlib

        module = importlib.import_module("strix_battery.cli")
        self.assertTrue(callable(module.main))

    def test_every_name_the_suite_imports_is_still_exported(self):
        for name in [
            "DEFAULT_DISCHARGE_CURVE", "ERROR_OFFLINE", "ERROR_PERMISSION",
            "HEADSET_ICON", "HIDIOCGFEATURE_64", "HIDIOCGRAWINFO",
            "MAX_SLEEP_MINUTES", "NOTIFY_TIMEOUT_SECONDS", "QUERY_PACKET",
            "SLEEP_ICON", "STRIX_PIDS", "StrixDevice", "load_discharge_curve",
            "main", "mv_to_percent", "noise_reduction_state_path",
            "offline_message", "parse_discharge_curve", "pil_to_qpixmap",
            "render_tray_icon", "run_pyqt_tray", "start_tray",
            "validate_sleep_minutes",
        ]:
            with self.subTest(name=name):
                self.assertTrue(
                    hasattr(strix_battery, name), f"strix_battery lost {name}"
                )
                self.assertTrue(
                    hasattr(importlib.import_module("strix_battery.cli"), name),
                    f"strix_battery.cli lost {name}",
                )

    def test_the_shim_beats_a_decoy_main_on_the_path(self):
        # The whole point: a top-level `main` must no longer be load-bearing.
        # The repo root is sys.path[0] for `-c`, so the shim must win over a
        # decoy placed on PYTHONPATH, and what wins must be the package's.
        import os
        import subprocess
        import sys
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "main.py").write_text("raise SystemExit('decoy won')\n")
            env = {**os.environ, "PYTHONPATH": tmp}
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import main; print(main.__file__); print(main.StrixDevice.__module__)",
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
            )
        self.assertNotIn("decoy won", result.stdout)
        self.assertIn("strix_battery.device", result.stdout)

    def test_the_shim_exposes_the_private_helper_the_suite_patches(self):
        # `from ... import *` skips underscore names; the suite reaches this one
        # through `main`.
        self.assertIs(
            main._run_qt_app_with_sigint_shutdown,
            strix_battery._run_qt_app_with_sigint_shutdown,
        )
```

`importlib` is needed by two tests; import it at the top of the class file scope is not required — the per-method imports above are deliberate so the class stays self-contained.

- [x] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync python -m unittest tests.test_main.PackageLayoutTests -v`

Expected: `ModuleNotFoundError: No module named 'strix_battery'`. Correct red state — the whole module fails to import until the package exists.

- [x] **Step 3: Move the modules**

```bash
mkdir -p strix_battery
git mv battery.py device.py icon.py tray.py strix_battery/
git mv main.py strix_battery/__init__.py
```

Verify the moves preserved the files exactly:

```bash
git status --short
```

Expected: five `R` (rename) entries, one of them `main.py -> strix_battery/__init__.py`. `git diff --cached -M --stat` should show each rename with no content change (100% similarity). If any file shows insertions/deletions, stop and investigate — something was rewritten during the move.

- [x] **Step 4: Relativize the intra-package imports**

Three files carry intra-package imports; edit only those lines:

- `strix_battery/device.py` — `from battery import mv_to_percent` becomes `from .battery import mv_to_percent`
- `strix_battery/tray.py` — `from device import StrixDevice, offline_message` becomes `from .device import StrixDevice, offline_message`; `from icon import pil_to_qpixmap, render_tray_icon` becomes `from .icon import pil_to_qpixmap, render_tray_icon`
- `strix_battery/__init__.py` — the four `from battery/device/icon/tray import (...)` blocks become `from .battery import (...)`, `from .device import (...)`, `from .icon import ...`, `from .tray import (...)`, each keeping its existing names and `# noqa` comments; drop the `#!/usr/bin/env python3` shebang and the trailing `if __name__ == "__main__": main()` guard (dead in a package); add a one-line module docstring naming the package.

`battery.py` and `icon.py` have no intra-package imports and do not change.

- [x] **Step 5: Write the CLI re-export module**

Create `strix_battery/cli.py`:

```python
"""Console entry point: `strix_battery.cli:main` is what the installed script runs.

The star re-export keeps this module a complete import surface, so tooling that
imports the entry point sees the same names the top-level `main` shim exposes.
The loud pin on the surface itself is `tests/test_main.py`'s PublicSurfaceTests,
which fails in CI if a name is ever dropped.
"""

from strix_battery import *  # noqa: F401,F403
from strix_battery import main  # noqa: F401
```

- [x] **Step 6: Write the top-level shim**

Create `main.py` (absent after the move):

```python
#!/usr/bin/env python3
"""Compatibility shim. The implementation moved to the `strix_battery` package.

Kept so `uv run python main.py --json` keeps working, as the README documents,
and so an existing `import main` does not break. Nothing should be added here;
the real module is `strix_battery`.
"""

import sys

from strix_battery import *  # noqa: F401,F403
from strix_battery import _run_qt_app_with_sigint_shutdown, main as _main  # noqa: F401

if __name__ == "__main__":
    sys.exit(_main())
```

`sys.exit(_main())` rather than bare `main()` matters: `main()` may call
`sys.exit(1)` itself on an offline device or a usage error, and a shim that
returns normally would swallow that into a `0` exit. The explicit
`_run_qt_app_with_sigint_shutdown` import is there because a star import skips
underscore names and the suite reaches that helper through `main`.

- [x] **Step 7: Point the packaging at the package**

In `pyproject.toml`, replace the `[project.scripts]` entry point and the `[tool.setuptools]` block:

```toml
[project.scripts]
strix-battery = "strix_battery.cli:main"
```

```toml
[tool.setuptools]
# The package keeps every module off the top level of site-packages; a generic
# `main` there collides with any other tool that ships one.
packages = ["strix_battery"]
```

Delete the `py-modules` line and its comment. `packages` and `py-modules` are mutually exclusive, so leaving both in place fails the build rather than silently preferring one.

(The version bump `0.3.0` → `0.4.0` and the `uv lock` refresh ride in this change's first commit, which is the plan re-anchor commit, per AGENTS.md.)

- [x] **Step 8: Retarget the test suite's namespaces**

Apply the mechanical substitutions to `tests/test_main.py`:

```bash
sed -i 's/patch("device\./patch("strix_battery.device./g; s/patch("tray\./patch("strix_battery.tray./g; s/patch("main\.StrixDevice"/patch("strix_battery.StrixDevice"/g' tests/test_main.py
```

Verify the counts: `grep -c 'patch("strix_battery\.' tests/test_main.py` → `57`; `grep -c 'patch("device\.\|patch("tray\.' tests/test_main.py` → `0`; `grep -c 'patch("main\.' tests/test_main.py` → `0`.

- [x] **Step 9: Run the full suite to verify it passes**

Run: `uv run --no-sync python -m unittest discover -s tests -v`

Expected: `OK` (118 existing tests plus 6 new). Every existing test must still pass with only the import-line swap and the patch retargets applied — that is the real assertion of this task: the shim resolves `patch("strix_battery.StrixDevice")` and every `patch("strix_battery.<module>.<attr>")` to the same module objects the code under test uses, and `from main import (...)` still reaches the package. A silently ineffective patch would leave a test passing for the wrong reason, which is why the retargeted sites are counted in Step 8, not eyeballed.

- [x] **Step 10: Run the other three gates**

Run: `uv run --group dev ruff check . && uv run --group dev ruff format --check . && uv run --group dev ty check`

Expected: all pass. Note `tests/test_main.py` may want the `strix_battery` import block in isort's order; ruff's `I` rule is the arbiter — fix the order it asks for, nothing else.

- [x] **Step 11: Verify the build and the entry points**

```bash
uv sync
uv build
ls -la dist/
```

`uv sync` refreshes the editable install and the `strix-battery` script wrapper to the new entry point. Then confirm the wheel no longer carries any top-level module:

```bash
uv run --no-sync python -c "
import zipfile, glob
w = sorted(glob.glob('dist/*.whl'))[-1]
names = zipfile.ZipFile(w).namelist()
print('top-level main.py:', 'main.py' in names)
print('top-level siblings:', [n for n in names if n.endswith('.py') and '/' not in n])
print('package files:', sorted(n for n in names if n.startswith('strix_battery/')))
eps = zipfile.ZipFile(w).read([n for n in names if n.endswith('entry_points.txt')][0]).decode()
print('entry_points:', eps.strip())
"
```

Expected: `top-level main.py: False`; no top-level `.py` at all; `strix_battery/` containing `__init__.py`, `cli.py`, `battery.py`, `device.py`, `icon.py`, `tray.py`; and `entry_points:` showing `strix-battery = strix_battery.cli:main`. A `True` on the first line means the change did not take effect in the build config — stop and fix `pyproject.toml` before committing.

Then confirm both entry points still run:

```bash
uv run python main.py --json; echo "exit=$?"
uv run strix-battery --json; echo "exit=$?"
```

Expected: both print `{"status": "offline", "text": "Headset offline"}` and exit `1` (no headset on this machine). CI's `python -c "import main"` smoke check is covered by the same shim.

- [x] **Step 12: Commit**

```bash
git add -A
git commit -m "refactor: move the five top-level modules into the strix_battery package"
```

---

### Task 2: Tell a broken PyQt6 apart from a missing one

**Files:**
- Modify: `strix_battery/tray.py` — the `start_tray` function and a new message pair
- Test: `tests/test_main.py` — new `StartTrayMessageTests` class before `class SleepDurationTests`

**Interfaces:**
- Consumes: `importlib.util.find_spec`, `run_pyqt_tray` (both unchanged in signature); the `strix_battery.tray` namespace produced by Task 1.
- Produces: `tray.PYQT6_MISSING_HINT: str` and `tray.PYQT6_UNUSABLE_HINT: str` — the two distinct messages, named so tests and future editors assert on the right one.

**Context you need:** `start_tray` (`strix_battery/tray.py`) has exactly one failure path today: `run_pyqt_tray` raises `ImportError` — which is what a *missing* PyQt6 produces — and the handler prints "Error: 'PyQt6' is required for tray mode. Install it by running: uv add PyQt6 pillow". A PyQt6 that is installed but broken (a broken Qt install, a missing platform plugin like `xcb` on a minimal desktop) raises the same `ImportError` and gets the same advice, which tells that user to install something they already have. That is the P3 finding's defect, unchanged by the split. The discriminator already exists in the stdlib: `importlib.util.find_spec("PyQt6")` checks *presence*, not importability — a broken install still produces a spec, a missing one does not. Put that check in the `except` branch (not a pre-check before the call: the current structure deliberately tries first, and a pre-check would restore a second structure the tray work removed), and print exactly one message either way. The missing-PyQt6 text stays byte-identical to today — the three old `print()` calls concatenate to one constant printed with the default `end`, trailing double newline included.

- [x] **Step 1: Write the failing tests**

Insert this class immediately before `class SleepDurationTests(unittest.TestCase):`:

```python
class StartTrayMessageTests(unittest.TestCase):
    """A broken Qt install and a missing Qt need different advice."""

    def messages_for(self, pyqt6_found):
        printed = []
        found = object() if pyqt6_found else None

        def fake_run_pyqt_tray(device):
            raise ImportError("No module named 'PyQt6.QtWidgets'")

        with patch("builtins.print", side_effect=printed.append), patch(
            "strix_battery.tray.importlib.util.find_spec", return_value=found
        ), patch(
            "strix_battery.tray.run_pyqt_tray", side_effect=fake_run_pyqt_tray
        ), self.assertRaises(SystemExit) as raised:
            start_tray(StrixDevice())
        self.assertEqual(raised.exception.code, 1)
        return " ".join(printed)

    def test_a_missing_pyqt6_keeps_the_install_instructions(self):
        message = self.messages_for(pyqt6_found=False)
        self.assertIn("is required for tray mode", message)
        self.assertIn("uv add PyQt6", message)
        self.assertNotIn(
            "could not be used",
            message,
            "a missing PyQt6 is fixed by installing it; say so",
        )

    def test_a_broken_pyqt6_is_not_told_to_install_itself(self):
        message = self.messages_for(pyqt6_found=True)
        self.assertIn("installed but could not be used", message)
        self.assertNotIn(
            "is required for tray mode",
            message,
            "telling a user with a broken Qt to install it again changes nothing",
        )

    def test_a_broken_pyqt6_names_the_real_cause(self):
        message = self.messages_for(pyqt6_found=True)
        self.assertIn("No module named 'PyQt6.QtWidgets'", message)

    def test_each_case_prints_exactly_one_message(self):
        for found in (True, False):
            with self.subTest(found=found):
                message = self.messages_for(found)
                self.assertEqual(message.count("Error:"), 1)
```

The two branches are told apart by `find_spec`, so the helper patches
`strix_battery.tray.importlib.util.find_spec` rather than pre-deciding which
message the code may print. `start_tray` is imported at the top of the file from
`main`, which re-exports the package — the same function object the tray code
defines, so the patches bite.

- [x] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync python -m unittest tests.test_main.StartTrayMessageTests -v`

Expected: `test_a_broken_pyqt6_is_not_told_to_install_itself` and
`test_a_broken_pyqt6_names_the_real_cause` fail, because the single current
message says "is required for tray mode" and never names the cause.
`test_each_case_prints_exactly_one_message` passes (today there is only ever one
message). That is the correct red state: the missing-case behaviour already
holds and is pinned; the broken-case behaviour is what is missing.

- [x] **Step 3: Split the message in `strix_battery/tray.py`**

Add `import importlib.util` to the stdlib import block at the top of the file (isort order: `importlib.util` before `signal`). Then, immediately after the glyph constants, add:

```python
# `start_tray` fails in two distinct ways and they need different advice.
# A missing PyQt6 is fixed by installing it. A PyQt6 that is installed but
# raises on import is a broken Qt install or a missing platform plugin, and
# telling that user to install it again changes nothing.
PYQT6_MISSING_HINT = (
    "\nError: 'PyQt6' is required for tray mode.\n"
    "Install it by running:\n"
    "  uv add PyQt6 pillow\n"
)

PYQT6_UNUSABLE_HINT = (
    "\nError: 'PyQt6' is installed but could not be used for tray mode.\n"
    "This usually means a broken Qt install or a missing platform plugin;\n"
    "reinstalling PyQt6 alone may not fix it.\n"
    "Cause: {error}\n"
    "To reinstall PyQt6 and its dependencies anyway, run:\n"
    "  uv add PyQt6 pillow\n"
)
```

Then replace the whole `start_tray` function with:

```python
def start_tray(device: StrixDevice):
    """Run the PyQt6 tray, or explain that it is unavailable."""
    try:
        run_pyqt_tray(device)
    except ImportError as exc:
        # `find_spec` checks presence, not importability: a PyQt6 that is
        # installed but broken still produces a spec, and that user needs
        # different advice from one who never installed it.
        if importlib.util.find_spec("PyQt6") is None:
            print(PYQT6_MISSING_HINT)
        else:
            print(PYQT6_UNUSABLE_HINT.format(error=exc))
        sys.exit(1)
```

`print(...)` with the default `end` is deliberate: the constant ends in one
`\n` and `print` adds the second, so the missing-PyQt6 branch's output is
byte-identical to the three `print()` calls it replaces.

- [x] **Step 4: Run the full suite to verify it passes**

Run: `uv run --no-sync python -m unittest discover -s tests -v`

Expected: `OK` (the suite so far, plus this task's 4 new tests). Note the
existing `test_start_tray_exits_when_pyqt6_is_not_installed` does not patch
`find_spec`, so with PyQt6 installed in the dev environment it now exercises
the *broken-install* branch — its assertions ("PyQt6" in the output, exit 1)
hold on both messages, which is why it needed no edit.

- [x] **Step 5: Verify both messages by hand**

```bash
uv run --no-sync python - <<'PY'
from unittest.mock import patch
from strix_battery import device as device_mod
from strix_battery import tray
from strix_battery.device import StrixDevice

for found, label in [(True, "broken"), (False, "missing")]:
    out = []

    def fake(device):
        raise ImportError("No module named 'PyQt6.QtWidgets'")

    with patch("builtins.print", side_effect=out.append), \
         patch("strix_battery.tray.importlib.util.find_spec",
               return_value=object() if found else None), \
         patch("strix_battery.tray.run_pyqt_tray", side_effect=fake):
        try:
            tray.start_tray(StrixDevice())
        except SystemExit as exc:
            out.append(f"[exit {exc.code}]")
    print(f"=== {label} ===")
    print("".join(str(x) for x in out))
PY
```

Expected: the `broken` case prints "installed but could not be used", names the
cause, and does **not** print "is required for tray mode"; the `missing` case
prints exactly the old three lines. Each prints exactly one `Error:` and exits
`1`.

- [x] **Step 6: Run the other three gates**

Run: `uv run --group dev ruff check . && uv run --group dev ruff format --check . && uv run --group dev ty check`

Expected: all pass.

- [x] **Step 7: Commit**

```bash
git add -A
git commit -m "fix: tell a broken PyQt6 apart from a missing one"
```

---

## Verification Record

Measured on the host running this plan, at `638c0ea` (the plan's original `abcb770` measurements are in the staleness note):

- `pyproject.toml:13` is `strix-battery = "main:main"` and `pyproject.toml:29` is `py-modules = ["battery", "device", "icon", "main", "tray"]`. The wheel therefore installs five generic top-level modules into `site-packages`.
- `README.md:63` documents `uv run python main.py --json`, which is why the shim is required rather than optional; `.github/workflows/ci.yml` runs `python -c "import main"` as a smoke check, which the shim also keeps alive.
- The suite (118 tests) imports all five modules at top level and patches `device.*` ×52, `tray.*` ×4 and `main.StrixDevice` ×1. Those patches must resolve, after the move, to the same module objects the code under test uses — which is why the full suite passing unmodified-but-retargeted is this plan's primary proof.
- The suite reaches `main._run_qt_app_with_sigint_shutdown`; a star import does not carry underscore names, so the shim imports it explicitly.
- `strix_battery` does not exist at `638c0ea`; `tray.py`'s `start_tray` has a single `except ImportError` printing one message.

## Out of Scope

- **Adding `pygobject` to a `fallback-tray` extra.** Moot: the pystray backend and every extra are gone (`pyproject.toml` has no `optional-dependencies`; `StrixTrayPystray` no longer exists).
- **Renaming `tests/test_main.py`.** It no longer matches the module it tests. Purely cosmetic and mechanical; do it in a follow-up if at all, so this plan's diff stays reviewable.
- **The `strix_battery` package's internal module split further** (renaming `battery.py`/`device.py`/`icon.py`/`tray.py` to package-scoped names like `capacity.py`). The names are generic, but they are now package-scoped, which is what removes the collision; renaming them is a separate cosmetic decision.
- **Anything about the HID protocol, the discharge curve, or the CLI exit codes.** Separate plans, all landed.
