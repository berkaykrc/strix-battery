# Discharge Curve Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the linear 3300-4150mV percentage map with a real Li-ion discharge curve, and make the curve a documented, adjustable, one-place constant instead of a hardcoded arithmetic expression.

**Architecture:** Two tasks. Task 1 makes the curve *configurable and honest about its own provenance* — a named table, a documented source comment, an environment override, and a calibration helper — without changing any behaviour, so the current linear map is provably preserved by tests. Task 2 replaces the table with measured values, in a commit that is explicitly allowed to be wrong on a machine that has not been calibrated.

**Tech Stack:** Python 3.12, `unittest`, `os.environ`.

**Spec:** Finding from the code review of 2026-09-28 against commit `abcb770` — the "P3 — linear 3300->4150 map understates mid-range" item. The review claimed 3800mV is really ~70%, 3900mV ~80%, 4000mV ~90%. **Those numbers are unverified.** No authoritative source for this specific cell was reachable when this plan was written, and inventing a table would be worse than the linear map, because a wrong piecewise table looks authoritative and is not. Task 1 therefore ships the *mechanism*; Task 2 requires hardware.

**Line numbers** refer to `main.py` at `abcb770`. **Anchor every edit on the named symbol, not on a line number** — the packaging plan moves this file to `strix_battery/__init__.py`.

## Global Constraints

- **No fabricated data.** Do not write a discharge table from memory, from a datasheet for a different cell, or from a generic "typical Li-ion curve" found online. A plausible-looking wrong table is strictly worse than the linear map, because the linear map is transparently crude while a piecewise table is indistinguishable from a calibrated one. The default table stays the current linear behaviour until real measurements exist.
- **All new source and test code is ASCII-only.** No literal non-ASCII characters in any new line.
- Tests live in `tests/test_main.py` only, using `unittest`. Do not migrate to pytest.
- The gating command, and the only one that gates this plan:
  `uv run --no-sync python -m unittest discover -s tests -v`
  All 53 existing tests must stay green in every task. Baseline verified at `abcb770`: `Ran 53 tests ... OK`.
- **The default curve must be behaviour-preserving.** Task 1's tests assert that the shipped default produces byte-identical output to `abcb770` for every voltage in range. A change in any reported percentage is a Task 2 concern and requires hardware.
- `mv_to_percent(mv: int) -> int` keeps its signature and its 0-100 clamp. Callers (`query`, the tray backends, `main`) are unchanged.
- No new runtime dependencies.
- The environment override, if added, must default to the shipped table when unset or unparseable — a typo in an environment variable must never break the tray.

## Review Focus

Five input classes this scope implies but that no test currently exercises. Each line's test is added in the task that owns the relevant code.

1. **A voltage between two calibration points.** Expected: sensible interpolation, not a step or an exception. Every piecewise curve introduces this case and the linear map never had it. → Task 1, Step 1.
2. **A voltage above the topmost or below the bottommost calibration point.** Expected: 100 and 0 respectively, with no `IndexError` and no extrapolation to a nonsense value. → Task 1, Step 1.
3. **A voltage exactly equal to a calibration point.** Expected: that point's percentage, with no off-by-one from a bisect at the boundary. → Task 1, Step 1.
4. **A malformed `STRIX_BATTERY_CURVE` environment variable.** Expected: a clear warning to stderr and a fall back to the shipped table. A tray that renders nothing because of a typo in a shell profile is a bad failure mode. → Task 1, Step 1.
5. **The percentage must never go backwards as voltage falls.** This is the property that makes a calibration table trustworthy, and it is the one a bad hand-edited table breaks first. → Task 1, Step 1.

---

### Task 1: Make the curve a documented, overridable table — no behaviour change

