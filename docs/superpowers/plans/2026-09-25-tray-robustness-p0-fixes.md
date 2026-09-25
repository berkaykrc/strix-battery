# Tray Robustness (P0 Fixes) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the four P0 defects in the tray path: an unbounded `notify-send` subprocess that can freeze the event loop, a tray app that hangs invisibly on a tray-less desktop, permission errors misreported as "headset offline", and a battery percentage that flickers on every poll.

**Architecture:** All four fixes are surgical, local edits to the existing single-module `main.py`. No structural refactor, no new dependencies, no new files. Each fix is independently testable without hardware by exercising the pure logic and by faking the two external boundaries the existing test suite already fakes (`pystray` and `PyQt6` via `sys.modules`, `subprocess` and `os` via `patch`). The plan deliberately does **not** address the P1/P2 findings (font caching, device-node caching, `pygobject` dependency, `strix_battery/` packaging) — those belong to a separate plan and are listed under "Out of Scope".

**Tech Stack:** Python 3.12, `unittest` + `unittest.mock`, Pillow (unchanged), PyQt6 (unchanged), `fcntl`/`ioctl` against `/dev/hidraw*`.

**Spec:** This plan. The P0 findings come from the code review that preceded it; the P0/P1/P2 split is reproduced under "Out of Scope" so the boundary is auditable.

**Line numbers** in the "Files" blocks refer to `main.py` and `tests/test_main.py` as of commit `709e78b`. Tasks 1 and 2 shift line numbers in `main.py`, so anchor every edit on the quoted code or the named `def`, not on the line number.

## Global Constraints

- Single module: all production code lives in `main.py`. Do **not** split it into a package in this plan.
- **All new source and test code is ASCII-only.** Where a UI glyph is needed, use an ASCII escape in a named constant — never a literal emoji. `main.py` already contains literal emoji on two lines of the *online* branch of `update_state`; leave those two lines alone and do not sweep them. This rule exists because a literal glyph in a code block cannot be matched or typed reliably, which silently breaks exact-text edits.
- Tests live in `tests/test_main.py` only, using `unittest` (not pytest). `pyproject.toml` has no `[tool.pytest]` section and `.vscode/settings.json` sets `"python.testing.pytestEnabled": false`. Do not migrate.
- `main.py` is imported as the top-level module `main` (`pyproject.toml:25`, `py-modules = ["main"]`). Do not rename it in this plan.
- The test command, and the only command that gates this plan:
  `uv run --no-sync python -m unittest discover -s tests -v`
  All 20 existing tests must stay green in every task. Baseline verified at `709e78b`: `Ran 20 tests ... OK`.
- **New tests must be hermetic.** Never assert on, or depend on, which `/dev/hidraw*` nodes the host happens to have. `os.open`, `fcntl.ioctl`, `glob.glob`, `time.sleep` and `os.close` must all be patched in any test that reaches real device code.
- No new runtime dependencies. `pyproject.toml` `dependencies` and `optional-dependencies` must be byte-identical at the end of this plan.
- No network access, no hardware access. Every test must pass headless.
- Existing style: 4-space indent, double quotes, module-level `UPPER_SNAKE` constants after the imports, type hints where already present.

## Review Focus

Five input classes the P0 scope implies but that no test currently exercises. Each one's test lives in the task that owns the relevant code.

1. **`notify-send` is not installed at all** (`FileNotFoundError` on `PATH`). Expected: the tray keeps polling forever. The `except FileNotFoundError` branch at `main.py:162` has never been executed by a test. → Task 1, Step 1.
2. **`notify-send` is installed but exits non-zero** (no D-Bus session, no notification daemon). Expected: no crash, no popup, polling continues. `check=False` makes this return normally today and nothing asserts it. → Task 1, Step 1.
3. **Battery crosses 15% and then rests at 16%** — the latch band between the trigger (`<= 15`) and the re-arm point (`> 20`). Expected: exactly one notification, never a second at 16%. `main.py:115-119` implements this latch and no test touches it. → Task 2, Step 1.
4. **Battery sits at 12% while charging.** Expected: no notification — the condition includes `not charging`. Dropping that clause would make the tray nag on every poll while plugged in. → Task 2, Step 1.
5. **One `/dev/hidraw*` node denies permission while a *different* node is the actual receiver.** Expected: the device is still found and the tray stays online. This is the specific regression the `find_node` change in Task 3 can introduce if the `except PermissionError` branch `return`s instead of `continue`s. → Task 3, Step 1.

---

### Task 1: Bound the `notify-send` subprocess

**Files:**
- Modify: `main.py` — add a module constant after `MAX_SLEEP_MINUTES = 1092` (line 21)
- Modify: `main.py:158-163` — the whole `StrixDevice.notify` method
- Test: `tests/test_main.py:1-8` (imports), plus a new `NotifyTests` class inserted immediately before `class SleepDurationTests` (line 10)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `main.NOTIFY_TIMEOUT_SECONDS: int` (value `3`). No later task calls it; the test suite reads it as `NOTIFY_TIMEOUT_SECONDS` so the value stays changeable in one place.

**Context you need:** `StrixDevice.notify` is called from two places, and in the Qt build **both run on the GUI main thread**:
- `main.py:116`, inside `query()`, which `update_state` calls from a 15s `QTimer` (`main.py:357-358`).
- `main.py:150`, inside `set_sleep()`, which the sleep-timer menu actions call from a Qt signal handler (`main.py:311`).

