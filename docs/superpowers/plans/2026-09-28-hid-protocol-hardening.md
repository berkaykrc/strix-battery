# HID Protocol Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the HID protocol layer self-documenting and safe against silent failure: name the packet byte offsets, freeze the query packet against mutation, and prove the decode path with hardware-free tests.

**Architecture:** Four surgical edits inside the existing single-module `main.py`, each anchored on named constants rather than line numbers. No new files except a focused test class. The decode logic gains named offsets so a firmware revision that shifts a byte becomes a loud constant mismatch rather than a silent "Offline". The query packet becomes `bytes` so no future edit can corrupt every subsequent query.

**Tech Stack:** Python 3.12, `unittest` + `unittest.mock`, `struct`, `fcntl`/`ioctl`.

**Spec:** Findings from the code review of 2026-09-28 against commit `abcb770`. Replaces the "P2 — magic offsets" and "`QUERY_PACKET` is a mutable module-level `bytearray`" items. The packaging and curve items live in their own plans.

**Line numbers** refer to `main.py` at `abcb770`. Anchor every edit on the quoted code or the named constant, not on a line number — Tasks 1 and 2 insert lines above the ones you edit.

## Global Constraints

- Single module: all production code stays in `main.py`. **Do not** convert to a `strix_battery/` package in this plan; a separate plan owns that.
- **All new source and test code is ASCII-only.** No literal non-ASCII characters in any new line, including comments. This is a hard rule: a literal glyph in a code block cannot be matched or typed reliably, which silently breaks exact-text edits.
- Tests live in `tests/test_main.py` only, using `unittest`. `pyproject.toml` has no pytest config and `.vscode/settings.json` sets `"python.testing.pytestEnabled": false`. Do not migrate to pytest.
- `main.py` remains importable as top-level module `main` (`pyproject.toml:25`).
- The gating command, and the only one that gates this plan:
  `uv run --no-sync python -m unittest discover -s tests -v`
  All 53 existing tests must stay green in every task. Baseline verified at `abcb770`: `Ran 53 tests ... OK`.
- **New tests must be hermetic.** Never depend on which `/dev/hidraw*` nodes the host has. Any test reaching device code must patch `os.open`, `fcntl.ioctl`, `glob.glob`, `time.sleep` and `os.close`. The host running this plan has 6 hidraw nodes; the plan's tests must pass unchanged on a host with 0 and on one with 20.
- **The ioctl constants are correct. Do not change them.** `HIDIOCGRAWINFO=0x80084803` matches `_IOR('H', 0x03, struct hidraw_devinfo)` (8 bytes). `0xC0404806`/`0xC0404807` match `HIDIOCSFEATURE(64)`/`HIDIOCGFEATURE(64)` given `HIDRAW_BUFFER_SIZE 64` and `_IOC(_IOC_WRITE|_IOC_READ, 'H', ...)` in `/usr/include/linux/hidraw.h`. Task 1 names the *byte offsets inside the response packet*, which is a different thing entirely.
- `find_node` already closes its fd on every branch, including the `struct.error` path. Do not add or remove closes there.
- No new runtime dependencies. `pyproject.toml` `dependencies` and `optional-dependencies` must be byte-identical at the end of this plan.
- Existing style: 4-space indent, double quotes, module-level `UPPER_SNAKE` constants after the imports.

## Review Focus

Five input classes this scope implies but that no test currently exercises. Each line's test is added in the task that owns the relevant code.