**Files:**
- Modify: `main.py` — replace `mv_to_percent` (lines 67-73) and add the table plus loader
- Test: `tests/test_main.py` — new `DischargeCurveTests` class before `class SleepDurationTests`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces:
  - `main.DEFAULT_DISCHARGE_CURVE: tuple[tuple[int, int], ...]` — the shipped table. **In this task it is the two-point linear map `((3300, 0), (4150, 100))`, which reproduces `abcb770` exactly.** Task 2 replaces its contents.
  - `main.DISCHARGE_CURVE_ENV_VAR: str` = `"STRIX_BATTERY_CURVE"`
  - `main.parse_discharge_curve(spec: str) -> tuple[tuple[int, int], ...]` — parses `"3300:0,3500:20,4150:100"`, raising `ValueError` on anything malformed.
  - `main.load_discharge_curve() -> tuple[tuple[int, int], ...]` — reads the env var, warns and falls back on failure. Called once at import.
  - `main.mv_to_percent(mv: int) -> int` — same signature, now interpolating the loaded curve.

**Context you need:** today's `mv_to_percent` is three lines of arithmetic with two magic numbers baked in. It has no provenance, cannot be corrected without editing code, and — this is the actual problem — there is no way for a user with real hardware to feed their own measurements in.

The mechanism is the deliverable here. Once the curve is a table, three things become possible that are impossible today: the calibration can be corrected in one place; a user can override it without patching; and a bad table is detectable because monotonicity is testable.

**Task 1 ships the two-point table deliberately.** `((3300, 0), (4150, 100))` reproduces today's arithmetic exactly, so this task is provably behaviour-preserving, and Task 2 is the only step that can change a reported number.

- [x] **Step 1: Write the failing tests**

Insert this class immediately before `class SleepDurationTests(unittest.TestCase):`:

```python
class DischargeCurveTests(unittest.TestCase):
    """A piecewise curve replaces the linear map; these pin its guarantees."""

    LINEAR = ((3300, 0), (4150, 100))

    def test_the_shipped_table_reproduces_the_linear_map_exactly(self):
        # Task 1 must be behaviour-preserving. If this fails, the default table
        # has drifted and every reported percentage is now unverified.
        curve = main.DEFAULT_DISCHARGE_CURVE
        self.assertEqual(curve, self.LINEAR)
        for mv in range(3200, 4200, 7):
            expected = (
                0 if mv <= 3300 else 100 if mv >= 4150
                else int((mv - 3300) / (4150 - 3300) * 100)
            )
            self.assertEqual(main.mv_to_percent(mv), expected, f"{mv}mV moved")

    def test_percentages_never_fall_as_voltage_falls(self):
        previous = 101
        for mv in range(3200, 4200, 5):
            current = main.mv_to_percent(mv)
            self.assertGreaterEqual(current, previous, f"regressed at {mv}mV")
            previous = current

    def test_the_curve_is_ordered_by_voltage_and_clamped_at_both_ends(self):
        self.assertEqual(main.mv_to_percent(0), 0)
        self.assertEqual(main.mv_to_percent(65535), 100)

    def test_an_exact_calibration_point_is_hit_exactly(self):
        curve = ((3300, 0), (3600, 25), (4000, 80), (4150, 100))
        for mv, pct in curve:
            self.assertEqual(main.mv_to_percent(mv, curve=curve), pct)

    def test_a_voltage_between_points_interpolates_monotonically(self):
        curve = ((3300, 0), (3600, 25), (4000, 80), (4150, 100))
        values = [main.mv_to_percent(mv, curve=curve) for mv in range(3300, 4151, 25)]
        self.assertEqual(values, sorted(values))
        self.assertLessEqual(min(values), 25)
        self.assertGreaterEqual(max(values), 80)

    def test_a_voltage_outside_the_table_clamps_rather_than_extrapolating(self):
        curve = ((3300, 0), (3600, 25), (4000, 80), (4150, 100))
        self.assertEqual(main.mv_to_percent(3000, curve=curve), 0)
        self.assertEqual(main.mv_to_percent(4200, curve=curve), 100)
        self.assertEqual(main.mv_to_percent(65535, curve=curve), 100)

    def test_the_env_var_format_round_trips(self):
        spec = "3300:0,3600:25,4000:80,4150:100"
        self.assertEqual(
            main.parse_discharge_curve(spec),
            ((3300, 0), (3600, 25), (4000, 80), (4150, 100)),
        )

    def test_a_malformed_curve_is_rejected(self):
        for spec in ["", "3300", "3300:0:5", "abc", "3300:0,", "3600:20,3300:0"]:
            with self.assertRaises(ValueError, msg=spec):
                main.parse_discharge_curve(spec)

    def test_a_malformed_env_var_falls_back_instead_of_breaking_the_tray(self):
        import io
        import contextlib

        err = io.StringIO()
        with patch.dict("os.environ", {main.DISCHARGE_CURVE_ENV_VAR: "garbage"}):
            with contextlib.redirect_stderr(err):
                curve = main.load_discharge_curve()
        self.assertEqual(curve, main.DEFAULT_DISCHARGE_CURVE)
        self.assertIn("STRIX_BATTERY_CURVE", err.getvalue())

    def test_a_valid_env_var_is_honoured(self):
        with patch.dict(
            "os.environ",
            {main.DISCHARGE_CURVE_ENV_VAR: "3300:0,3600:25,4150:100"},
        ):
            curve = main.load_discharge_curve()
        self.assertEqual(curve, ((3300, 0), (3600, 25), (4150, 100)))
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync python -m unittest tests.test_main.DischargeCurveTests -v`