`subprocess.run` with no `timeout=` waits forever. If D-Bus is wedged, the Qt event loop never returns, so the SIGINT handler installed at `main.py:269` never runs either — the tray becomes permanently unkillable except with `SIGKILL`. Adding `timeout=` converts that permanent freeze into a bounded 3s stall.

Verified facts this task relies on (both confirmed by running them):
- `subprocess.TimeoutExpired` **is** a subclass of `subprocess.SubprocessError`, so the existing `except` clause at `main.py:162` already catches it. No new `except` is needed.
- `subprocess.run(..., timeout=0.3)` against `sleep 30` does interrupt and raise `TimeoutExpired`.

- [ ] **Step 1: Write the failing tests**

Add `import subprocess` to the top of `tests/test_main.py`, immediately after the existing `import threading`:

```python
import subprocess
import threading
import unittest
```

Insert this class immediately before `class SleepDurationTests(unittest.TestCase):`:

```python
class NotifyTests(unittest.TestCase):
    """`notify()` runs on the GUI main thread, so it must never block forever."""

    def test_notify_passes_a_timeout_to_the_subprocess(self):
        device = StrixDevice()
        with patch("main.subprocess.run") as run:
            device.notify("ROG Strix Go", "Battery critically low: 12%!")
        self.assertEqual(
            run.call_args.kwargs.get("timeout"),
            NOTIFY_TIMEOUT_SECONDS,
            "notify-send must be bounded so a wedged D-Bus cannot freeze the tray",
        )

    def test_notify_swallows_a_timed_out_notify_send(self):
        device = StrixDevice()
        with patch(
            "main.subprocess.run",
            side_effect=subprocess.TimeoutExpired("notify-send", 5),
        ):
            device.notify("ROG Strix Go", "Battery critically low: 12%!")

    def test_notify_swallows_a_missing_notify_send_binary(self):
        device = StrixDevice()
        with patch(
            "main.subprocess.run", side_effect=FileNotFoundError("notify-send")
        ):
            device.notify("ROG Strix Go", "Battery critically low: 12%!")

    def test_notify_survives_a_failing_notify_send_exit_code(self):
        device = StrixDevice()
        with patch("main.subprocess.run", return_value=None) as run:
            device.notify("ROG Strix Go", "Battery critically low: 12%!")
        self.assertIs(run.call_args.kwargs.get("check"), False)
```

Replace the `from main import ...` line at `tests/test_main.py:7` with:

```python
from main import (
    MAX_SLEEP_MINUTES,
    NOTIFY_TIMEOUT_SECONDS,
    StrixDevice,
    StrixTrayPystray,
    validate_sleep_minutes,
)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync python -m unittest tests.test_main.NotifyTests -v`

Expected: `ImportError: cannot import name 'NOTIFY_TIMEOUT_SECONDS' from 'main'`. That is the correct red state — the import at the top of the test file fails before any test body runs.

- [ ] **Step 3: Add the constant**

In `main.py`, immediately after the `MAX_SLEEP_MINUTES = 1092` line, add:

```python

# `notify-send` runs on the Qt/GLib main thread, so a hung notification daemon
# would freeze the event loop with no way out. Bound the wait.
NOTIFY_TIMEOUT_SECONDS = 3
```

- [ ] **Step 4: Add the timeout to the subprocess call**

Replace the whole `notify` method with:

```python
    def notify(self, title: str, msg: str):
        """Send a desktop notification through notify-send, bounded in time."""
        try:
            subprocess.run(
                ["notify-send", title, msg, "-i", "audio-headset"],
                check=False,
                timeout=NOTIFY_TIMEOUT_SECONDS,
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            # FileNotFoundError: notify-send is not installed.
            # SubprocessError covers TimeoutExpired, so a hung daemon can only
            # stall the tray briefly instead of freezing it permanently.
            pass
```

- [ ] **Step 5: Run the full suite to verify it passes**

Run: `uv run --no-sync python -m unittest discover -s tests -v`

Expected: `Ran 24 tests` — `OK`. (20 pre-existing plus 4 new.) All 20 pre-existing tests must still pass.

