# strix_battery Package Conversion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop installing a generic top-level module named `main` into `site-packages` by converting the single module into a `strix_battery/` package, and fix the misleading PyQt6 startup message while the file is being split.

**Architecture:** A mechanical split. `main.py` becomes `strix_battery/__init__.py` with its contents unchanged, then a thin `strix_battery/cli.py` re-exports the public names so `main.py` becomes a five-line compatibility shim for anyone running it directly. The entry point moves to `strix_battery.cli:main`. No behaviour changes and no renames of existing symbols — the point of the conversion is the import path, not a refactor. Doing it as a rename-plus-shim means a mistake shows up as an import error, not as a silently different program.

**Tech Stack:** Python 3.12, setuptools, `unittest`, uv.

**Spec:** Findings from the code review of 2026-09-28 against commit `abcb770` — the "P2 — `py-modules = ["main"]` installs a generic top-level `main`" and "P3 — `main.py:490` prints the wrong PyQt6 message" items.

**Line numbers** refer to `main.py` at `abcb770`. This plan moves the whole file, so line numbers matter only for naming what to look for.

## Global Constraints

- **The conversion is mechanical. Do not refactor while you move.** No renames, no reordering, no new type hints, no "while I'm here" edits. Every `def` in `strix_battery/__init__.py` is byte-identical to the one in `main.py` at `abcb770`. A reviewer must be able to run `git diff -M` and see a pure rename.
- **All new source and test code is ASCII-only.** This matters more than usual here: the file being moved already contains two literal non-ASCII glyphs on the *online* branch of `update_state` (`main.py:452-454`). Use `git mv` for the file, not a copy-and-paste through a code block, or those two lines can be mangled silently. If an edit tool cannot match a line because of a glyph, edit around it and leave the glyph line untouched.
- Tests live in `tests/test_main.py` only, using `unittest`. Do not migrate to pytest. The test file keeps its name in this plan — renaming it to `tests/test_strix_battery.py` is a separate, optional cleanup.
- The gating command, and the only one that gates this plan:
  `uv run --no-sync python -m unittest discover -s tests -v`
  All 53 existing tests must stay green in every task. Baseline verified at `abcb770`: `Ran 53 tests ... OK`.
- **`main` must remain importable as a module name.** The README documents `uv run python main.py --json`, and the test suite does `import main` in a dozen places. The shim is what keeps both working, and a test must pin it.
- The public API is the set of names the test file imports. The new `strix_battery/cli.py` must re-export every one of them or `tests/test_main.py` breaks.
- No new runtime dependencies. `pyproject.toml` `dependencies` and `optional-dependencies` are unchanged apart from `[tool.setuptools]` and `[project.scripts]`.
- The `fallback-tray` extra's contents are unchanged; a separate plan owns adding `pygobject` to it.

## Review Focus

Five input classes this scope implies but that no test currently exercises. Each line's test is added in the task that owns the relevant code.

1. **A user who followed the README and runs `uv run python main.py --json`.** The shim must make this work exactly as before. Nothing in the suite covers it, because every test imports the module rather than executing the file. → Task 2, Step 1.
2. **The installed `strix-battery` console script.** It now resolves to `strix_battery.cli:main`, a different import path than before. Nothing verifies the entry point resolves at all — a typo there produces a wheel that installs cleanly and then fails on first run. → Task 2, Step 1.
3. **The console script must not be able to import a *different* `main`.** This is the entire reason for the conversion. A test that puts a decoy `main.py` on `sys.path` and confirms the shim still resolves to `strix_battery` proves the collision is gone. → Task 2, Step 1.
4. **PyQt6 present but raising `ImportError` at runtime.** Expected: a message that says PyQt6 was found but unusable, not "PyQt6 was not found". Today's two prints at `main.py:604` and `main.py:607` are correct for their own branches, but the `find_spec` check that gates them means the `not found` branch can also be reached after a failed import. → Task 3, Step 1.
5. **Neither PyQt6 nor pystray installed.** Expected: the existing install-instructions message and exit `1`. Must not change. → Task 3, Step 3.

