# Module Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the 810-line `main.py` into four focused modules without changing a single behaviour, keeping `python main.py` and `import main` working exactly as they do today.

**Architecture:** A mechanical move, bottom-up along the existing dependency direction: `battery` ← `device` ← `tray`, with `icon` standing alone and `main` reduced to argument parsing. Each task moves one module's code, retargets the test suite's `patch("main.X")` call sites to that module's namespace, and ends green. No renames, no reformatting, no behaviour edits — the diff must read as a move.

**Tech Stack:** Python 3.12, `unittest`, `unittest.mock`, ruff, ty, uv.

**Spec:** None. This plan has no spec document; the design decisions it encodes live in its Architecture section and its Global Constraints. Rulings made here are provisional for that reason.

## Global Constraints

- **This is a move, not a redesign.** Every `def`, `class` and constant moves byte-identical. No renames, no reordering inside a function, no new type hints, no "while I'm here" edits. `git diff -M` must show renames.
- **`main.py` stays the entry point.** The README documents `uv run python main.py --json`, and packaging is explicitly out of scope: `pyproject.toml` is not touched by this plan, and `main` remains a top-level module. Do not create a package, do not add `__init__.py`, do not change `[tool.setuptools]`.
- **`main.py` re-exports every public name it exported before.** `tests/test_main.py` does `from main import StrixDevice, mv_to_percent, ...` and so does anyone running the README example. Re-exporting *your own* symbols from `main.py` is required by this constraint.
- **`main.py` must NOT import `fcntl`, `glob`, `os`, `signal`, `subprocess` or `time`.** Those exist in `main.py` today only so the suite's `patch("main.<module>.<attr>")` resolves. Keeping them after the split would leave six stdlib imports in the entry point that no code in it uses — dead code that exists purely to satisfy tests, and it would mislead the next reader. The suite's call sites move to the owning module instead.
- **A retargeted patch must be verified to still bite.** The failure mode is silent: `patch("device.signal", fake)` applies cleanly, the test passes, and nothing was stubbed. Every task that retargets a patch proves its new target with a test that fails when the patch is ineffective.
- **Tests are extended, never rewritten.** No assertion is changed and no test is deleted. The only edits permitted are `patch("main.X")` → `patch("<module>.X")` and the two lines that add a new import.
- **All new source and test code is ASCII-only.**
- **The gating command, and the only one that gates this plan:**
  `uv run python -m unittest discover -s tests`
  All 76 existing tests must stay green in every task. Baseline verified at `fa55f17`: `Ran 76 tests ... OK`.
- The repo's other three gates (`ruff check`, `ruff format --check`, `ty check`) must also stay green; they are cheap and a move that trips a linter is not a clean move.
- **No new runtime dependencies.**

## Review Focus

Six input classes this scope implies but that no test currently exercises.

1. **A retargeted patch resolves to an attribute that does not exist in the new module.** `patch("device.fcntl.ioctl", ...)` raises `AttributeError` at patch time, which fails loudly — this one is safe. The dangerous twin is the reverse: the attribute exists but nothing in the new module calls it, so the patch applies and stubs nothing. Every task needs a test that asserts the stub was actually reached. → each task's Step 1.
2. **`main.noise_reduction_state_path` stops biting.** Expected: the state-file tests write to their own temp directory and never touch `~/.config/strix-battery/`. If the patch is ineffective the real path is used and the suite *still passes* — it writes a real file. This is the one failure mode in this plan that produces no red test, so it needs an explicit assertion, not just a green suite. → Task 2, Step 1.
3. **`main.signal` stops biting.** Expected: `QtSignalShutdownTests` records the handler registration in its fake. If the patch is ineffective, `signal.signal` installs a real handler in the test process and the fake's `registrations` list stays empty, so the assertion fails. Loud, but only if the fake is the thing asserted on. → Task 3, Step 1.
4. **A user runs `python main.py --tray` and the tray dies at import.** Expected: the same clear PyQt6 message as today. Moving `run_pyqt_tray` into `tray.py` changes which module's `ImportError` propagates, and `start_tray`'s single `except ImportError` must still catch it. → Task 3, Step 1.
5. **A user runs `python main.py --set-sleep 30` and gets a different error than today.** Expected: identical. `validate_sleep_minutes` and `device.set_sleep` cross a module boundary; a `ValueError` raised in `device.py` must still be caught by `main.py`. → Task 4, Step 1.
6. **Someone imports `main` from outside the repo's own tests.** Expected: every name in the old public surface is still reachable from `main`. Nothing pins the full list today — the suite imports 13 names and uses `main.<attr>` for 8 more. → Task 4, Step 1.