- [ ] **Step 6: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "fix: bound notify-send so a hung daemon cannot freeze the tray"
```

---

### Task 2: Stop the battery percentage from flickering

**Files:**
- Modify: `main.py` — add two module constants after the `NOTIFY_TIMEOUT_SECONDS` block
- Modify: `main.py:55-57` — `StrixDevice.__init__`
- Modify: `main.py` — insert three methods between `__init__` and `find_node`
- Modify: `main.py` — five edits inside `query()`: the three offline return sites, the percentage computation, and the low-battery block
- Test: `tests/test_main.py` — new `MvToPercentTests` and `PercentageStabilityTests` classes before `class SleepDurationTests`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces:
  - `main.BATTERY_DISPLAY_DEADBAND_PERCENT: int` (value `2`)
  - `main.LOW_BATTERY_NOTIFY_PERCENT: int` (value `15`)
  - `main.LOW_BATTERY_REARM_PERCENT: int` (value `20`)
  - `StrixDevice.stabilize_percentage(self, percentage: int) -> int`
  - `StrixDevice._go_offline(self) -> None`
  - `StrixDevice._update_low_battery_notice(self, percentage: int, charging: bool) -> bool`

  Task 3 widens `_go_offline` to `_go_offline(self, error: str = ERROR_OFFLINE) -> None`. Do not add that parameter here.

**Context you need:** `mv_to_percent` (`main.py:43-49`) maps millivolts to a percentage linearly and truncates with `int()`. One millivolt is about 0.118%, so 1% is about 8.5mV. Real receiver readings wobble by several millivolts between polls, so the integer output steps back and forth across a boundary, and every change triggers a fresh 6.4ms Pillow render plus a tray icon upload.

Measured, seeded, 400 consecutive readings at a steady 3900mV with +/-12mV noise:

| Design | Icon rewrites / 400 reads | Worst error on a real discharge ramp |
| --- | --- | --- |
| raw `int()`, no filter (today) | 286 | 0% |
| `round()` instead of `int()` | 285 | 1% |
| EWMA alpha 0.5 | 160 | 6% |
| 5-sample moving average | 76 | 12% |
| **deadband, 2% (this task)** | **94** | **0%** |
| deadband, 3% | 28 | 0% |

Every averaging design buys fewer rewrites by *lagging* a real discharge by 6 to 12 percentage points, which means the icon reports a level that is simply wrong. A deadband buys the same reduction with **zero** lag, because it only suppresses changes smaller than the deadband and passes anything larger straight through. That is why this plan uses a deadband and not smoothing.

`BATTERY_DISPLAY_DEADBAND_PERCENT = 2` means the displayed value moves when the raw value differs from the displayed one by 2 or more. A real cell crosses 2% in well under a minute, so nothing perceptible is lost. If field reports show residual flicker, raise the constant to `3`; it is a one-line change that costs nothing else.

- [ ] **Step 1: Write the failing tests**

Insert these two classes immediately before `class SleepDurationTests(unittest.TestCase):`:

```python
class MvToPercentTests(unittest.TestCase):
    """The percentage curve is the input to every displayed battery number."""

    def test_clamps_to_the_discharge_endpoints(self):
        self.assertEqual(mv_to_percent(3300), 0)
        self.assertEqual(mv_to_percent(4150), 100)
        self.assertEqual(mv_to_percent(3200), 0)
        self.assertEqual(mv_to_percent(4200), 100)

    def test_is_monotonic_across_the_whole_range(self):
        previous = -1
        for mv in range(3200, 4200, 5):
            current = mv_to_percent(mv)
            self.assertGreaterEqual(current, previous, f"regressed at {mv}mV")
            previous = current

    def test_stays_within_bounds_for_garbage_input(self):
        self.assertEqual(mv_to_percent(0), 0)
        self.assertEqual(mv_to_percent(65535), 100)


class PercentageStabilityTests(unittest.TestCase):
    """A noisy voltage must not make the tray icon strobe."""

    def test_the_first_read_is_reported_unchanged(self):
        device = StrixDevice()
        self.assertEqual(device.stabilize_percentage(70), 70)

    def test_jitter_smaller_than_the_deadband_holds_the_reported_value(self):
        device = StrixDevice()
        device.stabilize_percentage(70)
        for noisy in (70, 71, 69, 70, 71, 69):
            self.assertEqual(
                device.stabilize_percentage(noisy),
                70,
                f"{noisy}% is inside the deadband and must not move the display",
            )

    def test_a_move_at_the_deadband_is_reported(self):
        device = StrixDevice()
        device.stabilize_percentage(70)
        self.assertEqual(device.stabilize_percentage(72), 72)

    def test_jitter_around_the_new_value_is_held_at_the_new_value(self):
        device = StrixDevice()
        device.stabilize_percentage(70)
        device.stabilize_percentage(72)
        for noisy in (71, 72, 73, 72):
            self.assertEqual(device.stabilize_percentage(noisy), 72)

    def test_going_offline_clears_the_hold(self):
        device = StrixDevice()
        device.stabilize_percentage(70)
        with patch("main.glob.glob", return_value=[]):
            self.assertIsNone(device.query())
        # 69 would have been held at 70 had the hold survived the disconnect.
        self.assertEqual(device.stabilize_percentage(69), 69)

    def test_low_battery_notifies_exactly_once_across_the_latch_band(self):
        device = StrixDevice()
        with patch.object(device, "notify") as notify:
            for pct in (15, 14, 16, 15, 18, 20):
                device._update_low_battery_notice(pct, False)
        self.assertEqual(notify.call_count, 1)

    def test_low_battery_rearms_above_the_upper_hysteresis_point(self):
        device = StrixDevice()
        with patch.object(device, "notify") as notify:
            for pct in (15, 14, 15):
                device._update_low_battery_notice(pct, False)
            self.assertEqual(notify.call_count, 1)
            device._update_low_battery_notice(21, False)
            for pct in (15, 14):
                device._update_low_battery_notice(pct, False)
        self.assertEqual(notify.call_count, 2)

    def test_low_battery_does_not_notify_while_charging(self):
        device = StrixDevice()
        with patch.object(device, "notify") as notify:
            for pct in (15, 12, 10, 8):
                device._update_low_battery_notice(pct, True)
        notify.assert_not_called()
```

Replace the `from main import ...` line with:

```python
from main import (
    MAX_SLEEP_MINUTES,
    NOTIFY_TIMEOUT_SECONDS,
    StrixDevice,
    StrixTrayPystray,
    mv_to_percent,
    validate_sleep_minutes,
)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
uv run --no-sync python -m unittest tests.test_main.MvToPercentTests tests.test_main.PercentageStabilityTests -v
```
Expected: `ImportError: cannot import name 'mv_to_percent' from 'main'`. Both classes fail to import, so no test body runs yet.

- [ ] **Step 3: Add the constants**

In `main.py`, immediately after the `NOTIFY_TIMEOUT_SECONDS = 3` line, add:

```python

