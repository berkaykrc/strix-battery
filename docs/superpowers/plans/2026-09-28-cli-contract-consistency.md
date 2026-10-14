# CLI Contract Consistency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the command-line contract coherent: `--json` and the text report must agree on the offline exit code, and `set_sleep` must stop reporting a programming error as an unreachable device.

**Architecture:** Extract `main()`'s three modes into small pure-ish functions so each can be tested with a fake device, then unify the offline exit code in one named constant. No behaviour change to what is printed — only to what is *returned*, plus honest error attribution for the sleep timer.

**Tech Stack:** Python 3.12, `unittest` + `unittest.mock`, `argparse`, `json`, `subprocess`.

**Spec:** Findings from the code review of 2026-09-28 against commit `abcb770` — the "P2 — exit-code contract differs per output mode" and "P2 — `set_sleep` swallows its own validation error" items. Packaging lives in a separate plan.

**Line numbers** refer to `main.py` at `abcb770`. Anchor every edit on the quoted code or the named function, not on a line number — Task 1 inserts lines above the ones you edit.

## Global Constraints

- Single module: all production code stays in `main.py`. **Do not** convert to a `strix_battery/` package; a separate plan owns that.
- **All new source and test code is ASCII-only.** No literal non-ASCII characters in any new line, including comments. A literal glyph in a code block cannot be matched or typed reliably, which silently breaks exact-text edits.
- **What is printed must not change in this plan.** Byte-for-byte identical stdout and stderr for every invocation, both online and offline. This plan changes only the process **exit code** and the **wording of one error message**. If a step tempts you to reword output, it belongs in a different plan.
- Tests live in `tests/test_main.py` only, using `unittest`. Do not migrate to pytest; `pyproject.toml` has no pytest config and `.vscode/settings.json` sets `"python.testing.pytestEnabled": false`.
- `main.py` remains importable as top-level module `main`.
- The gating command, and the only one that gates this plan:
  `uv run --no-sync python -m unittest discover -s tests -v`
  All 53 existing tests must stay green in every task. Baseline verified at `abcb770`: `Ran 53 tests ... OK`.
- **New tests must be hermetic.** Never depend on which `/dev/hidraw*` nodes the host has. Patch `main.glob.glob` (or inject a fake device) in every test that reaches device code.
- No new runtime dependencies.
- The exit code for a successful run stays `0`. Only the offline case changes.
- Existing style: 4-space indent, double quotes, module-level `UPPER_SNAKE` constants after the imports.

## Review Focus

Five input classes this scope implies but that no test currently exercises. Each line's test is added in the task that owns the relevant code.

1. **`--json` while offline.** This is the single most common failure for a Waybar/Polybar script: the panel polls every few seconds, the headset is asleep, and the script must be able to tell "no battery" from "crashed". Today it cannot, because `--json` exits `0` and the text report exits `1`. → Task 2, Step 1.
2. **A Waybar script that keys on the exit code must break loudly today.** The regression this plan exists to fix: a script written against the text report's `exit 1` silently stops showing the offline state when someone adds `--json`. A test must assert the two modes agree. → Task 2, Step 1.
3. **`--set-sleep 99999`** (above the 1092 cap). Expected: `parser.error`, exit `2`, argparse's usage message — not a generic "receiver could not be reached". Today `main()` validates first and this works, so the test is a pin against regression, not a fix. → Task 1, Step 1.
4. **`--set-sleep -1`.** `argparse` accepts it as a valid integer; only `validate_sleep_minutes` rejects it. This is the path that proves the validation is reached at all. → Task 1, Step 1.
5. **`--set-sleep 30` with the receiver unplugged.** Expected: the user is told the *receiver* is unreachable, and exit is `1`. Distinct from case 3, where the cause is the argument. → Task 1, Step 1.

---

### Task 1: Report a bad sleep argument as a bad argument

**Files:**
- Modify: `main.py` — the `--set-sleep` branch of `main()` (lines 649-661)
- Test: `tests/test_main.py` — new `CliSleepTests` class before `class SleepDurationTests`