---

### Task 1: Extract the discharge curve into `battery.py`

**Files:**
- Create: `battery.py`
- Modify: `main.py` — delete the moved block, import from `battery`
- Modify: `tests/test_main.py` — add `import battery`, add one ownership test

**Interfaces:**
- Consumes: nothing. This is the bottom of the dependency graph.
- Produces: `battery.py` with `DEFAULT_DISCHARGE_CURVE`, `DISCHARGE_CURVE_ENV_VAR`, `_WARNED_CURVES`, `parse_discharge_curve(spec: str) -> tuple[tuple[int, int], ...]`, `load_discharge_curve() -> tuple[tuple[int, int], ...]`, `mv_to_percent(mv: int, curve: tuple[tuple[int, int], ...] | None = None) -> int`. `main.mv_to_percent` must be the same object as `battery.mv_to_percent`.

**Context you need:** the block is `main.py` lines 96-178 at `fa55f17` — `DISCHARGE_CURVE_ENV_VAR` through the end of `mv_to_percent`. `main.py` line 79-94 (`DEFAULT_DISCHARGE_CURVE` and its eleven-line warning comment) moves with it. `StrixDevice.query` at `main.py:307` calls `mv_to_percent(voltage_mv)` and `StrixDevice.stabilize_percentage` does not, so `main.py` needs one `from battery import mv_to_percent` to keep working. **No `patch("main.X")` site targets anything in this block**, which is why it goes first: it proves the move pattern at zero test risk.

- [ ] **Step 1: Write the failing test**

Add `import battery` after `import main` in `tests/test_main.py`, and add to `DischargeCurveTests`:

```python
    def test_the_discharge_curve_lives_in_the_battery_module(self):
        self.assertIs(main.mv_to_percent, battery.mv_to_percent)
        self.assertIs(main.load_discharge_curve, battery.load_discharge_curve)
        self.assertIs(main.parse_discharge_curve, battery.parse_discharge_curve)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run python -m unittest tests.test_main.DischargeCurveTests -v`
Expected: `ModuleNotFoundError: No module named 'battery'`. Correct red state.

- [ ] **Step 3: Create `battery.py`**

`git mv` is not available for an extracted block. Create `battery.py` and paste lines 79-101 and 104-178 of `main.py` verbatim, including the `DEFAULT_DISCHARGE_CURVE` warning comment and the `_WARNED_CURVES` comment. Add the imports the moved code needs: `import itertools`, `import os`, `import sys`.

- [ ] **Step 4: Delete the block from `main.py` and import instead**

In `main.py`, delete the moved lines and leave:

```python
from battery import mv_to_percent
```

Placed with the other imports at the top. `main.py` keeps re-exporting `mv_to_percent`, `parse_discharge_curve`, `load_discharge_curve`, `DEFAULT_DISCHARGE_CURVE` and `DISCHARGE_CURVE_ENV_VAR` — the tests import `mv_to_percent` from `main` directly, and the constraint above requires it.

- [ ] **Step 5: Run the full suite to verify it passes**

Run: `uv run python -m unittest discover -s tests`
Expected: `Ran 77 tests ... OK` (76 existing plus the new one). Every one of the 76 must pass unmodified — that is the proof the move was behaviour-preserving.

- [ ] **Step 6: Run the other three gates**