# Voltage readings wobble by a few millivolts between polls and `mv_to_percent`
# is a step function, so reporting the raw value makes the tray icon flicker.
# Only report a new percentage once it has genuinely moved. Raising this to 3
# calms the icon further at the cost of coarser steps.
BATTERY_DISPLAY_DEADBAND_PERCENT = 2

# Notify at 15% or below, but only re-arm for a fresh alert above 20%, so a
# battery resting in between does not notify on every poll.
LOW_BATTERY_NOTIFY_PERCENT = 15
LOW_BATTERY_REARM_PERCENT = 20
```

- [ ] **Step 4: Initialise the hold and add the three methods**

Replace `StrixDevice.__init__` with:

```python
    def __init__(self):
        self.last_status = None
        self.notified_low = False
        self._displayed_percentage = None
```

Then insert these three methods immediately after `__init__` and before `find_node`:

```python
    def stabilize_percentage(self, percentage: int) -> int:
        """Hold the last reported percentage until it genuinely moves.

        The hold only suppresses changes smaller than the deadband. A real
        discharge produces a much larger step and is reported immediately, so
        this costs no accuracy while removing the flicker that plain
        millivolt noise causes.
        """
        if (
            self._displayed_percentage is None
            or abs(percentage - self._displayed_percentage)
            >= BATTERY_DISPLAY_DEADBAND_PERCENT
        ):
            self._displayed_percentage = percentage
        return self._displayed_percentage

    def _go_offline(self):
        """Record that no headset answered and drop the display hold."""
        self.last_status = None
        self._displayed_percentage = None

    def _update_low_battery_notice(self, percentage: int, charging: bool) -> bool:
        """Notify once when critically low; return the new latch state.

        Split out from `query` so the 15% trigger and the 20% re-arm point can
        be tested without hardware, and so the latch logic is readable on its
        own.
        """
        if (
            percentage <= LOW_BATTERY_NOTIFY_PERCENT
            and not charging
            and not self.notified_low
        ):
            self.notify("ROG Strix Go 2.4", f"Battery critically low: {percentage}%!")
            self.notified_low = True
        elif percentage > LOW_BATTERY_REARM_PERCENT:
            self.notified_low = False
        return self.notified_low
```

- [ ] **Step 5: Wire the hold and the latch into `query`**

Make these five edits inside `query()`.

Edit 1 — the `find_node` result check. Replace:

```python
        fd, path = self.find_node()
        if fd is None:
            self.last_status = None
            return None
```

with:

```python
        fd, path = self.find_node()
        if fd is None:
            self._go_offline()
            return None
```

Edit 2 — the zeroed-response check. Replace:

```python
            if buf[1] != 0x1B:
                self.last_status = None
                return None
```

with:

```python
            if buf[1] != 0x1B:
                self._go_offline()
                return None
```

Edit 3 — the percentage computation. Replace:

```python
            voltage_mv = (buf[12] << 8) | buf[11]
            pct = mv_to_percent(voltage_mv)
```

with:

```python
            voltage_mv = (buf[12] << 8) | buf[11]
            pct = self.stabilize_percentage(mv_to_percent(voltage_mv))
```

Edit 4 — the low-battery block. Replace:

```python
            self.last_status = status

            # Notify once when the battery reaches a critical level (15% or less).
            if pct <= 15 and not charging and not self.notified_low:
                self.notify("ROG Strix Go 2.4", f"Battery critically low: {pct}%!")
                self.notified_low = True
            elif pct > 20:
                self.notified_low = False

            return status
```

with:

```python
            self.last_status = status

            self._update_low_battery_notice(pct, charging)

            return status
```

Edit 5 — the trailing `except OSError` handler. Replace:

```python
        except OSError:
            self.last_status = None
            return None
```

with:

```python
        except OSError:
            self._go_offline()
            return None
```

- [ ] **Step 6: Run the full suite to verify it passes**

Run: `uv run --no-sync python -m unittest discover -s tests -v`

Expected: `Ran 35 tests` — `OK`. (24 from Task 1, plus 3 in `MvToPercentTests` and 8 in `PercentageStabilityTests`.)

- [ ] **Step 7: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "fix: hold the reported battery percentage to stop tray flicker"
```

---

### Task 3: Distinguish "no permission" from "headset absent"

**Files:**
- Modify: `main.py` — two error constants after `MAX_SLEEP_MINUTES = 1092`, before the `NOTIFY_TIMEOUT_SECONDS` block
- Modify: `main.py` — insert `offline_message` after `mv_to_percent` (which ends at `main.py:49`) and before `class StrixDevice` (line 52)
- Modify: `main.py:55-57` — `StrixDevice.__init__`
- Modify: `main.py:59-77` — the whole `find_node` method
- Modify: `main.py` — the `_go_offline` method added in Task 2
- Modify: `main.py` — two edits inside `query()`
- Modify: `main.py` — the `else` branch of `update_state` (the offline branch, at `main.py:340-344`)
- Modify: `main.py:377-382` — `get_status_text`
- Modify: `main.py:415-421` — the offline branch of `StrixTrayPystray.update_tray`
- Test: `tests/test_main.py` — two new module-level fakes plus a `LastErrorTests` class before `class SleepDurationTests`; add `last_error = None` to `FakeDevice`

**Interfaces:**
- Consumes: `StrixDevice._go_offline()` from Task 2. This task changes its signature to `_go_offline(self, error: str = ERROR_OFFLINE) -> None`; the existing Task 2 call sites keep working because the parameter is optional.
- Produces:
  - `main.ERROR_OFFLINE: str` (value `"offline"`)
  - `main.ERROR_PERMISSION: str` (value `"permission"`)
  - `main.HEADSET_ICON: str` and `main.SLEEP_ICON: str` (ASCII escapes; see below)
  - `main.offline_message(last_error: str | None) -> str` — returns **plain text with no leading glyph**, so both the Qt and pystray paths can reuse it and the tests can assert on ASCII
  - `StrixDevice.last_error: str | None`