1. **The response packet is shorter than the code indexes into it.** A truncated or zero-length read must not raise `IndexError` out of `query()`. Today `buf` is always allocated at 64 bytes by `query()` itself, so this cannot happen from the current call path — but `decode_status` as extracted in Task 2 is a public function that a later caller could hand a short buffer. → Task 2, Step 1.
2. **Voltage and sleep-time fields are little-endian across a byte boundary.** `voltage_mv = (buf[12] << 8) | buf[11]` and `sleep_sec = (buf[22] << 8) | buf[21]`. Swapping the halves silently reports 15x the real value. No test asserts either. → Task 2, Step 1.
3. **A sleep duration whose seconds overflow the 16-bit field.** `validate_sleep_minutes` allows 1092 min = 65520 s, which fits. A future relaxation of the cap must fail loudly at encode time, not wrap silently. → Task 3, Step 1.
4. **`QUERY_PACKET` must be immutable.** If any code can write to it, the *second* and all later queries on a long-running tray send corrupted bytes. → Task 1, Step 1.
5. **A response whose `buf[1]` marker is right but whose other fields are nonsense** (voltage 0, sleep seconds 0xFFFF). Expected: a status dict with clamped values, not a crash and not a 65535-minute sleep timer. → Task 2, Step 1.

---

### Task 1: Freeze the query packet and name the response offsets

**Files:**
- Modify: `main.py` — the `QUERY_PACKET` definition (line 48) and the five decode expressions in `query()` (lines 195, 199, 201, 202)
- Test: `tests/test_main.py` — new `QueryPacketTests` class before `class SleepDurationTests`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces:
  - `main.QUERY_PACKET: bytes` — the same 64 bytes as today's `bytearray`, now immutable.
  - `main.REPLY_MARKER: int` = `0x1B`
  - `main.OFFSET_CHARGING: int` = `9`
  - `main.OFFSET_VOLUME_LSB: int` = `11`
  - `main.OFFSET_VOLUME_MSB: int` = `12`
  - `main.OFFSET_SLEEP_LSB: int` = `21`
  - `main.OFFSET_SLEEP_MSB: int` = `22`
  - `main.CHARGING_FLAG: int` = `0x0A`

  Task 2 reads all seven. Values above are the current literals, verified against the committed code at `abcb770`; do not change them in this task.

**Context you need:** `QUERY_PACKET` is a module-level `bytearray` handed straight to `fcntl.ioctl(fd, HIDIOCSFEATURE_64, QUERY_PACKET)` at line 187. `bytearray` is mutable, so any code that writes one byte corrupts every subsequent query for the life of the process — which for the tray is forever. The runtime is unaffected (a `bytearray` buffersize is accepted by `ioctl`), and the review confirmed no current code path mutates it, so this is pure hardening. `bytes` is accepted identically and is immutable by construction.

The offsets are named because a firmware revision that shifts one byte currently fails *silently* as "Offline", or worse, succeeds while reporting a wrong voltage. Naming them moves that failure to a diff a human reads.

- [ ] **Step 1: Write the failing tests**

Add these tests to `tests/test_main.py` in a new class immediately before `class SleepDurationTests(unittest.TestCase):`:

```python
class QueryPacketTests(unittest.TestCase):
    """The query packet is sent on every poll, forever, so it must not drift."""

    def test_the_query_packet_is_immutable(self):
        self.assertIsInstance(
            main.QUERY_PACKET,
            bytes,
            "a mutable bytearray lets one bad write corrupt every later query",
        )

    def test_the_query_packet_is_the_expected_sixty_four_byte_report(self):
        self.assertEqual(len(main.QUERY_PACKET), 64)
        self.assertEqual(
            main.QUERY_PACKET[:10],
            bytes([0xFF, 0x08, 0x00, 0xFD, 0x04, 0x12, 0xF1, 0x03, 0x52, 0x01]),
        )

    def test_the_named_offsets_match_the_hardcoded_response_layout(self):
        # These are the offsets the decode expressions used before they were
        # named. If a firmware revision moves a field, this test is where a
        # human should be told, not a user staring at "Offline".
        self.assertEqual(main.REPLY_MARKER, 0x1B)
        self.assertEqual(main.OFFSET_CHARGING, 9)
        self.assertEqual(main.OFFSET_CHARGING_FLAG, 0x0A)
        self.assertEqual(main.OFFSET_VOLUME_LSB, 11)
        self.assertEqual(main.OFFSET_VOLUME_MSB, 12)
        self.assertEqual(main.OFFSET_SLEEP_LSB, 21)
        self.assertEqual(main.OFFSET_SLEEP_MSB, 22)
```