Run: `uv run --group dev ruff check . && uv run --group dev ruff format --check . && uv run --group dev ty check`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add battery.py main.py tests/test_main.py
git commit -m "refactor: move the discharge curve into battery.py"
```

---

### Task 2: Extract the HID device layer into `device.py`

**Files:**
- Create: `device.py`
- Modify: `main.py` — delete the moved block, import from `device`
- Modify: `tests/test_main.py` — add `import device`, retarget 46 `patch` sites

**Interfaces:**
- Consumes: `battery.mv_to_percent` from Task 1.
- Produces: `device.py` with `HIDIOCGRAWINFO`, `HIDIOCSFEATURE_64`, `HIDIOCGFEATURE_64`, `ASUS_VID`, `STRIX_PIDS`, `MAX_SLEEP_MINUTES`, `ERROR_OFFLINE`, `ERROR_PERMISSION`, `NOTIFY_TIMEOUT_SECONDS`, `BATTERY_DISPLAY_DEADBAND_PERCENT`, `LOW_BATTERY_NOTIFY_PERCENT`, `LOW_BATTERY_REARM_PERCENT`, `QUERY_PACKET`, `NOISE_REDUCTION_SUB_COMMAND`, `noise_reduction_state_path()`, `offline_message(last_error)`, `StrixDevice`. `main.StrixDevice` must be the same object as `device.StrixDevice`.

**Context you need:** this is the largest move — the ioctl constants at `main.py` 16-19, the ASUS identity block at 21-23, the error constants at 25-28, the tuning thresholds at 35-48, `QUERY_PACKET` and `NOISE_REDUCTION_SUB_COMMAND` at 50-60, `noise_reduction_state_path` at 63-67, `validate_sleep_minutes` at 70-76, and the whole `StrixDevice` class plus `offline_message` at 181-450. `HEADSET_ICON`, `SLEEP_ICON`, `NOISE_REDUCTION_ICON`, `NOISE_REDUCTION_ICON` and the notify comment stay behind for Task 3.

**The patch retargeting table.** Every one of these is `patch("main.<X>")` in `tests/test_main.py`, and every one moves to the new namespace:

| Old | New | Sites |
|---|---|---|
| `main.glob.glob` | `device.glob.glob` | 15 |
| `main.os.open` | `device.os.open` | 11 |
| `main.os.close` | `device.os.close` | 6 |
| `main.fcntl.ioctl` | `device.fcntl.ioctl` | 6 |
| `main.time.sleep` | `device.time.sleep` | 5 |
| `main.noise_reduction_state_path` | `device.noise_reduction_state_path` | 4 |
| `main.subprocess.run` | `device.subprocess.run` | 3 |

`os` is also used by `render_tray_icon` for `os.path.exists`, but no test patches `os.path`, so `os.open` and `os.close` belong to `device` alone.

- [ ] **Step 1: Write the failing tests**

Add `import device` after `import main`, and add a new class before `class SleepDurationTests`:

```python
class DeviceLayerTests(unittest.TestCase):
    """The HID layer must be reachable only through `device`."""

    def test_the_device_layer_lives_in_the_device_module(self):
        self.assertIs(main.StrixDevice, device.StrixDevice)
        self.assertIs(main.offline_message, device.offline_message)
        self.assertIs(main.noise_reduction_state_path, device.noise_reduction_state_path)

    def test_a_patched_state_path_is_actually_reached_by_the_constructor(self):
        # The one patch whose failure is invisible: if this patch does not
        # bite, StrixDevice reads the REAL ~/.config path and the suite still
        # goes green — it just writes to the developer's home directory.
        reached = []

        def fake_path():
            reached.append(1)
            raise OSError("no state file for this test")

        with patch("device.noise_reduction_state_path", side_effect=fake_path):
            main.StrixDevice()

        self.assertEqual(len(reached), 1, "the state path was never called")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run python -m unittest tests.test_main.DeviceLayerTests -v`
Expected: `ModuleNotFoundError: No module named 'device'`. Correct red state.

- [ ] **Step 3: Create `device.py`**

Create `device.py` and paste the blocks listed in Context, verbatim and in their current order. Add the imports they need: `import contextlib`, `import fcntl`, `import glob`, `import os`, `import pathlib`, `import struct`, `import subprocess`, `import sys`, `import time`, and `from battery import mv_to_percent`.

- [ ] **Step 4: Delete the blocks from `main.py` and import instead**

In `main.py`, delete the moved lines and leave this at the top with the other imports:

```python
from device import (
    BATTERY_DISPLAY_DEADBAND_PERCENT,
    ERROR_OFFLINE,
    ERROR_PERMISSION,
    HIDIOCGFEATURE_64,
    HIDIOCGRAWINFO,
    MAX_SLEEP_MINUTES,
    NOTIFY_TIMEOUT_SECONDS,
    StrixDevice,
    noise_reduction_state_path,
    offline_message,
    validate_sleep_minutes,
)
```

`main.py` keeps re-exporting every name above — the suite imports 8 of them directly from `main`.

- [ ] **Step 5: Retarget the patch sites**

Apply the table above to `tests/test_main.py`. It is a literal string substitution on `patch("main.` → `patch("device.` for those seven targets only. **Do not** touch `main.signal`; it belongs to Task 3.

- [ ] **Step 6: Run the full suite to verify it passes**

Run: `uv run python -m unittest discover -s tests`
Expected: `Ran 79 tests ... OK` (77 from Task 1, plus 2 new).

- [ ] **Step 7: Prove the state-file patch is still effective**

The suite going green is not proof for `device.noise_reduction_state_path`. Run the new test alone and confirm it fails when the target is wrong:

```bash
sed -i 's/patch("device.noise_reduction_state_path"/patch("main.noise_reduction_state_path"/' tests/test_main.py
uv run python -m unittest tests.test_main.DeviceLayerTests -v   # must FAIL
sed -i 's/patch("main.noise_reduction_state_path", side_effect=fake_path)/patch("device.noise_reduction_state_path", side_effect=fake_path)/' tests/test_main.py
uv run python -m unittest tests.test_main.DeviceLayerTests -v   # must PASS
```

Expected: the first run fails on `the state path was never called`, proving the assertion has teeth; the second passes. Revert the temporary sed before committing.

- [ ] **Step 8: Run the other three gates**

Run: `uv run --group dev ruff check . && uv run --group dev ruff format --check . && uv run --group dev ty check`
Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add device.py main.py tests/test_main.py
git commit -m "refactor: move the HID device layer into device.py"
```

---

### Task 3: Extract the tray presentation layer into `icon.py` and `tray.py`

**Files:**
- Create: `icon.py`, `tray.py`
- Modify: `main.py` — delete the moved blocks, import from both
- Modify: `tests/test_main.py` — add `import icon`, `import tray`, retarget 2 `patch("main.signal")` sites

**Interfaces:**
- Consumes: `device.StrixDevice` from Task 2.
- Produces:
  - `icon.py` — `render_tray_icon(percentage: int, charging: bool, connected: bool)`, `pil_to_qpixmap(pil_img)`. Neither is patched by any test.
  - `tray.py` — `HEADSET_ICON`, `SLEEP_ICON`, `NOISE_REDUCTION_ICON`, `_run_qt_app_with_sigint_shutdown(app, timer, before_exec=None)`, `run_pyqt_tray(device: StrixDevice)`, `start_tray(device: StrixDevice)`. `main.run_pyqt_tray` must be the same object as `tray.run_pyqt_tray`.
- `start_tray`'s signature and its single `except ImportError` around `run_pyqt_tray(device)` are unchanged. `run_pyqt_tray` stays in the same module as `start_tray`, so the `ImportError` still propagates across the `from tray import run_pyqt_tray` boundary inside `start_tray`'s own body.

**Context you need:** `main.py` lines 30-33 (the three glyph constants), `_run_qt_app_with_sigint_shutdown` at 565-577, `run_pyqt_tray` at 580-709, `start_tray` at 712-722, and `render_tray_icon` plus `pil_to_qpixmap` at 453-562. `icon.py` needs `import os` only; `tray.py` needs `import signal`, `import sys`, and `from device import StrixDevice`. Both import PyQt6 and Pillow *inside* their functions, as today — that lazy import is what makes the tests able to fake PyQt6, and moving it to module scope would break `install_fake_pyqt6`.

- [ ] **Step 1: Write the failing tests**

Add `import icon` and `import tray` after `import main`, and add a class before `class SleepDurationTests`:

```python
class TrayLayerTests(unittest.TestCase):
    """The tray presentation layer must be reachable only through `tray`/`icon`."""

    def test_the_tray_lives_in_its_own_modules(self):
        self.assertIs(main.run_pyqt_tray, tray.run_pyqt_tray)
        self.assertIs(main.start_tray, tray.start_tray)
        self.assertIs(main.render_tray_icon, icon.render_tray_icon)
        self.assertIs(main.pil_to_qpixmap, icon.pil_to_qpixmap)

    def test_a_patched_signal_reaches_the_shutdown_helper(self):
        # `patch("main.signal", fake)` rebinds a name in main's namespace. Once
        # the helper lives in tray.py that patch would apply and stub nothing.
        controller = FakeSignalController(None)
        with patch("tray.signal", controller):
            main._run_qt_app_with_sigint_shutdown(
                FakeQtApp(), FakeQtTimer(), None
            )
        self.assertTrue(
            controller.registrations, "the fake signal module was never installed"
        )
```

`FakeSignalController`, `FakeQtApp` and `FakeQtTimer` already exist in the file, below the insertion point. Move the new class **after** `QtSignalShutdownTests` so those names are already defined at class-definition time — or place it after `class FakeQtTimer` and before `class QtSignalShutdownTests`. The second option is simpler: put it there.

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run python -m unittest tests.test_main.TrayLayerTests -v`
Expected: `ModuleNotFoundError: No module named 'tray'`. Correct red state.

- [ ] **Step 3: Create `icon.py` and `tray.py`**

Create both files, pasting the blocks listed in Context verbatim. `icon.py` gets `import os` plus the two function-local `from PIL import ...` imports. `tray.py` gets `import signal`, `import sys`, `from device import StrixDevice`, the three glyph constants, and the function-local `from PyQt6...` imports unchanged.

- [ ] **Step 4: Delete the blocks from `main.py` and import instead**

In `main.py`, delete the moved lines and leave at the top:

```python
from icon import pil_to_qpixmap, render_tray_icon
from tray import (
    HEADSET_ICON,
    NOISE_REDUCTION_ICON,
    SLEEP_ICON,
    _run_qt_app_with_sigint_shutdown,
    run_pyqt_tray,
    start_tray,
)
```

`main.py` keeps re-exporting all of them.

- [ ] **Step 5: Retarget the signal patch**

In `tests/test_main.py`, replace both `patch("main.signal", ...)` with `patch("tray.signal", ...)`. Two sites, in `QtSignalShutdownTests`.

- [ ] **Step 6: Run the full suite to verify it passes**

Run: `uv run python -m unittest discover -s tests`
Expected: `Ran 81 tests ... OK` (79 from Task 2, plus 2 new).

- [ ] **Step 7: Prove the signal patch is still effective**

`QtSignalShutdownTests` already asserts on `controller.registrations`, so a dead patch fails loudly — confirm it by breaking the target on purpose:

```bash
sed -i 's/patch("tray.signal"/patch("main.signal"/' tests/test_main.py
uv run python -m unittest tests.test_main.QtSignalShutdownTests -v   # must FAIL
sed -i 's/patch("main.signal"/patch("tray.signal"/' tests/test_main.py
uv run python -m unittest tests.test_main.QtSignalShutdownTests -v   # must PASS
```

Expected: the first run fails on the registrations assertion; the second passes. Revert the temporary sed before committing.

- [ ] **Step 8: Run the other three gates**

Run: `uv run --group dev ruff check . && uv run --group dev ruff format --check . && uv run --group dev ty check`
Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add icon.py tray.py main.py tests/test_main.py
git commit -m "refactor: move the tray and icon layers out of main.py"
```

---

### Task 4: Reduce `main.py` to an entry point and pin the public surface

**Files:**
- Modify: `main.py` — keep only the imports, `main()`, and the re-exports
- Modify: `tests/test_main.py` — add one test pinning the whole public surface

**Interfaces:**
- Consumes: everything from Tasks 1-3.
- Produces: a `main.py` of roughly 120 lines whose only job is argparse and dispatch. `main.main()` remains the console entry point named in `[project.scripts]`.

**Context you need:** after Task 3, `main.py` holds the argparse block and `main()` from `main.py` lines 725-810 at `fa55f17`, plus a growing pile of re-export imports. Two things must survive: the `if __name__ == "__main__": main()` guard at the bottom, and every name the suite imports. Nothing in `pyproject.toml` changes.

- [ ] **Step 1: Write the failing test**

Add a class at the end of `tests/test_main.py`:

```python
class PublicSurfaceTests(unittest.TestCase):
    """`import main` is the documented entry point; its surface must not shrink."""

    def test_every_documented_name_is_still_reachable_from_main(self):
        for name in [
            "ERROR_OFFLINE", "ERROR_PERMISSION", "HEADSET_ICON",
            "HIDIOCGFEATURE_64", "HIDIOCGRAWINFO", "MAX_SLEEP_MINUTES",
            "NOTIFY_TIMEOUT_SECONDS", "SLEEP_ICON", "STRIX_PIDS",
            "StrixDevice", "load_discharge_curve", "main", "mv_to_percent",
            "noise_reduction_state_path", "offline_message",
            "parse_discharge_curve", "pil_to_qpixmap", "render_tray_icon",
            "run_pyqt_tray", "start_tray", "validate_sleep_minutes",
        ]:
            with self.subTest(name=name):
                self.assertTrue(hasattr(main, name), f"main lost {name}")
```

`subTest` so a failure names the missing name instead of stopping at the first.

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run python -m unittest tests.test_main.PublicSurfaceTests -v`
Expected: it **passes** at this point, because Tasks 1-3 already preserve the surface. That is a finding, not a formality: it means the re-export constraints in Tasks 1-3 held. Record it in the ledger and keep the test — it is the pin that stops a future change from quietly dropping a name. Do not delete it for passing early.

- [ ] **Step 3: Slim `main.py`**

Delete any re-export that no module, test, or the README references. Keep the argparse block, `main()`, the guard, and the imports. `main.py` must end up importing nothing from the stdlib except `argparse`, `json` and `sys` — if any other stdlib import remains, it is one of the six the Global Constraints forbid; delete it.

- [ ] **Step 4: Run the full suite to verify it passes**

Run: `uv run python -m unittest discover -s tests`
Expected: `Ran 82 tests ... OK` (81 from Task 3, plus 1 new).

- [ ] **Step 5: Verify both documented invocations by hand**

```bash
uv run python main.py --json
uv run strix-battery --json
```

Expected: both print `{"status": "offline", "text": "Headset offline"}` and exit `0`. The second resolves through `[project.scripts]`, which still says `main:main` and must keep working.

- [ ] **Step 6: Check the line counts**

```bash
wc -l main.py battery.py device.py icon.py tray.py
```

Expected: `main.py` well under 150 lines and the largest single module under 300. If any module is still over 300, that is a finding to report, not a reason to keep going.

- [ ] **Step 7: Run the other three gates**

Run: `uv run --group dev ruff check . && uv run --group dev ruff format --check . && uv run --group dev ty check`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "refactor: reduce main.py to an entry point and pin the public surface"
```

---

## Verification Record

Measured at `fa55f17`:

- `main.py` is 810 lines. The four extracted blocks are 79-178 (`battery`), 16-76 and 181-450 (`device`), 453-562 (`icon`), 30-33 and 565-722 (`tray`), leaving roughly 120 for the entry point.
- The suite has **52** `patch("main.…")` call sites across 8 distinct targets: `glob.glob` 15, `os.open` 11, `os.close` 6, `fcntl.ioctl` 6, `time.sleep` 5, `noise_reduction_state_path` 4, `subprocess.run` 3, `signal` 2.
- Six of the eight patch a *module object's attribute*, so they take effect from whichever module calls them; they break only because `main.py` would stop importing that module. Two — `signal` and `noise_reduction_state_path` — rebind a name in `main`'s namespace and would stop working entirely.
- `grep -c 'patch("main.' tests/test_main.py` must return `0` when this plan finishes.
- No test patches anything in the battery or icon layers, which is why both can move without a single call-site edit.
- `FakeSignalController`, `FakeQtApp` and `FakeQtTimer` are defined at `tests/test_main.py` lines 783-842, below `class SleepDurationTests` at line 771.

## Out of Scope

- **Turning this into a real package.** Explicitly rejected: the code stays a CLI tool and a tray on GitHub, and `pyproject.toml` keeps `py-modules = ["main"]`. See `docs/superpowers/plans/2026-09-28-strix-battery-package.md` for the packaging argument, which this plan does not act on.
- **CI.** `.github/` still does not exist. That is `docs/superpowers/plans/2026-09-28-test-harness-and-ci.md`'s Task 3 and it should land before this plan, not after — a suite that runs on every push is what makes a four-task refactor cheap to review.
- **Renaming `tests/test_main.py`** to match the new layout. Cosmetic; do it in a follow-up if at all.
- **Anything about the HID protocol, the discharge curve's calibration, or the CLI exit codes.** Those have their own plans.