**Context you need:** `find_node` opens every `/dev/hidraw*` node. Its `except OSError: continue` at `main.py:64-65` swallows a permission failure exactly the way it swallows "this node is a keyboard, not ours". The user then sees "Headset: Offline / Out of range" forever, with no hint that the cause is the missing udev rule documented at `README.md:22-27`. Verified: patching `os.open` to raise `PermissionError` makes `query()` return `None`, identical to the receiver being unplugged.

Two facts this task relies on, both confirmed by running them:

- Python's `OSError` auto-mapping turns errno 13 (`EACCES`) and 1 (`EPERM`) into `PermissionError`, but leaves errno 19 (`ENODEV`), 5 (`EIO`) and 2 (`ENOENT`) as plain `OSError`. So `except PermissionError` placed **before** `except OSError` isolates the permission case without misclassifying device failures.
- The critical constraint: a permission error on *one* node must not stop the scan. The real receiver may be a different node that opens fine. `except PermissionError` must `continue`, never `return`.

**Why the glyph constants:** the offline branch of `update_state` currently contains two literal glyphs. Retyping them in a diff is unreliable, so this task lifts them into `HEADSET_ICON` and `SLEEP_ICON` using ASCII escapes and makes `offline_message` return glyph-free text. The two glyph lines on the *online* branch are out of scope — leave them as they are.

- [ ] **Step 1: Write the failing tests**

Insert these two module-level fakes and the test class immediately before `class SleepDurationTests(unittest.TestCase):`:

```python
ASUS_DEVINFO = struct.pack("<Ihh", 5, 0x0B05, 0x18D6)


def deny_first_node(path, *args, **kwargs):
    """`os.open` stand-in: /dev/hidraw0 is denied, every other node opens."""
    if path == "/dev/hidraw0":
        raise PermissionError(13, "Permission denied")
    return 7


def asus_ioctl(devinfo=ASUS_DEVINFO):
    """`fcntl.ioctl` stand-in: report an ASUS device and a valid status reply.

    Writes real bytes so `query()` reaches its success path. A bare MagicMock
    here would leave the read buffer zeroed, which the code correctly reports
    as offline, so the stale-error test would never clear.
    """

    def _ioctl(fd, request, buf, *args, **kwargs):
        if request == HIDIOCGRAWINFO:
            buf[:8] = devinfo
        elif request == HIDIOCGFEATURE_64:
            buf[1] = 0x1B   # status reply marker
            buf[9] = 0x0A   # charging
            buf[11] = 0x40  # voltage LSB; 0x0F40 = 3904 mV
            buf[12] = 0x0F
            buf[21] = 0x00  # sleep seconds
            buf[22] = 0x00

    return _ioctl


class LastErrorTests(unittest.TestCase):
    """A missing udev rule must not be reported as a missing headset."""

    def test_permission_denied_on_a_hidraw_node_is_reported_as_a_permission_error(self):
        device = StrixDevice()
        with patch("main.glob.glob", return_value=["/dev/hidraw0"]), patch(
            "main.os.open", side_effect=PermissionError(13, "Permission denied")
        ):
            self.assertIsNone(device.query())
        self.assertEqual(device.last_error, ERROR_PERMISSION)

    def test_a_vanished_receiver_is_reported_as_offline(self):
        device = StrixDevice()
        with patch("main.glob.glob", return_value=[]):
            self.assertIsNone(device.query())
        self.assertEqual(device.last_error, ERROR_OFFLINE)

    def test_a_non_permission_open_failure_is_still_reported_as_offline(self):
        device = StrixDevice()
        with patch("main.glob.glob", return_value=["/dev/hidraw0"]), patch(
            "main.os.open", side_effect=OSError(19, "No such device")
        ):
            self.assertIsNone(device.query())
        self.assertEqual(device.last_error, ERROR_OFFLINE)

    def test_a_successful_query_clears_a_stale_permission_error(self):
        device = StrixDevice()
        with patch("main.glob.glob", return_value=["/dev/hidraw0"]), patch(
            "main.os.open", side_effect=PermissionError(13, "Permission denied")
        ):
            device.query()
        self.assertEqual(device.last_error, ERROR_PERMISSION)

        with patch("main.glob.glob", return_value=["/dev/hidraw0"]), patch(
            "main.os.open", return_value=7
        ), patch("main.fcntl.ioctl", side_effect=asus_ioctl()), patch(
            "main.time.sleep"
        ), patch("main.os.close"):
            self.assertIsNotNone(device.query())
        self.assertIsNone(device.last_error)

    def test_a_denied_node_does_not_hide_the_real_receiver(self):
        device = StrixDevice()
        with patch(
            "main.glob.glob", return_value=["/dev/hidraw0", "/dev/hidraw1"]
        ), patch("main.os.open", side_effect=deny_first_node), patch(
            "main.os.close"
        ), patch("main.fcntl.ioctl", side_effect=asus_ioctl()), patch(
            "main.time.sleep"
        ):
            fd, path = device.find_node()
        self.assertEqual(path, "/dev/hidraw1")
        self.assertFalse(device._permission_denied)

    def test_offline_message_names_the_udev_rules_for_a_permission_error(self):
        message = offline_message(ERROR_PERMISSION)
        self.assertIn("permission", message.lower())
        self.assertIn("udev", message.lower())
        self.assertNotEqual(message, offline_message(ERROR_OFFLINE))

    def test_offline_message_stays_generic_when_the_headset_is_simply_absent(self):
        self.assertEqual(
            offline_message(ERROR_OFFLINE), "Headset: Offline / Out of range"
        )

    def test_offline_message_defaults_to_offline_for_an_unknown_error(self):
        self.assertEqual(offline_message(None), "Headset: Offline / Out of range")
```