Also add the names to the `from main import (...)` block in `tests/test_main.py`, keeping its existing alphabetical order. The full resulting block:

```python
from main import (
    CHARGING_FLAG,
    ERROR_OFFLINE,
    ERROR_PERMISSION,
    HEADSET_ICON,
    HIDIOCGFEATURE_64,
    HIDIOCGRAWINFO,
    MAX_SLEEP_MINUTES,
    NOTIFY_TIMEOUT_SECONDS,
    OFFSET_CHARGING,
    OFFSET_SLEEP_LSB,
    OFFSET_SLEEP_MSB,
    OFFSET_VOLUME_LSB,
    OFFSET_VOLUME_MSB,
    QUERY_PACKET,
    REPLY_MARKER,
    SLEEP_ICON,
    StrixDevice,
    mv_to_percent,
    offline_message,
    run_pyqt_tray,
    start_tray,
    validate_sleep_minutes,
)
```

If `main` is not yet imported by name in the test file, add `import main` above this block. It already is (the Qt lifecycle tests reference `main._run_qt_app_with_sigint_shutdown`).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync python -m unittest tests.test_main.QueryPacketTests -v`

Expected: `ImportError: cannot import name 'REPLY_MARKER' from 'main'`. That is the correct red state — the import fails before any test body runs.

- [ ] **Step 3: Make the packet immutable**

Replace the `QUERY_PACKET` definition in `main.py`:

```python
# 64-byte status query packet. `bytes`, not `bytearray`: this is a module-level
# constant handed straight to ioctl on every poll, so a stray single-byte write
# would corrupt every later query for the life of the process.
QUERY_PACKET = bytes([
    0xFF, 0x08, 0x00, 0xFD, 0x04, 0x12, 0xF1, 0x03, 0x52, 0x01
] + [0x00] * 54)
```

- [ ] **Step 4: Add the offset constants**

In `main.py`, immediately after the `QUERY_PACKET` definition, add:

```python
# Byte offsets inside the 64-byte status reply. Named so a firmware revision
# that shifts one field shows up as a diff a human reads, instead of a user
# staring at a permanent "Offline" with no hint why. Voltage and sleep time
# are little-endian 16-bit values split across LSB and MSB.
REPLY_MARKER = 0x1B
OFFSET_CHARGING = 9
CHARGING_FLAG = 0x0A
OFFSET_VOLUME_LSB = 11
OFFSET_VOLUME_MSB = 12
OFFSET_SLEEP_LSB = 21
OFFSET_SLEEP_MSB = 22
```

- [ ] **Step 5: Use the constants in `query`**

In `main.py`, replace the five literals inside `query()`. Anchor on the surrounding code; the four decode lines are adjacent:

```python
            # A powered-off or disconnected headset returns zeroed bytes.
            if buf[1] != REPLY_MARKER:
                self._go_offline()
                return None

            voltage_mv = (buf[OFFSET_VOLUME_MSB] << 8) | buf[OFFSET_VOLUME_LSB]
            pct = self.stabilize_percentage(mv_to_percent(voltage_mv))
            charging = buf[OFFSET_CHARGING] == CHARGING_FLAG
            sleep_sec = (buf[OFFSET_SLEEP_MSB] << 8) | buf[OFFSET_SLEEP_LSB]
```

Note the `buf[1]` becomes `buf[REPLY_MARKER]`. **`REPLY_MARKER` is the marker's byte value (0x1B), not its offset** — the marker lives at index 1 in the buffer, which is structural (the report ID is always the first byte) and is not one of the negotiated fields. Leave that index as the literal `1`; naming it as an "offset" alongside the others would invite someone to move it.

- [ ] **Step 6: Run the full suite to verify it passes**

Run: `uv run --no-sync python -m unittest discover -s tests -v`

Expected: `Ran 57 tests ... OK`. (53 existing plus 4 new.) All 53 existing tests must still pass — the offset renames are behaviour-preserving and the existing `asus_ioctl` fake in `LastErrorTests` writes to the same byte indices, so it keeps working unchanged.

- [ ] **Step 7: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "refactor: name the HID response offsets and freeze the query packet"
```