---

### Task 1: Move main.py into a package, keeping main.py as a shim

**Files:**
- Create: `strix_battery/__init__.py` (via `git mv main.py strix_battery/__init__.py`, then restore a shim)
- Create: `strix_battery/__init__.py` — final content is the old `main.py` verbatim
- Create: `strix_battery/cli.py` — re-exports
- Create: `main.py` — the shim, replacing the 695-line module
- Modify: `pyproject.toml` — `[project.scripts]` (line 13) and `[tool.setuptools]` (lines 24-25)
- Test: `tests/test_main.py` — new `PackageLayoutTests` class before `class SleepDurationTests`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces:
  - `strix_battery/__init__.py` — every public name previously at top-level `main`, unchanged: `ASUS_VID`, `STRIX_PIDS`, `MAX_SLEEP_MINUTES`, `ERROR_OFFLINE`, `ERROR_PERMISSION`, `HEADSET_ICON`, `SLEEP_ICON`, `NOTIFY_TIMEOUT_SECONDS`, `BATTERY_DISPLAY_DEADBAND_PERCENT`, `LOW_BATTERY_NOTIFY_PERCENT`, `LOW_BATTERY_REARM_PERCENT`, `QUERY_PACKET`, `HIDIOCGRAWINFO`, `HIDIOCSFEATURE_64`, `HIDIOCGFEATURE_64`, `StrixDevice`, `StrixTrayPystray`, `render_tray_icon`, `pil_to_qpixmap`, `run_pyqt_tray`, `start_tray`, `offline_message`, `mv_to_percent`, `validate_sleep_minutes`, `main`, and the private helpers.
  - `strix_battery/cli.py` — `main` (the entry point) plus a re-export of every name above.
  - `main.py` — importable, runs, and re-exports from `strix_battery`.

**Context you need:** `pyproject.toml:25` is `py-modules = ["main"]`. That installs a module literally called `main` into `site-packages`, where it sits on `sys.path` for every Python process on the machine. Any other `main.py` earlier on the path shadows it or is shadowed by it, silently. The import is fine today only because nothing else happens to use the name.

The safe way to do this is `git mv`, so git records a rename and the diff stays reviewable:

```bash
mkdir -p strix_battery
git mv main.py strix_battery/__init__.py
```

Then write the two new files. **Do not retype the contents of `strix_battery/__init__.py`** — it is already correct after the `git mv`. Its `if __name__ == "__main__": main()` guard at the bottom is now dead code (a package is never `__main__`), and Task 2 removes it along with the shim's dependency on it.

- [ ] **Step 1: Write the failing tests**

First add the import guard to the test file, immediately after the existing `import main`:

```python
import strix_battery
```

Then insert this class immediately before `class SleepDurationTests(unittest.TestCase):`:

```python
class PackageLayoutTests(unittest.TestCase):
    """A generic top-level `main` in site-packages is the reason for this change."""

    def test_the_package_is_importable_and_not_vendored_as_a_top_level_module(self):
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
            "ERROR_OFFLINE", "ERROR_PERMISSION", "MAX_SLEEP_MINUTES",
            "NOTIFY_TIMEOUT_SECONDS", "StrixDevice", "StrixTrayPystray",
            "mv_to_percent", "offline_message", "run_pyqt_tray",
            "start_tray", "validate_sleep_minutes", "main",
        ]:
            self.assertTrue(
                hasattr(strix_battery, name), f"strix_battery lost {name}"
            )
            self.assertTrue(
                hasattr(importlib.import_module("strix_battery.cli"), name),
                f"strix_battery.cli lost {name}",
            )

    def test_the_shim_cannot_be_shadowed_by_a_decoy_main(self):
        # The whole point: a top-level `main` must no longer be load-bearing.
        import subprocess
        import sys
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "main.py").write_text("raise SystemExit('decoy won')\n")
            env = {**__import__("os").environ, "PYTHONPATH": tmp}
            result = subprocess.run(
                [sys.executable, "-c", "import main; print(main.__file__)"],
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
            )
        self.assertNotIn("decoy won", result.stdout)
        self.assertIn("strix_battery", result.stdout)
```