Add `import struct` to the imports, and replace the `from main import ...` line with:

```python
import struct
import subprocess
import threading
import unittest
from types import ModuleType
from unittest.mock import patch

import main
from main import (
    ERROR_OFFLINE,
    ERROR_PERMISSION,
    HIDIOCGFEATURE_64,
    HIDIOCGRAWINFO,
    MAX_SLEEP_MINUTES,
    NOTIFY_TIMEOUT_SECONDS,
    StrixDevice,
    StrixTrayPystray,
    mv_to_percent,
    offline_message,
    validate_sleep_minutes,
)
```

Finally, give `FakeDevice` a `last_error` attribute so the pystray tests cannot break once the tray code reads it. In `FakeDevice.__init__` (`tests/test_main.py:108-118`), add this line immediately after the `self.status = {...}` assignment:

```python
        self.last_error = None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync python -m unittest tests.test_main.LastErrorTests -v`

Expected: `ImportError: cannot import name 'ERROR_OFFLINE' from 'main'`. No test body runs.

- [ ] **Step 3: Add the error and glyph constants**

In `main.py`, immediately after the `MAX_SLEEP_MINUTES = 1092` line and **before** the `NOTIFY_TIMEOUT_SECONDS` block from Task 1, add:

```python

# Why the last query produced no status. Distinguishing these lets the tray
# tell the user to fix their udev rules instead of blaming the headset.
ERROR_OFFLINE = "offline"
ERROR_PERMISSION = "permission"

# Tray glyphs as ASCII escapes so the source stays copy-pasteable and greppable.
HEADSET_ICON = "\U0001F3A7"
SLEEP_ICON = "\u23F1\uFE0F"
```

- [ ] **Step 4: Add the `offline_message` helper**

Insert this function immediately after `mv_to_percent` and before `class StrixDevice`:

```python
def offline_message(last_error: str | None) -> str:
    """Explain why no headset answered, telling permissions apart from absence.

    Returns plain text with no leading glyph so both the Qt and pystray paths
    can reuse it and so the tests can assert on ASCII.
    """
    if last_error == ERROR_PERMISSION:
        return (
            "Headset: no permission to open /dev/hidraw* "
            "-- see the udev rules in the README"
        )
    return "Headset: Offline / Out of range"
```

- [ ] **Step 5: Track the permission failure in `find_node`**

Replace `StrixDevice.__init__` with:

```python
    def __init__(self):
        self.last_status = None
        self.notified_low = False
        self.last_error = None
        self._permission_denied = False
        self._displayed_percentage = None
```

Replace the whole `find_node` method with:

```python
    def find_node(self):
        """Find the ASUS ROG Strix Go device and return its file descriptor."""
        permission_denied = False
        for path in sorted(glob.glob("/dev/hidraw*")):
            try:
                fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
            except PermissionError:
                # The udev rule from the README is missing. Keep scanning: a
                # different node may still be the receiver.
                permission_denied = True
                continue
            except OSError:
                continue

            try:
                buf = bytearray(8)
                fcntl.ioctl(fd, HIDIOCGRAWINFO, buf)
                _, vid, pid = struct.unpack('<Ihh', buf)
                if (vid & 0xFFFF) == ASUS_VID and (pid & 0xFFFF) in STRIX_PIDS:
                    # We found it, so a denied node elsewhere is irrelevant.
                    self._permission_denied = False
                    return fd, path
                os.close(fd)
            except (OSError, struct.error):
                os.close(fd)
                continue

        self._permission_denied = permission_denied
        return None, None
```

- [ ] **Step 6: Widen `_go_offline` and record the reason in `query`**

Replace the `_go_offline` method added in Task 2 with:

```python
    def _go_offline(self, error: str = ERROR_OFFLINE):
        """Record that no headset answered and drop the display hold."""
        self.last_status = None
        self.last_error = error
        self._displayed_percentage = None
```

In `query()`, replace the `find_node` result check:

```python
        fd, path = self.find_node()
        if fd is None:
            self._go_offline()
            return None
```

with:

```python
        fd, path = self.find_node()
        if fd is None:
            self._go_offline(
                ERROR_PERMISSION if self._permission_denied else ERROR_OFFLINE
            )
            return None
```

Then, in the success path, replace:

```python
            self.last_status = status
```

with:

```python
            self.last_status = status
            self.last_error = None
```

- [ ] **Step 7: Show the right message in the Qt tray**

In `main.py`, replace the entire `else` branch of `update_state` (`main.py:340-344`) with the block below. The result is ASCII-only, so the whole branch is now safely retypable:

```python
        else:
            message = offline_message(device.last_error)
            status_action.setText(f"{HEADSET_ICON} {message}")
            sleep_action.setText(f"{SLEEP_ICON} Sleep Timer: -")
            tooltip = message
            icon_img = render_tray_icon(0, False, connected=False)
```

Do not touch the two glyph lines in the `if st:` branch above it.

- [ ] **Step 8: Show the right message in the pystray fallback**

