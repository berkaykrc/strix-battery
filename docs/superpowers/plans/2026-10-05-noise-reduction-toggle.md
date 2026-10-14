# Microphone Noise Reduction Toggle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Add a working on/off toggle for the ASUS ROG Strix Go 2.4's microphone AI noise reduction, driven by the same HID feature report the sleep timer already uses.

**Architecture:** `StrixDevice` gains a `noise_reduction` attribute and a `set_noise_reduction(enabled: bool) -> bool` method that sends one 64-byte feature report and reports whether the headset accepted it. The state is remembered in a small JSON file next to the other app state because the firmware cannot be asked what the current setting is. Both tray backends (PyQt6 and the Pystray fallback) get a checkable menu item, and the CLI gets `--noise-reduction on|off` to match `--set-sleep`.

**Tech Stack:** Python 3.12, `fcntl` hidraw ioctls (`HIDIOCSFEATURE_64`), PyQt6 with a Pystray fallback, `unittest` with `unittest.mock`.

**Spec:** This conversation. The packet bytes were recovered by reading the IL of `GHelper.Peripherals.Headset.Models.StrixGo24` in `~/GHelper.exe` (assembly at file offset `0x41000`, `md5 695b1b2ef063f29ba933e65fd406dbcd`). There is no separate spec document.

## Protocol Reference

The command family, decoded from the three packets now known:

```
index:  0     1     2     3     4     5     6     7     8     9      10, 11
battery: FF   08    00    FD    04    12    F1    03    52    01
sleep:   FF   0A    00    FF    04    12    F1    05    52    0B     sec_lo, sec_hi
noise:   FF   08    00    FF    04    12    F1    03    52    0C/0D
```

- `[0]` report ID `0xFF`
- `[1]` total meaningful length minus 2
- `[2]` flags, always `0x00`
- `[3]` `FD` = read, `FF` = write
- `[4..6]` constant `04 12 F1`
- `[7]` command: `03` = general, `05` = energy
- `[8]` constant `0x52`
- `[9]` sub-command: `01` battery, `0B` sleep timer, `0C` noise reduction off, `0D` noise reduction on

The noise-reduction packet is 10 meaningful bytes zero-padded to 64. `0x0C` is off and `0x0D` is on, taken from the G-Helper IL:

```
IL_001d  ldc.i4.s 9
IL_001f  ldarg.1
IL_0020  brtrue.s -> IL_0026
IL_0022  ldc.i4.s 0xc
IL_0026  ldc.i4.s 0xd
IL_0028  stelem.i4
```

so the argument is `0x0D` when true and `0x0C` when false. G-Helper's `StrixGo24.HasNoiseReductionLevel()` returns false, so there are exactly two states and no level argument.

`StrixGo24.ReadNoiseReduction` does not read the firmware; it restores `AppConfig["headset_nc_StrixGo24"]`. The firmware exposes no way to query the current value, which is why this project needs its own state file. G-Helper's `HasReset()` is also false, so there is no reset-to-defaults command and nothing to reverse a mistaken toggle with beyond sending the opposite packet.

## Global Constraints

- Linux only. `HIDIOCSFEATURE_64` is the Linux equivalent of the `HidD_SetFeature` call G-Helper uses; no new dependency and no new permission requirement.
- Python 3.12, no third-party library additions. Standard library only (`json`, `pathlib`, `fcntl`, `os`, `time`).
- Every new method on `StrixDevice` returns `bool` for success and leaves `last_status` / `last_error` semantics untouched, matching `set_sleep`.
- A failed send must never leave `noise_reduction` reporting a state the headset does not have.
- Comments in `main.py` are written as "why" explanations, not restatements of the code, matching the existing style.
- Tests run with `uv run python -m unittest discover -s tests`. The suite must stay green; 1031 lines of existing tests are not to be edited except to add.

## Review Focus

Conditions a real user hits that no task's tests exercise until they are named here:

1. **No udev rule installed** — `/dev/hidraw2` is `crw-rw-rw-. root root`, so an unprivileged user gets `PermissionError` on open. The toggle must fail with the same permission wording `offline_message()` already produces, not a traceback.
2. **Headset powered off but dongle still plugged in** — the feature report is accepted, so the toggle must not claim the setting changed on hardware that is asleep.
3. **Two processes toggling at once** — the tray and a `--noise-reduction` CLI invocation can race; the state file must not be corrupted into unreadable JSON.
4. **The state file is deleted or hand-edited to garbage** — startup must not crash the tray; it falls back to off.
5. **The user toggles and then Armoury Crate toggles** — the two disagree because our state file is now stale. The tray must not present the stale file value as authoritative after a failed send.
6. **`0x0C`/`0x0D` are guessed from IL, not confirmed against a capture** — if the firmware ignores them the send still succeeds, so the toggle would silently appear to work. Task 1's byte tests pin the bytes; the hardware confirmation step in Task 5 is what proves the semantics.

---

### Task 1: The feature report packet

**Files:**
- Modify: `main.py` (add module constant near `QUERY_PACKET` at line 47-50)
- Test: `tests/test_main.py` (`NoiseReductionPacketTests`, `NoiseReductionBehaviourTests` — already written and failing)

**Interfaces:**
- Consumes: nothing. `StrixDevice` exists with `find_node()`, `notify(title, msg)`.
- Produces: `main.NOISE_REDUCTION_SUB_COMMAND: dict[bool, int]` mapping `True -> 0x0D` and `False -> 0x0C`, and `StrixDevice.set_noise_reduction(enabled: bool) -> bool` plus the instance attribute `noise_reduction: bool`.

The eight tests in `tests/test_main.py` already exist and fail with `AttributeError: 'StrixDevice' object has no attribute 'set_noise_reduction'`. They are the whole deliverable for this task; do not rewrite them.

- [x] **Step 1: Confirm the eight tests fail for the right reason**

Run: `uv run python -m unittest tests.test_main.NoiseReductionPacketTests tests.test_main.NoiseReductionBehaviourTests 2>&1 | tail -5`

Expected: `FAILED`, every error reading `AttributeError: 'StrixDevice' object has no attribute 'set_noise_reduction'`.

- [x] **Step 2: Add the sub-command mapping next to `QUERY_PACKET`**

In `main.py`, immediately after the `QUERY_PACKET` definition (ends line 50), add:

```python
# Mic noise reduction, from the packet G-Helper 0.286 sends for this headset:
# FF 08 00 FF 04 12 F1 03 52 <0C off | 0D on>. Byte 9 is the only field that
# changes between the two states, so keep the mapping in one place rather than
# branching at the call site.
NOISE_REDUCTION_SUB_COMMAND = {False: 0x0C, True: 0x0D}
```

- [x] **Step 3: Add the `noise_reduction` attribute to `__init__`**

In `StrixDevice.__init__` (line 99-104), add `self.noise_reduction = False` alongside the other per-instance fields.

- [x] **Step 4: Implement `set_noise_reduction` after `set_sleep`**

Place this method immediately after `set_sleep` (which ends at line 251, before `def notify`). It follows `set_sleep`'s shape exactly: find the node, build the packet, send, notify, close in `finally`.

```python
def set_noise_reduction(self, enabled: bool):
    """Toggle the microphone AI noise reduction in the headset firmware."""
    fd, _ = self.find_node()
    if fd is None:
        return False

    try:
        payload = bytearray([
            0xFF, 0x08, 0x00, 0xFF, 0x04, 0x12, 0xF1, 0x03, 0x52,
            NOISE_REDUCTION_SUB_COMMAND[bool(enabled)]
        ] + [0x00] * 54)

        fcntl.ioctl(fd, HIDIOCSFEATURE_64, payload)
        self.noise_reduction = bool(enabled)
        self.notify(
            "ROG Strix Go",
            "Microphone noise reduction on." if enabled
            else "Microphone noise reduction off."
        )
        return True
    except OSError:
        return False
    finally:
        os.close(fd)
```