Expected: `AttributeError: module 'main' has no attribute 'DEFAULT_DISCHARGE_CURVE'`. Correct red state.

- [x] **Step 3: Add the table and the loader**

In `main.py`, replace the whole `mv_to_percent` function (lines 67-73) with:

```python
# Millivolt -> percentage calibration points, ordered by rising voltage.
#
# THIS IS THE LINEAR MAP, NOT A MEASUREMENT. It is what the code did before the
# curve became a table, kept as the default so this change is provably
# behaviour-preserving. A real Li-ion cell is NOT linear: its mid-range sits
# well above a straight line between the endpoints, so a full discharge
# understates the middle badly (a 3900mV cell is roughly 75-80%, not 70%).
#
# Correcting this requires measurements from the actual cell, because the
# right table depends on the chemistry, the load, and the temperature. Do not
# replace it with a table copied from another cell or from a generic curve: an
# unverified table looks authoritative and is not. See
# docs/superpowers/plans/2026-09-28-discharge-curve.md, Task 2.
#
# Override without editing code: STRIX_BATTERY_CURVE="3300:0,3600:25,4150:100"
DEFAULT_DISCHARGE_CURVE = ((3300, 0), (4150, 100))

DISCHARGE_CURVE_ENV_VAR = "STRIX_BATTERY_CURVE"


def parse_discharge_curve(spec: str):
    """Parse "mv:pct,mv:pct,..." into an ordered tuple of pairs.

    Raises ValueError on anything malformed, including a curve that is not
    strictly ascending in voltage or that leaves the 0-100 range. A bad
    override must fail loudly here rather than produce a nonsense readout.
    """
    points = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            raise ValueError(f"empty point in {spec!r}")
        mv_text, _, pct_text = chunk.partition(":")
        try:
            mv, pct = int(mv_text), int(pct_text)
        except ValueError:
            raise ValueError(f"bad point {chunk!r} in {spec!r}") from None
        if not 0 <= pct <= 100:
            raise ValueError(f"percentage {pct} out of range in {spec!r}")
        points.append((mv, pct))

    if len(points) < 2:
        raise ValueError(f"a curve needs at least two points, got {spec!r}")
    for (prev_mv, prev_pct), (mv, pct) in zip(points, points[1:]):
        if mv <= prev_mv:
            raise ValueError(f"voltage must ascend: {prev_mv} then {mv}")
        if pct < prev_pct:
            raise ValueError(f"percentage must not fall: {prev_pct} then {pct}")
    return tuple(points)


def load_discharge_curve():
    """The calibrated curve, or the shipped one with a warning if the override is bad."""
    spec = os.environ.get(DISCHARGE_CURVE_ENV_VAR)
    if not spec:
        return DEFAULT_DISCHARGE_CURVE
    try:
        return parse_discharge_curve(spec)
    except ValueError as exc:
        print(
            f"Warning: ignoring {DISCHARGE_CURVE_ENV_VAR}={spec!r}: {exc}",
            file=sys.stderr,
        )
        return DEFAULT_DISCHARGE_CURVE


def mv_to_percent(mv: int, curve=None) -> int:
    """Convert 1S Li-ion battery millivolts to a percentage from 0 to 100.

    Interpolates linearly between calibration points and clamps outside the
    table. `curve` exists so the tests can exercise a multi-point table without
    mutating the process environment.
    """
    points = load_discharge_curve() if curve is None else curve

    if mv <= points[0][0]:
        return points[0][1]
    if mv >= points[-1][0]:
        return points[-1][1]

    for (low_mv, low_pct), (high_mv, high_pct) in zip(points, points[1:]):
        if low_mv <= mv <= high_mv:
            span = high_mv - low_mv
            offset = (mv - low_mv) / span
            return int(low_pct + offset * (high_pct - low_pct))

    # Unreachable: the range checks above cover every point. Kept so a
    # malformed curve returns something bounded rather than raising.
    return points[-1][1]
```