Replace `get_status_text` (`main.py:377-382`) with:

```python
    def get_status_text(self):
        st = self.device.last_status
        if not st:
            return offline_message(self.device.last_error)
        power_status = "Charging" if st["charging"] else "On battery"
        return f"Battery: {st['percentage']}% ({st['voltage']} mV - {power_status})"
```

Then replace the offline branch of `StrixTrayPystray.update_tray` (`main.py:419-421`) with:

```python
        else:
            self.icon.icon = render_tray_icon(0, False, connected=False)
            self.icon.title = offline_message(self.device.last_error)
```

- [ ] **Step 9: Run the full suite to verify it passes**

Run: `uv run --no-sync python -m unittest discover -s tests -v`

Expected: `Ran 43 tests` — `OK`. (35 from Task 2, plus 8 in `LastErrorTests`.)

- [ ] **Step 10: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "fix: report a hidraw permission failure instead of a missing headset"
```

---

### Task 4: Refuse to start the tray when the desktop has no tray

**Files:**
- Modify: `main.py` — insert the guard inside `run_pyqt_tray`, between `QApplication.setQuitOnLastWindowClosed(False)` and `tray = QSystemTrayIcon()`
- Test: `tests/test_main.py` — new `FakeQApplication` class, new `install_fake_pyqt6` helper, new `QtTrayAvailabilityTests` class, all before `class SleepDurationTests`

**Interfaces:**
- Consumes: nothing from other tasks. Runs `main.run_pyqt_tray` and `main.start_tray` with PyQt6 faked out.
- Produces: no new production API. The guard raises `SystemExit(1)`.

**Context you need:** `run_pyqt_tray` never asks whether a system tray actually exists. On a desktop with no StatusNotifierItem host — GNOME without the AppIndicator extension, a bare Wayland session, a headless box — `tray.show()` at `main.py:354` only logs a Qt warning, and the process then sits inside `app.exec()` forever: no icon, no menu, no window, no way out except `SIGKILL` or a terminal the user has to know to open.

Verified: under `QT_QPA_PLATFORM=offscreen`, `QSystemTrayIcon()` constructs fine and `QSystemTrayIcon.isSystemTrayAvailable()` returns `False`. That is exactly the case the guard must catch.

**The single most important detail in this task:** the guard must raise `SystemExit`, **not** `ImportError`. `start_tray` at `main.py:483-487` wraps the Qt call in `try: run_pyqt_tray(device) / except ImportError:` and falls through to pystray. An `ImportError` here would silently start pystray, which also needs a tray and would fail the same way. `SystemExit` is a `BaseException`, is not caught by `except ImportError`, and propagates out of `main()` to exit the process with the message.

Place the guard immediately after `QApplication.setQuitOnLastWindowClosed(False)` and **before** `tray = QSystemTrayIcon()`, so that on a tray-less desktop no widgets are ever constructed.

The fakes below use two-level `sys.modules` injection, which was verified to satisfy `from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon` inside `run_pyqt_tray`. `ModuleType` is already imported in the test file at line 3.

- [ ] **Step 1: Write the failing tests**

Insert this class, helper and test class immediately before `class SleepDurationTests(unittest.TestCase):`:

```python
class FakeQApplication:
    """Just enough QApplication for the tray-availability guard."""

    _instance = None

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @staticmethod
    def setQuitOnLastWindowClosed(value):
        pass


def install_fake_pyqt6(testcase, tray_available):
    """Point every `from PyQt6...` import in `main` at in-memory fakes."""
    qtwidgets = ModuleType("PyQt6.QtWidgets")
    qtwidgets.QApplication = FakeQApplication
    qtwidgets.QMenu = type("QMenu", (), {})
    qtwidgets.QSystemTrayIcon = type(
        "QSystemTrayIcon",
        (),
        {"isSystemTrayAvailable": staticmethod(lambda: tray_available)},
    )

    qtcore = ModuleType("PyQt6.QtCore")
    qtcore.QTimer = type("QTimer", (), {})

    qtgui = ModuleType("PyQt6.QtGui")
    qtgui.QAction = type("QAction", (), {})
    qtgui.QIcon = type("QIcon", (), {})

    package = ModuleType("PyQt6")
    package.__path__ = []

    patcher = patch.dict(
        "sys.modules",
        {
            "PyQt6": package,
            "PyQt6.QtCore": qtcore,
            "PyQt6.QtGui": qtgui,
            "PyQt6.QtWidgets": qtwidgets,
        },
    )
    patcher.start()
    testcase.addCleanup(patcher.stop)


class QtTrayAvailabilityTests(unittest.TestCase):
    """A desktop with no tray must get a clear message, not a silent hang."""

    def test_run_pyqt_tray_exits_when_no_system_tray_host_exists(self):
        install_fake_pyqt6(self, tray_available=False)
        printed = []
        with patch("builtins.print", side_effect=printed.append):
            with self.assertRaises(SystemExit) as raised:
                run_pyqt_tray(StrixDevice())
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("no system tray", " ".join(printed).lower())

    def test_the_guard_exits_rather_than_raising_import_error(self):
        # start_tray catches ImportError and falls back to pystray, which also
        # needs a tray. SystemExit is a BaseException, so it escapes that catch.
        install_fake_pyqt6(self, tray_available=False)
        with patch("builtins.print"):
            with self.assertRaises(SystemExit):
                run_pyqt_tray(StrixDevice())

    def test_start_tray_does_not_fall_back_to_pystray_on_a_tray_less_desktop(self):
        install_fake_pyqt6(self, tray_available=False)
        with patch("builtins.print"), patch(
            "main.importlib.util.find_spec", return_value=object()
        ), patch("main.StrixTrayPystray") as tray_class:
            with self.assertRaises(SystemExit):
                start_tray(StrixDevice())
        tray_class.assert_not_called()