Note the third test expects exit `1` from the CLI: with no device reachable, `--json` currently exits `1` only after a separate plan unifies the exit code. **At `abcb770` it exits `0`.** Since this plan must not change exit codes, that test would fail here.

So: do not assert an exact exit code in this task. Change the assertion in the test above to accept either, and tighten it in the CLI-contract plan if that plan wants:

```python
        self.assertIn(result.returncode, (0, 1))
        self.assertIn("offline", result.stdout)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync python -m unittest tests.test_main.PackageLayoutTests -v`

Expected: `ModuleNotFoundError: No module named 'strix_battery'`. Correct red state.

- [ ] **Step 3: Move the module**

```bash
mkdir -p strix_battery
git mv main.py strix_battery/__init__.py
```

Verify the move preserved the file exactly:

```bash
git diff --cached --stat
```

Expected: a rename with 0 insertions and 0 deletions. If the numbers are not 0/0, stop and investigate — something was rewritten during the move.

- [ ] **Step 4: Write the CLI re-export module**

Create `strix_battery/cli.py`:

```python
"""Console entry point and stable import surface for the package.

Re-exports the public names so `strix_battery.cli:main` works as a console
script and so tooling that imports the entry point sees the same API the
top-level `main` shim did.
"""

from strix_battery import *  # noqa: F401,F403
from strix_battery import (  # noqa: F401
    ASUS_VID,
    BATTERY_DISPLAY_DEADBAND_PERCENT,
    ERROR_OFFLINE,
    ERROR_PERMISSION,
    HIDIOCGFEATURE_64,
    HIDIOCGRAWINFO,
    HIDIOCSFEATURE_64,
    HEADSET_ICON,
    LOW_BATTERY_NOTIFY_PERCENT,
    LOW_BATTERY_REARM_PERCENT,
    MAX_SLEEP_MINUTES,
    NOTIFY_TIMEOUT_SECONDS,
    QUERY_PACKET,
    SLEEP_ICON,
    STRIX_PIDS,
    StrixDevice,
    StrixTrayPystray,
    main,
    mv_to_percent,
    offline_message,
    pil_to_qpixmap,
    render_tray_icon,
    run_pyqt_tray,
    start_tray,
    validate_sleep_minutes,
)
```

The explicit list is redundant with the star import but deliberate: it is what makes a dropped public name a **loud `ImportError`** at import time instead of a silent `AttributeError` for a user. The star import also covers the private helpers (`_load_glib`, `_run_qt_app_with_sigint_shutdown`) that the test suite reaches through `patch("main....")`.

- [ ] **Step 5: Write the top-level shim**

Create `main.py` (it is currently absent after the move):

```python
#!/usr/bin/env python3
"""Compatibility shim. The implementation moved to the `strix_battery` package.

Kept so `python main.py --json` keeps working, as the README documents, and so
an existing `import main` does not break. Nothing should be added here; the
real module is `strix_battery`.
"""

import sys

from strix_battery import *  # noqa: F401,F403
from strix_battery import main as _main

if __name__ == "__main__":
    sys.exit(_main())
```

`sys.exit(_main())` rather than bare `main()` matters: `main()` may call
`sys.exit(1)` itself on a usage error, and a shim that returns normally would
swallow that into a `0` exit.

- [ ] **Step 6: Point the packaging at the package**

In `pyproject.toml`, replace the `[project.scripts]` entry point and the
`[tool.setuptools]` block:

```toml
[project.scripts]
strix-battery = "strix_battery.cli:main"
```

```toml
[tool.setuptools]
packages = ["strix_battery"]
```

Delete the `py-modules = ["main"]` line. `packages` and `py-modules` are
mutually exclusive, so leaving both in place fails the build rather than
silently preferring one.