---

### Task 2: Extract the response decode and pin it with tests

**Files:**
- Modify: `main.py` — add `decode_status(buf, path)` as a module-level function, placed immediately before `class StrixDevice`
- Modify: `main.py` — the decode block in `query()`, replaced by a call to `decode_status`
- Test: `tests/test_main.py` — new `DecodeStatusTests` class before `class SleepDurationTests`

**Interfaces:**
- Consumes: all seven constants from Task 1.
- Produces: `main.decode_status(buf: bytes | bytearray, path: str) -> dict | None`

  Returns `None` when `buf[REPLY_MARKER-index-1]` is not the marker, so the caller keeps the offline transition. On success returns exactly the dict `query()` builds today, with keys `percentage`, `voltage`, `charging`, `sleep_min`, `path` — unchanged, because `update_state` in both tray backends and `main()`'s JSON output all read those keys.

  This function is pure: no `self`, no device state, no IO. That is what makes the decode testable without hardware. It does **not** apply `stabilize_percentage` — the display hold is device state and stays in `query()`. It does **not** fire the low-battery notification either.

**Context you need:** the decode is currently unreachable in a test. `tests/test_main.py:164-175` has an `asus_ioctl` fake that writes the right bytes, but only two tests use it, and both assert on `last_error`, never on the decoded values. So `voltage_mv`, `charging` and `sleep_sec` are computed by production code on every poll and asserted by nothing. An endianness slip would ship.

Extracting the pure part is what closes that. Keep it pure: if you find yourself wanting `self` in it, the thing you want is in `query()`.

- [ ] **Step 1: Write the failing tests**

Insert this class immediately before `class SleepDurationTests(unittest.TestCase):`:

```python
def make_reply(voltage=3904, charging=True, sleep_sec=0, marker=0x1B):
    """Build a 64-byte status reply the way the receiver would send it."""
    buf = bytearray(64)
    buf[1] = marker
    buf[main.OFFSET_CHARGING] = 0x0A if charging else 0x03
    buf[main.OFFSET_VOLUME_LSB] = voltage & 0xFF
    buf[main.OFFSET_VOLUME_MSB] = (voltage >> 8) & 0xFF
    buf[main.OFFSET_SLEEP_LSB] = sleep_sec & 0xFF
    buf[main.OFFSET_SLEEP_MSB] = (sleep_sec >> 8) & 0xFF
    return buf


class DecodeStatusTests(unittest.TestCase):
    """The decode is pure, so every field can be asserted without hardware."""

    def test_decodes_a_little_endian_voltage(self):
        status = main.decode_status(make_reply(voltage=3904), "/dev/hidraw3")
        self.assertEqual(status["voltage"], 3904)

    def test_decodes_a_voltage_whose_high_byte_is_not_zero(self):
        # 0x1000 = 4096mV. A swapped LSB/MSB would read 16, not 4096.
        status = main.decode_status(make_reply(voltage=4096), "/dev/hidraw3")
        self.assertEqual(status["voltage"], 4096)

    def test_decodes_the_charging_flag(self):
        self.assertIs(main.decode_status(make_reply(charging=True), "/d")["charging"], True)
        self.assertIs(main.decode_status(make_reply(charging=False), "/d")["charging"], False)

    def test_decodes_sleep_seconds_as_whole_minutes(self):
        status = main.decode_status(make_reply(sleep_sec=1800), "/dev/hidraw3")
        self.assertEqual(status["sleep_min"], 30)

    def test_carries_the_device_path_through(self):
        status = main.decode_status(make_reply(), "/dev/hidraw7")
        self.assertEqual(status["path"], "/dev/hidraw7")

    def test_a_missing_marker_means_offline(self):
        self.assertIsNone(main.decode_status(make_reply(marker=0x00), "/dev/hidraw3"))

    def test_clamps_a_garbage_voltage_to_a_valid_percentage(self):
        # An all-ones voltage field must not produce a nonsense percentage.
        status = main.decode_status(make_reply(voltage=0xFFFF), "/dev/hidraw3")
        self.assertEqual(status["percentage"], 100)
        self.assertEqual(status["percentage"], mv_to_percent(0xFFFF))

    def test_a_maximum_sleep_field_does_not_crash(self):
        status = main.decode_status(make_reply(sleep_sec=0xFFFF), "/dev/hidraw3")
        self.assertEqual(status["sleep_min"], 1092)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync python -m unittest tests.test_main.DecodeStatusTests -v`