Note the asymmetry with `set_sleep`: `set_sleep` calls `self.query()` afterwards to refresh the display, and this must not. The firmware has no read-back for this setting, so a follow-up query would only report battery data and would overwrite `last_status` for no gain.

- [x] **Step 5: Run the packet tests**

Run: `uv run python -m unittest tests.test_main.NoiseReductionPacketTests -v 2>&1 | tail -12`

Expected: all `ok`.

- [x] **Step 6: Run the behaviour tests**

Run: `uv run python -m unittest tests.test_main.NoiseReductionBehaviourTests -v 2>&1 | tail -12`

Expected: all `ok`. `test_only_byte_nine_differs_between_the_two_states` is the one that fails if any other byte moved.

- [x] **Step 7: Run the whole suite**

Run: `uv run python -m unittest discover -s tests 2>&1 | tail -5`

Expected: `OK`, with 8 more tests than before this task.

- [x] **Step 8: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "feat: send the mic noise reduction feature report"
```

---

### Task 2: Persist the state across restarts

**Files:**
- Modify: `main.py` (module-level path helper, `StrixDevice.__init__`, `__init__`)
- Test: `tests/test_main.py` (new class `NoiseReductionStateFileTests`)

**Interfaces:**
- Consumes: `StrixDevice.noise_reduction` and `set_noise_reduction` from Task 1.
- Produces: `main.noise_reduction_state_path() -> pathlib.Path`, and `StrixDevice.load_noise_reduction_state()` / `StrixDevice.save_noise_reduction_state()` methods. Later tasks read `device.noise_reduction` only; they never touch the path helper.

The firmware cannot report this setting, so the value survives a restart only if we store it. G-Helper uses `AppConfig["headset_nc_StrixGo24"]`; this project keeps app state in a JSON file instead, consistent with how it already persists nothing else on disk but keeps its config under the user's home.

- [x] **Step 1: Write the failing test class**

Append to `tests/test_main.py`, before the `if __name__ == "__main__":` block:

```python
class NoiseReductionStateFileTests(unittest.TestCase):
    """The firmware cannot be asked for this setting, so it must be stored.

    A missing, unreadable or hand-mangled file has to degrade to "off" rather
    than take the tray down at startup.
    """

    def write_state(self, tmp, content):
        path = tmp / "noise_reduction.json"
        path.write_text(content)
        return path

    def test_a_saved_true_is_restored(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            path = self.write_state(tmp, json.dumps({"noise_reduction": True}))
            with patch("main.noise_reduction_state_path", return_value=path):
                device = StrixDevice()
                device.load_noise_reduction_state()
            self.assertTrue(device.noise_reduction)

    def test_a_missing_file_leaves_the_state_off(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            with patch("main.noise_reduction_state_path", return_value=tmp / "absent.json"):
                device = StrixDevice()
                device.load_noise_reduction_state()
            self.assertFalse(device.noise_reduction)

    def test_garbage_json_leaves_the_state_off(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            path = self.write_state(tmp, "{not json at all")
            with patch("main.noise_reduction_state_path", return_value=path):
                device = StrixDevice()
                device.load_noise_reduction_state()
            self.assertFalse(device.noise_reduction)

    def test_a_saved_non_boolean_leaves_the_state_off(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            path = self.write_state(tmp, json.dumps({"noise_reduction": "yes"}))
            with patch("main.noise_reduction_state_path", return_value=path):
                device = StrixDevice()
                device.load_noise_reduction_state()
            self.assertFalse(device.noise_reduction)

    def test_the_state_is_written_as_a_boolean_after_a_successful_send(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            path = tmp / "noise_reduction.json"
            device = StrixDevice()
            with patch("main.glob.glob", return_value=["/dev/hidraw0"]), patch(
                "main.os.open", return_value=7
            ), patch("main.fcntl.ioctl", side_effect=asus_ioctl()), patch(
                "main.time.sleep"
            ), patch("main.os.close"), patch.object(device, "notify"), patch(
                "main.noise_reduction_state_path", return_value=path
            ):
                device.set_noise_reduction(True)
            self.assertEqual(
                json.loads(path.read_text()), {"noise_reduction": True}
            )

    def test_a_failed_send_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            path = tmp / "noise_reduction.json"
            device = StrixDevice()
            with patch("main.glob.glob", return_value=[]):
                device.set_noise_reduction(True)
            self.assertFalse(path.exists())
```

Add `import json`, `import pathlib` and `import tempfile` to the top of the file if they are not already imported; the file currently imports `struct`, `subprocess`, `threading`, `unittest` and the `unittest.mock` helpers.

- [x] **Step 2: Run to verify failure**

Run: `uv run python -m unittest tests.test_main.NoiseReductionStateFileTests 2>&1 | tail -5`

Expected: `FAILED (errors=6)`, with `AttributeError` naming `noise_reduction_state_path`.

- [x] **Step 3: Add the path helper**

In `main.py`, next to the other module-level constants (after `NOISE_REDUCTION_SUB_COMMAND`):

```python
# The firmware will not report the current noise-reduction state, so the last
# value we sent is the only record of it. Keep it beside the other user
# settings rather than in the working directory.
def noise_reduction_state_path():
    return pathlib.Path.home() / ".config" / "strix-battery" / "noise-reduction.json"
```

Add `import pathlib` to the imports at the top of `main.py`.

- [x] **Step 4: Add load and save methods**

Place these immediately after `set_noise_reduction` from Task 1:

```python
def load_noise_reduction_state(self):
    """Restore the last noise-reduction value we sent.

    Anything unexpected in the file means "off": the firmware has no reset
    command, so refusing to start over a bad state file would be worse than
    guessing.
    """
    try:
        with noise_reduction_state_path().open(encoding="utf-8") as handle:
            stored = json.load(handle)
        self.noise_reduction = stored.get("noise_reduction") is True
    except (OSError, ValueError, AttributeError):
        self.noise_reduction = False

def save_noise_reduction_state(self):
    """Persist the current value, creating the directory on first use."""
    try:
        path = noise_reduction_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump({"noise_reduction": self.noise_reduction}, handle)
    except OSError:
        pass
```

The `is True` comparison in `load` is what makes a stored `"yes"` fall back to off rather than become truthy.

- [x] **Step 5: Call save from `set_noise_reduction` and load from `__init__`**

In `set_noise_reduction`, add `self.save_noise_reduction_state()` immediately after `self.noise_reduction = bool(enabled)` and before the `self.notify(...)` call, so a notify failure cannot skip the write.

In `__init__`, add `self.load_noise_reduction_state()` after `self.noise_reduction = False`. The explicit `False` first matters: it guarantees the attribute exists even if the loader is patched out.

- [x] **Step 6: Run the new tests**

Run: `uv run python -m unittest tests.test_main.NoiseReductionStateFileTests -v 2>&1 | tail -12`

Expected: all `ok`.

- [x] **Step 7: Run the whole suite**

Run: `uv run python -m unittest discover -s tests 2>&1 | tail -5`

Expected: `OK`.

- [x] **Step 8: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "feat: persist the noise reduction state across restarts"
```

---

### Task 3: The `--noise-reduction` CLI flag

**Files:**
- Modify: `main.py` (`main()` at line 619-691)
- Test: `tests/test_main.py` (new class `NoiseReductionCliTests`)

**Interfaces:**
- Consumes: `StrixDevice.set_noise_reduction` and `device.noise_reduction` from Tasks 1-2.
- Produces: `main.PARSE_NOISE_REDUCTION: dict[str, bool]` mapping `"on" -> True`, `"off" -> False`, consumed by `main()` to validate the flag. No new public function.

- [x] **Step 1: Write the failing test class**

Append to `tests/test_main.py`, before the `if __name__ == "__main__":` block:

```python
class NoiseReductionCliTests(unittest.TestCase):
    """`--noise-reduction on|off` must behave like `--set-sleep`."""

    def run_main(self, argv):
        out = []
        with patch.object(sys, "argv", ["strix-battery"] + argv), patch(
            "builtins.print", side_effect=out.append
        ):
            try:
                main.main()
            except SystemExit as exc:
                return exc.code, out
        return None, out

    def test_on_prints_a_confirmation_and_exits_zero(self):
        device = StrixDevice()
        with patch.object(main, "StrixDevice", return_value=device), patch.object(
            device, "set_noise_reduction", return_value=True
        ) as setter:
            code, out = self.run_main(["--noise-reduction", "on"])
        setter.assert_called_once_with(True)
        self.assertIn("noise reduction on", out[0].lower())
        self.assertIsNone(code)

    def test_off_turns_it_off(self):
        device = StrixDevice()
        with patch.object(main, "StrixDevice", return_value=device), patch.object(
            device, "set_noise_reduction", return_value=True
        ) as setter:
            self.run_main(["--noise-reduction", "off"])
        setter.assert_called_once_with(False)

    def test_a_failed_send_reports_the_receiver_and_exits_one(self):
        device = StrixDevice()
        with patch.object(main, "StrixDevice", return_value=device), patch.object(
            device, "set_noise_reduction", return_value=False
        ):
            code, out = self.run_main(["--noise-reduction", "on"])
        self.assertEqual(code, 1)
        self.assertIn("could not be reached", out[0])

    def test_an_unknown_word_is_rejected_before_any_send(self):
        device = StrixDevice()
        with patch.object(sys, "argv", ["strix-battery", "--noise-reduction", "maybe"]), \
             patch.object(main, "StrixDevice", return_value=device), \
             patch.object(device, "set_noise_reduction") as setter:
            with self.assertRaises(SystemExit) as ctx:
                main.main()
        setter.assert_not_called()
        self.assertNotEqual(ctx.exception.code, 0)
```

`setter.assert_not_called()` is the point of the test: argparse must reject the word before any device access, so a typo in the flag value can never half-apply a setting.

- [x] **Step 2: Run to verify failure**

Run: `uv run python -m unittest tests.test_main.NoiseReductionCliTests 2>&1 | tail -6`

Expected: `FAILED (errors=4)`. All four tests raise `SystemExit(2)` out of argparse complaining that `--noise-reduction` is unrecognised, so every one of them is an error rather than three errors and one failure.

- [x] **Step 3: Add the flag to the parser**

In `main()`, after the `--set-sleep` argument block (ends line 638), add:

```python
parser.add_argument(
    "-n", "--noise-reduction",
    choices=["on", "off"],
    metavar="STATE",
    help="Set the microphone AI noise reduction (on or off)",
)
```

`choices` makes argparse reject anything else and exit non-zero before any device work happens, which is what the fourth test checks.

- [x] **Step 4: Add the handling block**

In `main()`, after the `--set-sleep` handling block (ends line 661), add:

```python
if args.noise_reduction is not None:
    enabled = args.noise_reduction == "on"
    if device.set_noise_reduction(enabled):
        print("Microphone noise reduction on." if enabled
              else "Microphone noise reduction off.")
    else:
        print("Error: Headset receiver could not be reached.")
        sys.exit(1)
    return
```

- [x] **Step 5: Run the CLI tests**

Run: `uv run python -m unittest tests.test_main.NoiseReductionCliTests -v 2>&1 | tail -12`

Expected: all `ok`.

- [x] **Step 6: Run the whole suite**

Run: `uv run python -m unittest discover -s tests 2>&1 | tail -5`

Expected: `OK`.

- [x] **Step 7: Update the README usage block**

In `README.md`, add to the usage list after the `--set-sleep 30` line:

```bash
uv run strix-battery --noise-reduction on
uv run strix-battery --noise-reduction off
```

- [x] **Step 8: Commit**

```bash
git add main.py tests/test_main.py README.md
git commit -m "feat: add the --noise-reduction CLI flag"
```

---

### Task 4: Tray menu items in both backends

**Files:**
- Modify: `main.py` (`run_pyqt_tray` at line 380-477, `StrixTrayPystray.get_noise_reduction_text`/`create_menu` at line 494-524)
- Test: `tests/test_main.py` (new classes `QtNoiseReductionMenuTests`, `PystrayNoiseReductionMenuTests`)

**Interfaces:**
- Consumes: `device.set_noise_reduction(enabled: bool) -> bool`, `device.noise_reduction: bool` from Tasks 1-2.
- Produces: `main.NOISE_REDUCTION_ICON` (a module-level str for the PyQt label), `StrixTrayPystray.get_noise_reduction_text() -> str`, and a PyQt `QAction` whose text is `f"{NOISE_REDUCTION_ICON} Microphone Noise Reduction: {'On' if device.noise_reduction else 'Off'}"`.

Both backends already display the sleep timer as a disabled, read-only status row plus a separate submenu of actions. The toggle follows the same shape so the two tray implementations stay comparable: a status row that reports the current value, and a submenu with the action that changes it.

- [x] **Step 1: Write the failing PyQt test class**

Append to `tests/test_main.py`, before the `if __name__ == "__main__":` block:

```python
class QtNoiseReductionMenuTests(unittest.TestCase):
    """The PyQt6 tray must show the state and offer the toggle."""

    def texts(self):
        return [a.text for a in RecordingAction.instances if a.text is not None]

    def test_the_status_row_reports_the_current_state(self):
        install_fake_pyqt6(self, tray_available=True, permissive=True)
        device = StrixDevice()
        device.noise_reduction = True
        with patch.object(device, "query", return_value=None):
            with self.assertRaises(SystemExit):
                run_pyqt_tray(device)
        self.assertIn(
            f"{main.NOISE_REDUCTION_ICON} Microphone Noise Reduction: On",
            self.texts(),
        )

    def test_the_toggle_action_sends_the_opposite_state(self):
        install_fake_pyqt6(self, tray_available=True, permissive=True)
        device = StrixDevice()
        device.noise_reduction = False
        with patch.object(device, "query", return_value=None), patch.object(
            device, "set_noise_reduction", return_value=True
        ) as setter:
            with self.assertRaises(SystemExit):
                run_pyqt_tray(device)
        toggle = next(a for a in RecordingAction.instances if a.text == "Toggle Microphone Noise Reduction")
        toggle.triggered.emit(False)
        setter.assert_called_once_with(True)

    def test_offline_hides_the_current_value(self):
        install_fake_pyqt6(self, tray_available=True, permissive=True)
        device = StrixDevice()
        with patch.object(device, "query", return_value=None):
            with self.assertRaises(SystemExit):
                run_pyqt_tray(device)
        self.assertIn(
            f"{main.NOISE_REDUCTION_ICON} Microphone Noise Reduction: -",
            self.texts(),
        )
```

`RecordingAction` needs to record `triggered` connections for the second test to fire one. Add to `RecordingAction`:

```python
def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)
    RecordingAction.instances.append(self)
    self.text = None
    self.triggered = FakeSignal()
```

and define `FakeSignal` above the class:

```python
class FakeSignal:
    """Records connections so a test can fire one."""

    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in self.slots:
            slot(*args)
```

The existing tests pass `permissive=True` fakes whose `triggered` is a `FakeQtWidget`; giving `RecordingAction` a real signal must not disturb them, since they only read `.text`.

- [x] **Step 2: Write the failing Pystray test class**

```python
class PystrayNoiseReductionMenuTests(unittest.TestCase):
    """The Pystray fallback must match the PyQt6 menu."""

    def test_the_status_text_reflects_the_device_state(self):
        tray = object.__new__(StrixTrayPystray)
        device = FakeDevice()
        device.noise_reduction = True
        tray.device = device
        self.assertIn("On", tray.get_noise_reduction_text())

    def test_the_menu_offers_the_toggle_and_calls_the_device(self):
        tray = object.__new__(StrixTrayPystray)
        device = FakeDevice()
        device.noise_reduction = False
        device.set_noise_reduction_calls = []
        device.set_noise_reduction = lambda enabled: device.set_noise_reduction_calls.append(enabled)
        tray.device = device
        menu = tray.create_menu()
        toggle = find_menu_item(menu.items, "Toggle Microphone Noise Reduction")
        self.assertIsNotNone(toggle)
        toggle.action()
        self.assertEqual(device.set_noise_reduction_calls, [True])
```

Add `self.noise_reduction = False` to `FakeDevice.__init__` so the new attribute does not break the existing `PystrayPollingTests`.

- [x] **Step 3: Run to verify failure**

Run: `uv run python -m unittest tests.test_main.QtNoiseReductionMenuTests tests.test_main.PystrayNoiseReductionMenuTests 2>&1 | tail -6`

Expected: `FAILED (errors=5)`. The three PyQt tests fail with `AttributeError: module 'main' has no attribute 'NOISE_REDUCTION_ICON'`, the two Pystray tests with `AttributeError: 'StrixTrayPystray' object has no attribute 'get_noise_reduction_text'`.

- [x] **Step 4: Add the icon constant**

In `main.py`, next to `HEADSET_ICON` and `SLEEP_ICON` (lines 29-30), add:

```python
NOISE_REDUCTION_ICON = "\U0001F507"
```

- [x] **Step 5: Add the PyQt status action and toggle**

In `run_pyqt_tray`, after the `sleep_action` block and before `menu.addSeparator()` (around line 417), add a status action:

```python
noise_action = QAction(f"{NOISE_REDUCTION_ICON} Microphone Noise Reduction: -", menu)
noise_action.setEnabled(False)
menu.addAction(noise_action)
```

Then, after the `sleep_menu` submenu is added (line 429), add:

```python
noise_menu = QMenu("Microphone Noise Reduction", menu)
for enabled, label in [
    (True, "Enable Noise Reduction"),
    (False, "Disable Noise Reduction"),
]:
    action = QAction(label, noise_menu)
    action.triggered.connect(
        lambda checked=False, want=enabled: (device.set_noise_reduction(want), update_state())
    )
    noise_menu.addAction(action)
menu.addMenu(noise_menu)
```

Binding `want=enabled` as a default argument is required, not optional: the existing sleep lambda uses the same `m=mins` pattern because a bare closure would capture the loop variable.

- [x] **Step 6: Update `update_state` to refresh the status action**

In `update_state`, in the `if st:` branch after `sleep_action.setText(...)` (line 453), add:

```python
noise_action.setText(
    f"{NOISE_REDUCTION_ICON} Microphone Noise Reduction: "
    f"{'On' if device.noise_reduction else 'Off'}"
)
```

and in the `else` branch after `sleep_action.setText(...)` (line 459), add:

```python
noise_action.setText(f"{NOISE_REDUCTION_ICON} Microphone Noise Reduction: -")
```

Hiding the value when offline is deliberate: the stored value is only what we last sent, and with no dongle there is nothing to confirm it against.

- [x] **Step 7: Add the Pystray status text and toggle**

In `StrixTrayPystray`, add next to `get_sleep_text`:

```python
def get_noise_reduction_text(self):
    st = self.device.last_status
    if not st:
        return "Microphone Noise Reduction: -"
    state = "On" if self.device.noise_reduction else "Off"
    return f"Microphone Noise Reduction: {state}"
```

and in `create_menu`, add a status row after the sleep row and a submenu after the sleep submenu:

```python
item(lambda text: self.get_noise_reduction_text(), None, enabled=False),
Menu.SEPARATOR,
item("Automatic Sleep Timer", Menu(
    item("Never Turn Off (0 min)", lambda: self.device.set_sleep(0)),
    item("5 Minutes", lambda: self.device.set_sleep(5)),
    item("10 Minutes", lambda: self.device.set_sleep(10)),
    item("15 Minutes", lambda: self.device.set_sleep(15)),
    item("30 Minutes", lambda: self.device.set_sleep(30)),
)),
item("Microphone Noise Reduction", Menu(
    item("Enable Noise Reduction", lambda: self.device.set_noise_reduction(True)),
    item("Disable Noise Reduction", lambda: self.device.set_noise_reduction(False)),
)),
```

Each lambda passes the boolean positionally, matching `set_noise_reduction(enabled: bool) -> bool`. The sleep submenu is repeated in full rather than elided: the two `Menu(...)` blocks are siblings inside one call, so an implementer cannot otherwise tell where the existing block ends. Add these into the existing `return Menu(...)` — the first two lines go above its `Menu.SEPARATOR` that precedes the sleep submenu, and the second block goes directly after the sleep submenu's closing `)),`.

- [x] **Step 8: Run both menu test classes**

Run: `uv run python -m unittest tests.test_main.QtNoiseReductionMenuTests tests.test_main.PystrayNoiseReductionMenuTests -v 2>&1 | tail -12`

Expected: all `ok`.

- [x] **Step 9: Run the whole suite**

Run: `uv run python -m unittest discover -s tests 2>&1 | tail -5`

Expected: `OK`. The existing `OfflineMessageWiringTests` assert by membership over `RecordingAction.instances`, so the new actions must not break them.

- [x] **Step 10: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "feat: add the noise reduction toggle to both tray backends"
```

---

### Task 5: Confirm the bytes against real hardware

**Files:**
- None. This task only runs commands and records the outcome.

**Interfaces:**
- Consumes: everything from Tasks 1-4.
- Produces: no code. Its output is the knowledge that `0x0C` really is off and `0x0D` really is on, which Task 1's tests only assert as literals.

The `0x0C` / `0x0D` values come from reading G-Helper's IL, not from a USB capture on this machine. If the firmware ignores both, every send returns success and the toggle would look like it works while doing nothing. Only hardware can close that gap.

- [x] **Step 1: Check the receiver is present**

Run: `lsusb | grep -i 0b05:18d6 && ls -l /dev/hidraw*`

Expected: `0b05:18d6 ASUSTek Computer, Inc. ROG STRIX Go 2.4` and a world-accessible hidraw node. If the node is not writable, apply the README udev rules first and stop until it is.

- [x] **Step 2: Turn noise reduction on and confirm it takes effect**

Run: `uv run python main.py --noise-reduction on`

Expected: `Microphone noise reduction on.` Then play a recording of speech and listen for the difference on a fan or keyboard clatter. This is the manual check the automated tests cannot do.

- [x] **Step 3: Turn it off and confirm it reverts**

Run: `uv run python main.py --noise-reduction off`

Expected: `Microphone noise reduction off.` and the background noise audibly returns. If step 2 and step 3 both report success but sound identically, the sub-command bytes are wrong; capture Armoury Crate's traffic with USBPcap and diff it against `FF 08 00 FF 04 12 F1 03 52 0C` / `... 0D` to find the real byte.

- [x] **Step 4: Confirm the state survives a restart**

Run: `uv run python main.py --noise-reduction on`, then in the tray check the status row reads `On` after restarting the tray.

Expected: the row shows the value restored from `~/.config/strix-battery/noise-reduction.json`.

- [x] **Step 5: Confirm offline handling does not lie**

Run with the headset powered off but the dongle plugged in: `uv run python main.py --noise-reduction on`

Expected: either success with the note that the headset was asleep, or the existing `Error: Headset receiver could not be reached.` What must not happen is a crash. If the send succeeds against a sleeping headset, record that in a comment on `set_noise_reduction` so a future reader knows the state file can lead the firmware.

- [x] **Step 6: Run the full suite one more time**

Run: `uv run python -m unittest discover -s tests 2>&1 | tail -5`

Expected: `OK`.

- [x] **Step 7: Commit any follow-up fix**

If step 3 found wrong bytes, correct `NOISE_REDUCTION_SUB_COMMAND` in `main.py`, update the two literal packets in `NoiseReductionPacketTests`, and commit:

```bash
git add main.py tests/test_main.py
git commit -m "fix: correct the noise reduction sub-command from a capture"
```