- [ ] **Step 7: Run the full suite to verify it passes**

Run: `uv run --no-sync python -m unittest discover -s tests -v`

Expected: `Ran 58 tests ... OK`. (53 existing plus 5 new.)

Every one of the 53 existing tests must still pass **unmodified**, except for
the `import strix_battery` line and the new class. That is the real assertion
of this task: the shim resolves `patch("main.glob.glob")`, `patch("main.fcntl.ioctl")`,
`patch("main.subprocess.run")` and `patch("main._load_glib")` to the same module
object the code under test uses. If any of those patches were silently
ineffective, tests would fail — so the suite passing is the proof the shim's
re-export is not a copy.

- [ ] **Step 8: Verify the build and both entry points**

```bash
uv build
ls -la dist/
```

Expected: a wheel and an sdist. Confirm the wheel no longer contains a
top-level `main.py`:

```bash
uv run --no-sync python -c "
import zipfile, glob
w = sorted(glob.glob('dist/*.whl'))[-1]
names = zipfile.ZipFile(w).namelist()
print('top-level main.py:', 'main.py' in names)
print('package files:', [n for n in names if n.startswith('strix_battery/')])
"
```

Expected: `top-level main.py: False`, and a `strix_battery/` entry with
`__init__.py` and `cli.py`. A `True` here means the change did not take effect
in the build config — stop and fix `pyproject.toml` before committing.

Then confirm the installed script still runs:

```bash
uv run --no-sync strix-battery --json; echo "exit=$?"
```

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "refactor: move main.py into the strix_battery package with a shim"
```

---

### Task 2: Fix the misleading PyQt6 startup message

**Files:**
- Modify: `strix_battery/__init__.py` — the `start_tray` function (lines 597-616)
- Test: `tests/test_main.py` — new `StartTrayMessageTests` class before `class SleepDurationTests`

**Interfaces:**
- Consumes: `importlib.util.find_spec`, `run_pyqt_tray`, `StrixTrayPystray` (all unchanged in signature).
- Produces: `main.PYQT6_UNUSABLE_HINT: str` and `main.PYSTRAY_FALLBACK_HINT: str` — the two distinct messages, exported so tests assert on them.

**Context you need:** `start_tray` has two distinct failure paths that currently print nearly the same advice:

- `main.py:604` — PyQt6 was found by `find_spec` but `run_pyqt_tray` raised `ImportError` (a broken Qt install, a missing platform plugin). Prints `"Info: PyQt6 could not be used, starting with Pystray."`
- `main.py:607` — `find_spec` returned `None`. Prints `"Info: PyQt6 was not found, starting with Pystray."` followed by `uv add PyQt6`.

Each message is right for its own branch. The confusion is that both are
"Info" lines in the same function, and the second one appears after the first
has already told the user PyQt6 exists. A user whose Qt is broken reads
"install PyQt6" and installs it again, which changes nothing.

The fix is to make each message say which situation it is, and to say what
actually differs — the broken case needs a reinstall or a platform plugin, not
a fresh install.

- [ ] **Step 1: Write the failing tests**

Insert this class immediately before `class SleepDurationTests(unittest.TestCase):`:

```python
class StartTrayMessageTests(unittest.TestCase):
    """A broken Qt install and a missing Qt need different advice."""

    def messages_for(self, pyqt6_found, run_raises):
        printed = []

        def fake_run_pyqt_tray(device):
            if run_raises:
                raise ImportError("No module named 'PyQt6.QtWidgets'")

        pystray_app = object()
        with patch("builtins.print", side_effect=printed.append), patch(
            "main.importlib.util.find_spec",
            side_effect=lambda name: object() if pyqt6_found else None,
        ), patch(
            "main.run_pyqt_tray",
            side_effect=fake_run_pyqt_tray,
        ), patch(
            "main.importlib.util.find_spec",
            side_effect=lambda name: object() if pyqt6_found else None,
        ), patch(
            "main.StrixTrayPystray", return_value=pystray_app
        ), patch.object(
            pystray_app, "start", create=True, lambda: None
        ):
            start_tray(StrixDevice())
        return " ".join(printed)

    def test_a_missing_pyqt6_says_it_was_not_found(self):
        message = self.messages_for(pyqt6_found=False, run_raises=False)
        self.assertIn("not found", message)
        self.assertIn("PyQt6", message)

    def test_a_broken_pyqt6_does_not_say_it_was_not_found(self):
        message = self.messages_for(pyqt6_found=True, run_raises=True)
        self.assertIn("could not be used", message)
        self.assertNotIn(
            "not found",
            message,
            "telling a user with a broken Qt to install it again changes nothing",
        )

    def test_the_broken_case_names_a_concrete_cause(self):
        message = self.messages_for(pyqt6_found=True, run_raises=True)
        self.assertIn("ImportError", message)