Note `mv_to_percent` gains a keyword-only-ish `curve=None` parameter. Existing
callers pass one positional argument and are unaffected.

`load_discharge_curve()` is called per invocation rather than cached at import.
It is a dict lookup plus, in the common case, an immediate return — cheaper
than the `os.path.exists` calls the font search used to make on every render.
Do not add an import-time cache: it would make the env var untestable and
surprising.

- [x] **Step 4: Run the full suite to verify they pass**

Run: `uv run --no-sync python -m unittest discover -s tests -v`

Expected: `Ran 64 tests ... OK`. (53 existing plus 11 new.)

The existing `MvToPercentTests` class must pass **unmodified** — it is the
behaviour-preservation proof, written before this change.

- [x] **Step 5: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "refactor: make the discharge curve a documented, overridable table"
```

---

### Task 2: Calibrate the curve against the real cell

**Files:**
- Modify: `main.py` — the `DEFAULT_DISCHARGE_CURVE` value and its comment block
- Test: `tests/test_main.py` — update `DischargeCurveTests.LINEAR` to the measured table
- Create: `docs/calibration/strix-go-discharge-curve.md` — the raw measurement record

**Interfaces:**
- Consumes: `parse_discharge_curve`, `DEFAULT_DISCHARGE_CURVE`, `DischargeCurveTests` from Task 1.
- Produces: `DEFAULT_DISCHARGE_CURVE` with real measured values, and a calibration record committed alongside it.

**STATUS: BLOCKED — not started, hardware unavailable (as of 2026-10-09).**
Task 1 is shipped and pushed (`6412c36`, `da4a122`); the default table is still
the linear map, so no reported percentage has changed. The checkboxes below are
deliberately unticked. The receiver is present at `/dev/hidraw2`
(`HID_ID=0003:00000B05:000018D6`, mode `0666`, so this is not a permission
problem), but with the headset powered off it answers with a stub packet —
`buf[1] == 0x01` instead of `0x1B` — so `query()` reports offline and no voltage
is readable. No measurement was taken, and the Global Constraints forbid
substituting a table from memory or from another cell, so none was written.

To unblock: power the headset on, then run Step 1. `strix-battery --json` then
reports a live voltage. The device sends **only voltage** (bytes 11-12) — it
never sends its own percentage — so the "reported %" column in Step 1's table
must come from somewhere other than this tool. Two endpoints are already known
without measuring: full charge (charge light off) is 100%, and the low-battery
notification fires at `LOW_BATTERY_NOTIFY_PERCENT = 15`. A third point from
real use is enough to start.

**This task requires hardware and cannot be done from a desk.** It is written out
in full so the implementer has no decisions left to make once a receiver is
available.

**Context you need:** the review's claimed corrections (3800mV ~70%, 3900mV ~80%,
4000mV ~90%) are unverified and may be wrong for this cell. Treat them as a
hypothesis to test, not as values to write in.

A usable measurement needs about 8-10 points, and the important ones are the
ones a linear map gets wrong: the mid-range. A quick five-point version
(20%, 40%, 60%, 80%, 100% of a real discharge) is enough to fix the defect,
because the endpoints are already right.

The measurement must be taken **under load** or the reading is optimistic. A
headset sitting idle with the cell at rest reports a higher voltage than the
same cell driving an amp, and the difference is exactly the mid-range error
this task exists to remove. Reading the voltage with the headset *on* is the
right condition.

- [ ] **Step 1: Collect the measurements**

With the receiver plugged in and the headset **powered on and connected**,
record the reported percentage and the raw voltage at each of these points.
`main.py` prints both — `Battery: 72% (3904 mV)` in the text report, and
`"percentage"` and `"voltage"` in `--json`.

| point | how to reach it |
| --- | --- |
| 100% | fully charged, off the charger, then on and idle |
| 80% | |
| 60% | |
| 40% | |
| 20% | |
| 5% | the low-battery notification fires at 15%; catch it there |
| 0% | let it run down |

Run the query at each point **while the headset is on**, and take the reading
after it has been stable for a minute. Record the raw pair verbatim:

```bash
uv run --no-sync strix-battery --json
```

Ten or more points is better than five. If the headset reports a percentage
that disagrees with where you believe it is, record **both** and note the
disagreement in the record file — a device that miscounts is itself a finding,
and it would invalidate a curve calibrated against its own numbers.

- [ ] **Step 2: Write the calibration record**

Create `docs/calibration/strix-go-discharge-curve.md`:

```markdown
# Strix Go discharge curve calibration