**Interfaces:**
- Consumes: `validate_sleep_minutes(minutes: int) -> int` and `MAX_SLEEP_MINUTES` (both already exist), and `StrixDevice.set_sleep(minutes: int) -> bool` unchanged.
- Produces: no new production API. `main()` keeps its signature and its `sys.exit` behaviour.

**Context you need:** `main()` already validates before calling, so the visible CLI is honest today. The defect is one layer down: `set_sleep` at `main.py:225-228` catches its own `ValueError` and returns `False`, and `False` means "could not reach the device" to every caller. A tray menu item and a `main()` bug both produce the identical result, so a future change that reaches `set_sleep` with a bad value gets diagnosed as absent hardware.

This task makes the CLI path's *attribution* explicit and testable, and gives `set_sleep` a way to say which failure it was. Task 2 then uses the same mechanism for the exit code.

The distinction to encode: an invalid `minutes` is a caller error (reject loudly, exit 2, argparse's own format), while a valid `minutes` that cannot be delivered is a device problem (message, exit 1).

- [x] **Step 1: Write the failing tests**

Add `sys` and `io` to the test file's imports if not present, then insert this class immediately before `class SleepDurationTests(unittest.TestCase):`:

```python
class CliSleepTests(unittest.TestCase):
    """--set-sleep must distinguish a bad argument from an absent device."""

    def run_main(self, argv, device=None):
        """Run main() with a stub device; return (exit_code, stdout, stderr)."""
        device = device or FakeDevice()
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["strix-battery"] + argv), patch(
            "main.StrixDevice", return_value=device
        ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                main.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_a_negative_duration_is_rejected_as_a_usage_error(self):
        code, _, err = self.run_main(["--set-sleep", "-1"])
        self.assertEqual(code, 2, "argparse usage errors exit 2")
        self.assertIn("must be between", err)

    def test_a_duration_above_the_cap_is_rejected_as_a_usage_error(self):
        code, _, err = self.run_main(["--set-sleep", str(MAX_SLEEP_MINUTES + 1)])
        self.assertEqual(code, 2)
        self.assertIn("must be between", err)

    def test_a_valid_duration_with_no_receiver_reports_the_receiver(self):
        device = FakeDevice()
        device.sleep_result = False
        code, out, _ = self.run_main(["--set-sleep", "30"], device)
        self.assertEqual(code, 1)
        self.assertIn("could not be reached", out)

    def test_a_successful_set_sleep_exits_zero(self):
        device = FakeDevice()
        device.sleep_result = True
        code, out, _ = self.run_main(["--set-sleep", "30"], device)
        self.assertEqual(code, 0)
        self.assertIn("Sleep timer set to 30 minutes.", out)

    def test_the_validated_minutes_reach_the_device(self):
        device = FakeDevice()
        self.run_main(["--set-sleep", "45"], device)
        self.assertEqual(device.sleep_calls, [45])
```

`FakeDevice` exists at `tests/test_main.py:597` but has no `set_sleep`. Add to its `__init__`, after the `self.last_error = None` line:

```python
        self.sleep_result = True
        self.sleep_calls = []
```

and add the method to `FakeDevice`, immediately after its existing `query` method:

```python
    def set_sleep(self, minutes):
        self.sleep_calls.append(minutes)
        return self.sleep_result
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync python -m unittest tests.test_main.CliSleepTests -v`

Expected: `AttributeError: 'FakeDevice' object has no attribute 'sleep_result'`. Correct red state — the fixture does not exist yet.

- [x] **Step 3: Make the sleep failure attributable**

In `main.py`, replace the whole `--set-sleep` branch of `main()` with:

```python
    if args.set_sleep is not None:
        # An out-of-range value is a caller error, not a device problem, so it
        # gets argparse's usage-error format and exit 2. Only a value that
        # validated but could not be delivered is a reachability failure.
        try:
            validate_sleep_minutes(args.set_sleep)
        except ValueError as exc:
            parser.error(str(exc))

        if not device.set_sleep(args.set_sleep):
            print("Error: Headset receiver could not be reached.")
            sys.exit(EXIT_OFFLINE)
        print(f"Sleep timer set to {args.set_sleep} minutes.")
        return
```

The `success` variable and the duplicated print collapse into one path. The two `print` calls are on mutually exclusive branches, so stdout is unchanged.

- [x] **Step 4: Add the exit-code constants**

In `main.py`, immediately after the `MAX_SLEEP_MINUTES = 1092` line, add:

```python
# Process exit codes, so a panel script can tell "the headset is not there"
# from "the program crashed". Waybar and Polybar modules key on these.
EXIT_OK = 0
EXIT_OFFLINE = 1
EXIT_USAGE = 2
```

`EXIT_USAGE` documents argparse's own convention. It is not raised by hand anywhere in this plan — `parser.error` produces it.

- [x] **Step 5: Run the full suite to verify it passes**

Run: `uv run --no-sync python -m unittest discover -s tests -v`

Expected: `Ran 58 tests ... OK`. (53 existing plus 5 new.)

- [x] **Step 6: Verify the real CLI, not just the fakes**

```bash
uv run --no-sync python main.py --set-sleep -1; echo "exit=$?"
uv run --no-sync python main.py --set-sleep 99999; echo "exit=$?"
```

Expected: both print argparse's usage block to stderr ending in the "must be between 0 and 1092 minutes" message, and both report `exit=2`.

- [x] **Step 7: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "fix: reject a bad sleep duration as a usage error, not a missing device"
```

---

### Task 2: Make --json and the text report agree on offline

**Files:**
- Modify: `main.py` — the status-report branch of `main()` (lines 663-691)
- Modify: `main.py` — add `render_json_status` and `render_text_status` as module-level functions, placed immediately before `def main():`
- Test: `tests/test_main.py` — new `ExitCodeContractTests` class before `class SleepDurationTests`

**Interfaces:**
- Consumes: `EXIT_OK` and `EXIT_OFFLINE` from Task 1, `StrixDevice.query()`, and `offline_message(last_error)` (existing).
- Produces:
  - `main.render_json_status(st: dict | None) -> str` — the exact JSON line printed in `--json` mode, including the trailing newline. Returns the string rather than printing it, so a test can assert on it without capturing stdout.
  - `main.render_text_status(st: dict) -> str` — the five-line text report for the online case only. Callers must handle the offline case themselves, because the offline text is a *different kind of thing*: an error message, not a report.
  - `EXIT_JSON_PARSE_ERROR: int` = `0` is **not** introduced; JSON output stays exit `0` on success.

**Context you need:** verified at `abcb770` by running `main()` with `glob.glob` patched to return `[]`:

| invocation | exit | stdout |
| --- | --- | --- |
| `strix-battery --json` | **0** | `{"status": "offline", "text": "Headset offline"}` |
| `strix-battery` | **1** | `ROG Strix Go 2.4 receiver or headset is offline/out of range.` |

A Waybar module that runs the command and keys on a non-zero exit to render a fallback sees "healthy" from `--json` and "broken" from the text report, for the identical physical state. That is the bug.

Both output strings must stay **byte-identical**. Only the exit code moves.

- [x] **Step 1: Write the failing tests**

Insert this class immediately before `class SleepDurationTests(unittest.TestCase):`:

```python
class ExitCodeContractTests(unittest.TestCase):
    """A panel script must not see two different verdicts for one headset."""

    def run_main(self, argv, query_result):
        device = FakeDevice()
        device.query_result = query_result
        out = io.StringIO()
        with patch.object(sys, "argv", ["strix-battery"] + argv), patch(
            "main.StrixDevice", return_value=device
        ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                main.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue()

    ONLINE = {
        "percentage": 72,
        "voltage": 3904,
        "charging": True,
        "sleep_min": 30,
        "path": "/dev/hidraw3",
    }

    def test_both_modes_report_offline_with_the_same_exit_code(self):
        json_code, _ = self.run_main(["--json"], None)
        text_code, _ = self.run_main([], None)
        self.assertEqual(
            json_code,
            text_code,
            "a Waybar script keying on the exit code silently breaks when only"
            " one of these modes changes",
        )
        self.assertEqual(json_code, main.EXIT_OFFLINE)

    def test_both_modes_report_online_with_exit_zero(self):
        json_code, _ = self.run_main(["--json"], self.ONLINE)
        text_code, _ = self.run_main([], self.ONLINE)
        self.assertEqual(json_code, 0)
        self.assertEqual(text_code, 0)

    def test_json_offline_output_is_unchanged(self):
        _, out = self.run_main(["--json"], None)
        self.assertEqual(
            out,
            json.dumps({"status": "offline", "text": "Headset offline"}) + "\n",
        )

    def test_json_online_output_is_unchanged(self):
        _, out = self.run_main(["--json"], self.ONLINE)
        self.assertEqual(json.loads(out)["percentage"], 72)
        self.assertEqual(json.loads(out)["text"], "%72 (Charging)")
        self.assertEqual(json.loads(out)["sleep_min"], 30)

    def test_text_online_output_is_unchanged(self):
        _, out = self.run_main([], self.ONLINE)
        self.assertIn("--- ROG Strix Go 2.4 Status ---", out)
        self.assertIn("Battery: 72% (3904 mV)", out)
        self.assertIn("Power status: Charging", out)
        self.assertIn("Sleep timer: 30 minutes", out)
        self.assertIn("Device node: /dev/hidraw3", out)

    def test_json_output_is_a_single_parseable_line(self):
        _, out = self.run_main(["--json"], self.ONLINE)
        self.assertEqual(len(out.strip().splitlines()), 1)
```

`FakeDevice` needs a configurable `query` result. Change its `query` method to:

```python
    def query(self):
        self.query_threads.append(threading.get_ident())
        self.query_finished.set()
        if self.query_error is not None:
            raise self.query_error
        if self.after_query is not None:
            self.after_query()
        return self.query_result
```

and add `self.query_result = self.status` to its `__init__`, immediately after the `self.status = {...}` block. The default preserves every existing test, all of which expect the old canned status dict.

- [x] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync python -m unittest tests.test_main.ExitCodeContractTests -v`

Expected: `test_both_modes_report_offline_with_the_same_exit_code` fails with `0 != 1`. That is the correct red state, and its failure message is the bug report.

- [x] **Step 3: Extract the two renderers**

In `main.py`, immediately before `def main():`, add:

```python
def render_json_status(st) -> str:
    """Render the single JSON line a panel script consumes.

    Returned rather than printed so the exact bytes are assertable in a test.
    """
    if st is None:
        return json.dumps({"status": "offline", "text": "Headset offline"})
    charge_str = " (Charging)" if st["charging"] else ""
    return json.dumps({
        "status": "online",
        "percentage": st["percentage"],
        "voltage": st["voltage"],
        "charging": st["charging"],
        "sleep_min": st["sleep_min"],
        "text": f"%{st['percentage']}{charge_str}",
    })


def render_text_status(st) -> str:
    """Render the human-facing report. The offline case is an error, not a report."""
    state = "Charging" if st["charging"] else "On battery"
    return (
        "--- ROG Strix Go 2.4 Status ---\n"
        f"Battery: {st['percentage']}% ({st['voltage']} mV)\n"
        f"Power status: {state}\n"
        f"Sleep timer: {st['sleep_min']} minutes\n"
        f"Device node: {st['path']}\n"
    )
```

`render_text_status` takes a non-`None` `st` only. Handling `None` inside would force a "not a report" string through a function named "report".

- [x] **Step 4: Use them in `main` and unify the exit code**

In `main.py`, replace everything from the `st = device.query()` line through the end of the function (the `# 3. Query status` section) with:

```python
    st = device.query()

    if args.json:
        print(render_json_status(st))
        if st is None:
            sys.exit(EXIT_OFFLINE)
        return

    if st is None:
        print("ROG Strix Go 2.4 receiver or headset is offline/out of range.")
        sys.exit(EXIT_OFFLINE)

    print(render_text_status(st), end="")
```

The `end=""` matters: `render_text_status` already ends its last line with `\n`, and `print` would add a second one. The byte count of stdout is unchanged from `abcb770` in every branch.

- [x] **Step 5: Run the full suite to verify it passes**

Run: `uv run --no-sync python -m unittest discover -s tests -v`

Expected: `Ran 64 tests ... OK`. (58 from Task 1, plus 6 new.)

- [x] **Step 6: Verify the real CLI, both modes, offline**

```bash
uv run --no-sync python main.py --json; echo "json exit=$?"
uv run --no-sync python main.py; echo "text exit=$?"
```

Expected: the two stdout strings are exactly as they are at `abcb770`, and both exit statuses now read `1`. Confirm the strings did not move:

```bash
uv run --no-sync python main.py --json | od -c | tail -3
```

Expected: the line ends with `"Headset offline"}\n` — one trailing newline, not two.

- [x] **Step 7: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "fix: make --json and the text report agree on the offline exit code"
```

---

## Verification Record

Measured on the host running this plan, `abcb770`, with `glob.glob` patched to return `[]` so no real device is touched:

- `strix-battery --json` → exit `0`, stdout `{"status": "offline", "text": "Headset offline"}`
- `strix-battery` → exit `1`, stdout `ROG Strix Go 2.4 receiver or headset is offline/out of range.`
- The two disagree, which is the defect. Both strings above are the byte-exact output this plan must preserve.
- `main.py:666-679` is the `--json` block that returns `0`; `main.py:682-684` is the text block that exits `1`.
- `set_sleep(-1)` and `set_sleep(99999)` both return `False` today, identical to the absent-device case — verified by calling them with `find_node` patched to return `(None, None)`.

## Out of Scope

- **The tray path's `last_error` handling.** The Qt tray backend already uses `offline_message`, which is correct and already tested. This plan is the CLI only.
- **`main.py`'s `except ImportError` around `run_pyqt_tray`.** The tray-availability guard raises `SystemExit`, which correctly escapes this. Not a defect.
- **Packaging the module** (`py-modules = ["main"]`). Separate plan.

## Execution record (2026-10-14)

The steps above are checked off because they were carried out, but four of them
did not match the code as it stood, and the plan's own numbers were stale. What
actually happened:

1. **Baseline drift.** The plan is written against `abcb770`; it was executed at
   `d0b674e`, after the module split. The suite was already **82** tests, not 53,
   so the step checkpoints are 82 -> 87 (Task 1) and 87 -> 93 (Task 2), not
   58 and 64.
2. **Task 1, Step 1 - `FakeDevice` did not exist.** The plan says it "exists at
   `tests/test_main.py:597` and has no `set_sleep`". The module split removed it.
   It was written fresh, carrying the `sleep_result` / `sleep_calls` / `set_sleep`
   surface the step prescribes, so the step's intent held and only its premise
   was wrong.
3. **Task 1, Step 4 - no `MAX_SLEEP_MINUTES = 1092` line in `main.py`.** It moved
   to `device.py` during the split, so `EXIT_OK` / `EXIT_OFFLINE` / `EXIT_USAGE`
   went after the import block in `main.py`, which is where the global constraint
   (production code stays in `main.py`) puts them.
4. **Task 2 - the Interfaces text contradicts Steps 3 and 4.** The Interfaces say
   `render_json_status` returns the JSON line "including the trailing newline",
   while Steps 3 and 4 print it with a bare `print()`, which would emit two.
   Steps 3 and 4 were followed: the renderer returns no trailing newline and
   `print` supplies the single one the plan's own `od -c` check requires.
   `render_text_status` does end with a newline and is printed with `end=""`.

Beyond the plan, the post-review fix pass added three byte-exact output pins that
no step asked for (`test_json_output_is_byte_exact`, `test_the_text_report_is_byte_exact`,
`test_text_offline_output_is_unchanged`; suite 96), and one follow-up commit cleared
the deferred review minors. The version bumped once, in the change's first commit.