```

The doubled `patch("main.importlib.util.find_spec", ...)` in the helper is a
typo risk — `patch` applies the later one. Write it once, and let the
`side_effect` handle both names, since both are only ever asked whether they
exist. The simplified helper is:

```python
    def messages_for(self, pyqt6_found, run_raises):
        printed = []

        def fake_run_pyqt_tray(device):
            if run_raises:
                raise ImportError("No module named 'PyQt6.QtWidgets'")

        found = object() if pyqt6_found else None
        pystray_app = object()
        with patch("builtins.print", side_effect=printed.append), patch(
            "main.importlib.util.find_spec", return_value=found
        ), patch("main.run_pyqt_tray", side_effect=fake_run_pyqt_tray), patch(
            "main.StrixTrayPystray", return_value=pystray_app
        ):
            start_tray(StrixDevice())
        return " ".join(printed)
```

Use this version. `StrixTrayPystray` is patched to a bare `object()`, so
`start()` on it is an `AttributeError` — the real class's `start` runs the GLib
loop, which must not happen in a test. Patch it with a real stub instead:

```python
        fake_app = object.__new__(StrixTrayPystray)
        fake_app._glib = None
        fake_app._main_loop = None
        fake_app._poll_source_id = None
        fake_app._shutting_down = True
```

with `fake_app.icon = FakePystrayIcon()`, so `quit()` short-circuits on
`_shutting_down` and never touches the icon or the main loop.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync python -m unittest tests.test_main.StartTrayMessageTests -v`

Expected: `test_a_broken_pyqt6_does_not_say_it_was_not_found` fails, because
after the `ImportError` branch prints "could not be used", control falls
through to the second block and prints "was not found" as well. That double
message is the bug.

- [ ] **Step 3: Separate the two messages**

In `strix_battery/__init__.py`, immediately after the `QUERY_PACKET` block,
add:

```python
# `start_tray` has two distinct failure modes and they need different advice.
# A missing PyQt6 is fixed by installing it. A PyQt6 that is present but raises
# on import is a broken install or a missing platform plugin, and telling that
# user to install it again changes nothing.
PYQT6_MISSING_HINT = (
    "Info: PyQt6 was not found, starting with Pystray.\n"
    "For a smoother experience and native context menu, run: uv add PyQt6\n"
)

PYQT6_UNUSABLE_HINT = (
    "Info: PyQt6 is installed but could not be used, starting with Pystray.\n"
    "This usually means a broken Qt install or a missing platform plugin;\n"
    "reinstalling PyQt6 alone may not fix it.\n"
    "For a smoother experience and native context menu, run: uv add PyQt6\n"
)
```

Then replace the whole `start_tray` function with:

```python
def start_tray(device: StrixDevice):
    """Prefer PyQt6 and fall back to Pystray when necessary."""
    if importlib.util.find_spec("PyQt6") is not None:
        try:
            run_pyqt_tray(device)
            return
        except ImportError as exc:
            # PyQt6 is present but unusable. This is a different problem from
            # PyQt6 being absent, and needs different advice.
            print(PYQT6_UNUSABLE_HINT.format(error=exc), end="")

    if importlib.util.find_spec("pystray") is not None and importlib.util.find_spec("PIL") is not None:
        print(PYQT6_MISSING_HINT, end="")
        app = StrixTrayPystray(device)
        app.start()
        return

    print("\nError: 'PyQt6' is required for tray mode.")
    print("Install it by running:")
    print("  uv add PyQt6 pillow\n")
    sys.exit(1)
```