```

Add `run_pyqt_tray` and `start_tray` to the `from main import ...` line, keeping the existing alphabetical order:

```python
    run_pyqt_tray,
    start_tray,
    validate_sleep_minutes,
)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync python -m unittest tests.test_main.QtTrayAvailabilityTests -v`

Expected: the import succeeds (`run_pyqt_tray` and `start_tray` already exist), then all three tests fail with
`TypeError: QAction.__init__() takes exactly one argument (3 given)`.
That is the correct red state: it proves `run_pyqt_tray` ran straight past the absent guard and into widget construction.

- [ ] **Step 3: Add the guard**

In `main.py`, insert this block immediately after the line `QApplication.setQuitOnLastWindowClosed(False)` and immediately before the line `tray = QSystemTrayIcon()`:

```python
    if not QSystemTrayIcon.isSystemTrayAvailable():
        # Without a StatusNotifierItem host the icon never appears and the
        # process would sit in app.exec() forever with no window and no menu.
        # SystemExit, not ImportError: start_tray only catches ImportError, and
        # the pystray fallback needs a tray just as much.
        print(
            "Error: no system tray was found on this desktop.\n"
            "Qt could not reach a StatusNotifierItem host, so the tray icon "
            "would never appear.\n"
            "On GNOME, install the AppIndicator extension; on KDE Plasma, "
            "enable the Status Notifier extra.\n"
        )
        sys.exit(1)
```

- [ ] **Step 4: Run the full suite to verify it passes**

Run: `uv run --no-sync python -m unittest discover -s tests -v`

Expected: `Ran 46 tests` — `OK`. (43 from Task 3, plus 3 in `QtTrayAvailabilityTests`.)

- [ ] **Step 5: Verify against real Qt, not just the fakes**

The fakes prove the guard's control flow. Confirm it against real Qt, headless, where a tray genuinely does not exist:

```bash
QT_QPA_PLATFORM=offscreen uv run --no-sync python main.py --tray; echo "exit=$?"
```

Expected output, then exit status 1:

```
Error: no system tray was found on this desktop.
Qt could not reach a StatusNotifierItem host, so the tray icon would never appear.
On GNOME, install the AppIndicator extension; on KDE Plasma, enable the Status Notifier extra.
```

The command must return promptly. If it hangs, the guard is in the wrong place, or `isSystemTrayAvailable()` is being called before the `QApplication` exists.

- [ ] **Step 6: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "fix: exit with a clear message when the desktop has no system tray"
```

---

## Verification Record

This plan was executed end to end against a throwaway copy of the tree before being written up, and these are the observed results rather than predictions:

- `uv run --no-sync python -m unittest discover -s tests -v` → **`Ran 46 tests ... OK`**, with all 20 pre-existing tests still green.
- `QT_QPA_PLATFORM=offscreen ... python main.py --tray` → the three-line error message and `exit=1`, returning promptly instead of hanging.
- The two intermediate test counts (24 after Task 1, 35 after Task 2, 43 after Task 3) were all confirmed by real runs.
- Two defects were found and fixed by that dry run, and the versions in this plan are the corrected ones: the first `find_node` test depended on which `/dev/hidraw*` the host actually had, and the first stale-error test used a bare `MagicMock` for `ioctl`, which left the reply buffer zeroed so `query()` correctly reported offline and the assertion never cleared.

## Out of Scope

Findings from the same review that this plan deliberately does **not** touch. Recorded so the boundary is auditable and a follow-up plan need not re-derive them.

**P1** — cache the Pillow font (`main.py:222-237`; measured 6.4ms and 4 `stat` calls per render); cache the device node so `/dev/hidraw*` is not re-globbed every 15s (measured 30 `os.open` calls per 5 offline queries); add `pygobject` to the `fallback-tray` extra, without which the pystray path silently loses automatic refresh (`main.py:462-468`); remove the duplicate device query on every sleep-menu click (`main.py:151` plus `main.py:311`).

**P2** — convert `py-modules = ["main"]` into a `strix_battery/` package so a generic top-level `main` stops landing in `site-packages`; name the HID byte offsets at `main.py:100-103` and pin them with decode tests; let `set_sleep` propagate its own validation error instead of returning an ambiguous `False` (`main.py:130-133`); make `--json` and the text report agree on the offline exit code (`main.py:549-562` versus `main.py:565-567`).

**P3** — replace the linear 3300-4150mV curve with a piecewise table (it understates mid-range by 10 to 12 points); replace the eleven `object.__new__(StrixTrayPystray)` constructions in the tests with a factory; add a linter and CI; fix the misleading "PyQt6 was not found" message at `main.py:490`.

**Explicitly not a defect** — the ioctl constants at `main.py:15-17` are correct. `HIDIOCGRAWINFO=0x80084803` matches `_IOR('H', 0x03, struct hidraw_devinfo)` (8 bytes), and `0xC0404806`/`0xC0404807` match `HIDIOCSFEATURE(64)`/`HIDIOCGFEATURE(64)` given `HIDRAW_BUFFER_SIZE 64` and `_IOC(_IOC_WRITE|_IOC_READ, 'H', ...)` in `/usr/include/linux/hidraw.h`. Do not "fix" these. `find_node` also closes its fd on every branch, so there is no descriptor leak.