Expected: `AttributeError: module 'main' has no attribute 'decode_status'`. That is the correct red state.

- [ ] **Step 3: Add `decode_status`**

In `main.py`, immediately before `class StrixDevice:`, add:

```python
def decode_status(buf, path):
    """Turn a raw 64-byte status reply into a status dict, or None.

    Pure on purpose: no device state, no IO. That is what lets every field be
    asserted in a test without a receiver plugged in. The display hold and the
    low-battery notice stay in `query()`, because those are device state.
    """
    if buf[1] != REPLY_MARKER:
        return None

    voltage_mv = (buf[OFFSET_VOLUME_MSB] << 8) | buf[OFFSET_VOLUME_LSB]
    return {
        "percentage": mv_to_percent(voltage_mv),
        "voltage": voltage_mv,
        "charging": buf[OFFSET_CHARGING] == CHARGING_FLAG,
        "sleep_min": ((buf[OFFSET_SLEEP_MSB] << 8) | buf[OFFSET_SLEEP_LSB]) // 60,
        "path": path,
    }
```

Note this returns the **raw** `mv_to_percent(voltage_mv)`, not the stabilised value. `query()` wraps it:

- [ ] **Step 4: Call it from `query`**

In `main.py`, replace the block inside `query()` that Task 1 rewrote — from the `if buf[1] != REPLY_MARKER:` check through the closing `}` of the `status` dict — with:

```python
            status = decode_status(buf, path)
            if status is None:
                self._go_offline()
                return None

            status["percentage"] = self.stabilize_percentage(status["percentage"])

            self.last_status = status
            self.last_error = None

            self._update_low_battery_notice(
                status["percentage"], status["charging"]
            )

            return status
```

The `time.sleep(0.04)` between the `HIDIOCSFEATURE_64` write and the `HIDIOCGFEATURE_64` read, and the `try`/`except OSError`/`finally: os.close(fd)` wrapper around all of it, are unchanged.

- [ ] **Step 5: Run the full suite to verify it passes**

Run: `uv run --no-sync python -m unittest discover -s tests -v`

Expected: `Ran 65 tests ... OK`. (57 from Task 1, plus 8 in `DecodeStatusTests`.)