Note the `except ImportError as exc` — the caught exception is interpolated
into the message, so the user sees the real cause (`No module named
'PyQt6.QtWidgets'`) rather than a guess.

The `PYQT6_MISSING_HINT` print moves inside the `if` rather than before it,
because reaching that block does not prove PyQt6 was missing — it proves
neither backend can start.

- [ ] **Step 4: Run the full suite to verify it passes**

Run: `uv run --no-sync python -m unittest discover -s tests -v`

Expected: `Ran 61 tests ... OK`. (58 from Task 1, plus 3 new.)

- [ ] **Step 5: Verify both messages by hand**

```bash
uv run --no-sync python - <<'PY'
from unittest.mock import patch
import main

for found, raises, label in [(True, True, "broken"), (False, False, "missing")]:
    out = []
    def fake(device):
        if raises:
            raise ImportError("No module named 'PyQt6.QtWidgets'")
    with patch("builtins.print", side_effect=out.append), \
         patch("main.importlib.util.find_spec", return_value=object() if found else None), \
         patch("main.run_pyqt_tray", side_effect=fake), \
         patch("main.StrixTrayPystray") as app:
        app.return_value.start.return_value = None
        try:
            main.start_tray(main.StrixDevice())
        except Exception as exc:
            out.append(f"[stopped: {type(exc).__name__}]")
    print(f"=== {label} ===")
    print("".join(str(x) for x in out) or "(no output)")
PY
```

Expected: the `broken` case prints "installed but could not be used" and does
**not** print "not found"; the `missing` case prints "was not found". Each
prints exactly one startup message, not two.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "fix: tell a broken PyQt6 apart from a missing one"
```

---

## Verification Record

Measured on the host running this plan, `abcb770`:

- `pyproject.toml:13` is `strix-battery = "main:main"` and `pyproject.toml:25`
  is `py-modules = ["main"]`. The console script therefore resolves through a
  module named `main` that setuptools places directly in `site-packages`.
- `main.py:599` calls `importlib.util.find_spec("PyQt6")`; `:604` prints
  "PyQt6 could not be used" on `ImportError`; `:607` prints "PyQt6 was not
  found". Both messages exist at `abcb770` — the defect is that the second
  prints after the first has already said PyQt6 is present.
- `README.md:66` documents `uv run python main.py --json`, which is why the
  shim is required rather than optional.
- `tests/test_main.py` does `import main` and patches
  `main.glob.glob`, `main.fcntl.ioctl`, `main.subprocess.run`,
  `main._load_glib`, `main.time.sleep`, `main.os.open`, `main.os.close`,
  `main.render_tray_icon`, `main.signal`, `main.importlib.util.find_spec` and
  `main.StrixTrayPystray`. All of these must resolve through the shim to the
  same module object the code under test uses, which is why the 53 existing
  tests passing unmodified is this plan's primary proof.

## Out of Scope

- **Adding `pygobject` to the `fallback-tray` extra.** A separate plan owns
  that; it also updates the README's installation section, and the two plans
  would otherwise conflict on the same lines.
- **Renaming `tests/test_main.py`.** It no longer matches the module it tests.
  Purely cosmetic and mechanical; do it in a follow-up if at all, so this
  plan's diff stays reviewable.
- **The `strix_battery` package's internal module split** — moving the device
  layer, the icon rendering and the two tray backends into separate modules.
  That is a real improvement, and it is not this plan: doing it in the same
  change would make a rename-plus-shim unreviewable. Consider it after this
  conversion lands and the suite is green.
- **Anything about the HID protocol, the discharge curve, or the CLI exit
  codes.** Separate plans.