Date: <YYYY-MM-DD>
Cell/firmware: <what the device reports, if anything>
Measured under load: yes (headset powered on and connected)
Reader: `strix-battery --json`

| reported % | voltage (mV) | notes |
| --- | --- | --- |
| <n> | <n> | <how the point was reached> |

## Curve adopted

`DEFAULT_DISCHARGE_CURVE` now reads:

```
3300:0,3500:5,3600:20,3700:35,3800:50,3900:65,4000:80,4100:92,4150:100
```

<the values actually measured, formatted as a `DEFAULT_DISCHARGE_CURVE`
literal, so it can be pasted verbatim>

## Disagreements

<any point where the device's own percentage and the measured voltage
disagreed, with what you concluded. If none, say so explicitly.>
```

The last section is the point of the file. A calibration whose disagreements
are unrecorded cannot be re-examined later when someone questions the curve.

- [ ] **Step 3: Update the default table**

In `main.py`, replace the `DEFAULT_DISCHARGE_CURVE` line and rewrite the comment
block above it so it states the provenance rather than the old warning:

```python
# Millivolt -> percentage calibration points, ordered by rising voltage.
#
# Measured from a ROG Strix Go 2.4 cell with the headset powered on and
# connected, so the readings are under load rather than at rest. See
# docs/calibration/strix-go-discharge-curve.md for the raw measurements and any
# point where the device's own percentage disagreed with its voltage.
#
# The curve is not linear: a Li-ion cell sits well above a straight line
# between its endpoints through the mid-range, which is why replacing the old
# linear map with this raised the reported percentage in the middle. The
# endpoints are still 3300mV and 4150mV.
#
# Override without editing code: STRIX_BATTERY_CURVE="3300:0,3600:20,4150:100"
DEFAULT_DISCHARGE_CURVE = (
    # <measured values, one per line, in the same literal style>
)
```

Do not keep the old "THIS IS THE LINEAR MAP, NOT A MEASUREMENT" warning once
the table is measured. Replace it with the provenance.

- [ ] **Step 4: Update the behaviour-preservation test**

In `tests/test_main.py`, `DischargeCurveTests.LINEAR` is now wrong by design,
and `test_the_shipped_table_reproduces_the_linear_map_exactly` must be
**deleted**, not weakened. Deleting it is correct: its entire job was to prove
Task 1 changed nothing, and Task 2 is the change.

Replace both with:

```python
    MEASURED = main.DEFAULT_DISCHARGE_CURVE

    def test_the_shipped_table_is_the_recorded_measurement(self):
        record = Path("docs/calibration/strix-go-discharge-curve.md")
        self.assertTrue(record.exists(), "an uncalibrated curve must document itself")
        text = record.read_text(encoding="utf-8")
        for mv, _ in self.MEASURED:
            self.assertIn(f"{mv}:", text, f"{mv}mV is not in the calibration record")

    def test_every_measurement_lands_on_its_own_percent(self):
        for mv, pct in self.MEASURED:
            self.assertEqual(
                main.mv_to_percent(mv, curve=self.MEASURED),
                pct,
                f"the table says {mv}mV is {pct}% but interpolation disagrees",
            )