- [ ] **Step 6: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "refactor: extract the pure status decode and pin its fields with tests"
```

---

### Task 3: Pin the sleep-timer packet encoding

**Files:**
- Modify: `main.py` — the `seconds = minutes * 60` block and the `payload` construction in `set_sleep`
- Test: `tests/test_main.py` — new `SleepPacketTests` class before `class SleepDurationTests`

**Interfaces:**
- Consumes: `validate_sleep_minutes` and `MAX_SLEEP_MINUTES` (both already exist), plus the offset constant style from Task 1.
- Produces:
  - `main.SLEEP_PACKET_COMMAND: bytes` — the leading `0xFF 0x0A 0x00 0xFF 0x04 0x12 0xF1 0x05 0x52 0x0B` header, frozen like `QUERY_PACKET`
  - `main.OFFSET_SLEEP_SECONDS_LSB: int` = `10`
  - `main.OFFSET_SLEEP_SECONDS_MSB: int` = `11`
  - `main.build_sleep_packet(minutes: int) -> bytes` — pure; validates, encodes, returns 64 bytes. `set_sleep` calls it and does the IO.

**Context you need:** `set_sleep` builds its payload inline at `main.py:239-242` and hands the `bytearray` to ioctl. Two things are untested and both matter: the seconds encoding is little-endian across bytes 10 and 11, and the 16-bit field can silently wrap. `MAX_SLEEP_MINUTES = 1092` gives 65520 s, which fits in 16 bits with 16 to spare — so the cap exists *because of this field*. If the cap is ever relaxed, a wrap would set the sleep timer to the wrong value with no error. Extracting a pure `build_sleep_packet` lets a test assert both.

Note the encoding is **not** in `main.py:202`'s `sleep_sec` offsets — those describe the *read* path's reply buffer. The write path uses bytes 10 and 11 of a different packet. Keep the two sets of constants visually distinct and separately named.

- [ ] **Step 1: Write the failing tests**

Insert this class immediately before `class SleepDurationTests(unittest.TestCase):`:

```python
class SleepPacketTests(unittest.TestCase):
    """The 16-bit seconds field wraps silently, so the encoding must be pinned."""

    def test_encodes_minutes_as_little_endian_seconds(self):
        packet = main.build_sleep_packet(30)
        lsb = packet[main.OFFSET_SLEEP_SECONDS_LSB]
        msb = packet[main.OFFSET_SLEEP_SECONDS_MSB]
        self.assertEqual((msb << 8) | lsb, 1800)

    def test_the_maximum_duration_does_not_wrap(self):
        packet = main.build_sleep_packet(MAX_SLEEP_MINUTES)
        lsb = packet[main.OFFSET_SLEEP_SECONDS_LSB]
        msb = packet[main.OFFSET_SLEEP_SECONDS_MSB]
        encoded = (msb << 8) | lsb
        self.assertEqual(encoded, 65520)
        self.assertLess(encoded, 1 << 16, "the seconds field must not overflow")

    def test_zero_never_turns_the_headset_off(self):
        packet = main.build_sleep_packet(0)
        lsb = packet[main.OFFSET_SLEEP_SECONDS_LSB]
        msb = packet[main.OFFSET_SLEEP_SECONDS_MSB]
        self.assertEqual(((msb << 8) | lsb), 0)

    def test_the_packet_is_sixty_four_bytes(self):
        self.assertEqual(len(main.build_sleep_packet(5)), 64)

    def test_the_command_header_is_frozen(self):
        self.assertEqual(
            main.SLEEP_PACKET_COMMAND,
            bytes([0xFF, 0x0A, 0x00, 0xFF, 0x04, 0x12, 0xF1, 0x05, 0x52, 0x0B]),
        )

    def test_the_packet_is_immutable(self):
        self.assertIsInstance(main.build_sleep_packet(5), bytes)

    def test_rejects_a_duration_that_would_overflow_the_field(self):
        with self.assertRaises(ValueError):
            main.build_sleep_packet(MAX_SLEEP_MINUTES + 1)
```

Add `SLEEP_PACKET_COMMAND` to the `from main import (...)` block. The other names in the test body are reached through the `main.` prefix, so they need no import.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync python -m unittest tests.test_main.SleepPacketTests -v`

Expected: `AttributeError: module 'main' has no attribute 'build_sleep_packet'`. Correct red state.

- [ ] **Step 3: Add the constants and the pure encoder**

In `main.py`, immediately after the `OFFSET_SLEEP_MSB = 22` block from Task 1, add:

```python
# The sleep-timer command is a different packet from the status query, and its
# own seconds field. Kept separate from the read-path offsets above so nobody
# confuses the two.
SLEEP_PACKET_COMMAND = bytes([
    0xFF, 0x0A, 0x00, 0xFF, 0x04, 0x12, 0xF1, 0x05, 0x52, 0x0B
])
OFFSET_SLEEP_SECONDS_LSB = 10
OFFSET_SLEEP_SECONDS_MSB = 11
```

Then insert `build_sleep_packet` immediately after `validate_sleep_minutes`:

```python
def build_sleep_packet(minutes: int) -> bytes:
    """Encode the sleep-timer command. Pure, so the encoding can be tested.

    The seconds field is 16 bits, which is why MAX_SLEEP_MINUTES is 1092.
    The assert is a backstop: if the cap is ever relaxed, a wrap would set the
    timer to a wrong value with no error rather than failing loudly.
    """
    validate_sleep_minutes(minutes)
    seconds = minutes * 60
    assert 0 <= seconds < (1 << 16), f"{seconds}s does not fit the 16-bit field"

    packet = bytearray(64)
    packet[: len(SLEEP_PACKET_COMMAND)] = SLEEP_PACKET_COMMAND
    packet[OFFSET_SLEEP_SECONDS_LSB] = seconds & 0xFF
    packet[OFFSET_SLEEP_SECONDS_MSB] = (seconds >> 8) & 0xFF
    return bytes(packet)
```

- [ ] **Step 4: Call it from `set_sleep`**

In `main.py`, replace the whole `set_sleep` method with:

```python
    def set_sleep(self, minutes: int):
        """Set the automatic sleep timer (0 = never turn off)."""
        try:
            payload = build_sleep_packet(minutes)
        except ValueError:
            return False

        fd, _ = self.find_node()
        if fd is None:
            return False

        try:
            fcntl.ioctl(fd, HIDIOCSFEATURE_64, payload)
            self.notify("ROG Strix Go", f"Sleep timer set to {minutes} minutes.")
            self.query()
            return True
        except OSError:
            return False
        finally:
            os.close(fd)
```

The `try/except ValueError` is retained deliberately: `main()` already rejects out-of-range values via `parser.error` before calling `set_sleep`, and a tray menu item cannot. Swallowing here keeps the tray from crashing on a programming error. A separate plan owns whether that `False` is ambiguous enough to change — do not change it here.

- [ ] **Step 5: Run the full suite to verify it passes**

Run: `uv run --no-sync python -m unittest discover -s tests -v`

Expected: `Ran 72 tests ... OK`. (65 from Task 2, plus 7 in `SleepPacketTests`.)

- [ ] **Step 6: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "refactor: pin the sleep-timer packet encoding against silent wrap"
```

---

## Verification Record

Measured on the host running this plan, `abcb770`, Python 3.12.12, Pillow 12.3, 6 hidraw nodes present:

- Baseline before this plan: `Ran 53 tests ... OK`.
- `QUERY_PACKET` is `bytearray`, length 64, first 10 bytes `FF 08 00 FD 04 12 F1 03 52 01` — the values Task 1's tests pin.
- The decode expressions are at `main.py:195` (`buf[1] != 0x1B`), `:199` (voltage), `:201` (charging), `:202` (sleep seconds) — the offsets Task 1 names.
- `set_sleep` builds its 12-byte header plus 52 zeros inline at `main.py:239-242` — the bytes Task 3 freezes.
- `MAX_SLEEP_MINUTES * 60 == 65520 < 65536`, so the maximum duration does not wrap today. Task 3's `assert` is what keeps that true if the cap moves.
- No current code path mutates `QUERY_PACKET` or the sleep payload; both are hardening, not bug fixes.

## Out of Scope

- **The `pygobject` gap in the `fallback-tray` extra** — a separate plan owns packaging and dependency changes.
- **Naming `main` as a top-level module** (`py-modules = ["main"]` lands a generic `main` in `site-packages`) — a separate packaging plan.
- **`O_RDWR` during enumeration.** `find_node` opens every hidraw node read-write only to read 8 bytes of `HIDIOCGRAWINFO`, then closes the non-matching ones. Probing `O_RDONLY` first and reopening read-write only on a VID/PID match would be least privilege. It is a real, small improvement, but it changes the fd lifecycle in the one method the review already confirmed has no leak, and it needs a device to verify against. Deliberately not bundled here.
- **The linear discharge curve.** Separate plan.