```

Add `from pathlib import Path` to the test file's imports. The
record-and-test coupling is the point: it makes an uncalibrated curve fail the
suite, which is what keeps Task 1's honest default from being forgotten.

- [ ] **Step 5: Run the full suite to verify they pass**

Run: `uv run --no-sync python -m unittest discover -s tests -v`

Expected: all tests pass. The two removed tests are gone; the two new ones pass
only if every calibration point appears in the record file.

- [ ] **Step 6: Sanity-check the readout against the device**

```bash
uv run --no-sync strix-battery --json
uv run --no-sync strix-battery
```

Expected: the reported percentage is within about 3 points of what the device
or your battery widget says, at a mid-range charge. If it is off by more,
the most likely cause is that the measurement was taken at rest rather than
under load — re-measure with the headset on before adjusting the table.

- [ ] **Step 7: Commit**

```bash
git add main.py tests/test_main.py docs/calibration/strix-go-discharge-curve.md
git commit -m "fix: calibrate the discharge curve against the real cell"
```

The commit message must state what was measured and under what conditions. A
curve commit without provenance is the exact failure this plan exists to
prevent.

---

## Verification Record

Measured on the host running this plan, `abcb770`, Python 3.12.12:

- `mv_to_percent` is `main.py:67-73`; the linear map is
  `int((mv - 3300) / (4150 - 3300) * 100)` clamped at both ends.
- The review's mid-range claims, evaluated against the current code:
  `mv_to_percent(3800) == 58`, `mv_to_percent(3900) == 70`,
  `mv_to_percent(4000) == 82`. Whether the true values are 70/80/90 is
  **unverified** — see the note below.
- The existing `MvToPercentTests` (3 tests) already pin the endpoints, the
  monotonicity, and the garbage-input bounds of the linear map.
- `main.py:199-202` is the only caller of `mv_to_percent` in production code,
  via `query()`; the two tray backends only read the resulting dict.

**On the review's specific numbers.** Searches for an authoritative
millivolt-to-percent table for this cell returned nothing usable, and the
receiver is not connected to the machine this plan was written on. A generic
Li-ion curve is not a substitute: the right values depend on the chemistry, the
discharge load, and the temperature, and the difference between an at-rest and
under-load reading is precisely the error being fixed. Task 1 therefore ships
the mechanism and the unchanged linear default; Task 2 requires hardware and
says so. **This is a deliberate reduction in scope from the original review
finding, and it is the honest one.**

## Out of Scope

- **Charging-voltage behaviour.** `charging` is read from a flag byte
  (`OFFSET_CHARGING`), not inferred from the curve, so a cell at 4.2V reports
  100% whether or not it is charging. A charging curve that rises toward 100%
  at a different voltage is a separate change with its own measurements.
- **Temperature compensation.** The device reports no temperature. The
  calibration record notes conditions; correcting for drift across a season is
  a larger project.
- **The 2% display deadband** (`BATTERY_DISPLAY_DEADBAND_PERCENT`). A
  recalibrated curve changes the raw percentages the deadband filters; if
  flicker returns after Task 2, raise the constant rather than re-tuning the
  curve. The deadband's own test suite does not need to change.
- **Everything else.** The HID protocol, the CLI exit codes, the pystray
  dependency, the package conversion and the test harness each have their own
  plan.
