# Discharge Curve and Level Mapping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the linear 3300-4150 mV percentage map with a documented, overridable table, and adopt the verified Armoury Crate quarter-step mapping for the headset's displayed percentage.

**Architecture:** Two tasks. Task 1 makes the curve *configurable and honest about its own provenance* — a named table, a documented source comment, an environment override, and a calibration helper — without changing any behaviour, so the current linear map is provably preserved by tests. Task 2 (previously blocked on hardware) is now unblocked by the protocol facts in HaloBattery PR #296: the headset reports millivolts, and Armoury Crate turns them into quarter steps. The displayed percentage therefore uses a verified step map (`headset_level_from_voltage`), while `mv_to_percent` keeps its linear default as an uncalibrated helper for tooling and tests.

**Tech Stack:** Python 3.12, `unittest`, `os.environ`.

**Spec:** Finding from the code review of 2026-09-28 against commit `abcb770` — the "P3 — linear 3300->4150 map understates mid-range" item. The review claimed 3800 mV is really ~70%, 3900 mV ~80%, 4000 mV ~90%. **Those numbers were unverified.** HaloBattery PR #296 (issue #190) later established the real mapping from hardware captures: 100 at MSB `0x10` or >= 4100 mV, 75 at MSB `0x0F` or >= 3800 mV, 50 at >= 3700 mV, 25 at MSB `0x0E` or >= 3500 mV, and 10 below. Byte 13 is a constant `0x40`, not a level. See `docs/protocols.md` (ASUS ROG Strix Go 2.4).

## Global Constraints

- **No fabricated data.** The quarter-step table comes only from HaloBattery PR #296 / G-Helper #6067, which is hardware-tested. Do not invent intermediate percentages.
- **All new source and test code is ASCII-only.**
- Tests live in `tests/test_main.py` only, using `unittest`.
- The gating command: `uv run python -m unittest discover -s tests`. All existing tests must stay green in every task.
- **The default curve must be behaviour-preserving.** Task 1's tests assert that the shipped default produces byte-identical output to `abcb770` for every voltage in range. Changing the reported percentage is a Task 2 concern.
- `mv_to_percent(mv: int) -> int` keeps its signature and its 0-100 clamp. Callers are unchanged.
- No new runtime dependencies.
- The environment override, if added, must default to the shipped table when unset or unparseable.

## Review Focus

Five input classes this scope implies but that no test currently exercises:

1. **A voltage between two calibration points.** Expected: sensible interpolation, not a step or an exception. → Task 1, Step 1.
2. **A voltage above the topmost or below the bottommost calibration point.** Expected: 100 and 0 respectively, with no `IndexError` and no extrapolation. → Task 1, Step 1.
3. **A voltage exactly equal to a calibration point.** Expected: that point's percentage, with no off-by-one. → Task 1, Step 1.
4. **A malformed `STRIX_BATTERY_CURVE` environment variable.** Expected: a clear warning to stderr and a fall back to the shipped table. → Task 1, Step 1.
5. **The percentage must never go backwards as voltage falls.** → Task 1, Step 1.

---

### Task 1: Make the curve a documented, overridable table — no behaviour change

**Files:**
- Modify: `strix_battery/battery.py` — replace `mv_to_percent` with a table plus loader
- Test: `tests/test_main.py` — `DischargeCurveTests` class

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces:
  - `DEFAULT_DISCHARGE_CURVE: tuple[tuple[int, int], ...]` — the shipped table. In this task it is the two-point linear map `((3300, 0), (4150, 100))`, which reproduces `abcb770` exactly.
  - `DISCHARGE_CURVE_ENV_VAR: str` = `"STRIX_BATTERY_CURVE"`
  - `parse_discharge_curve(spec) -> tuple[tuple[int, int], ...]`
  - `load_discharge_curve() -> tuple[tuple[int, int], ...]`
  - `mv_to_percent(mv: int, curve=None) -> int`

**Status: SHIPPED.** Implemented in `strix_battery/battery.py`; tests in `tests/test_main.py`.

- [x] **Step 1:** Write the failing tests (done).
- [x] **Step 2:** Run the tests to verify they fail (done).
- [x] **Step 3:** Add the table and the loader (done).
- [x] **Step 4:** Run the full suite to verify they pass (done).
- [x] **Step 5:** Commit (done).

---

### Task 2: Adopt the verified Armoury Crate quarter-step mapping

**Files:**
- Modify: `strix_battery/device.py` — add `headset_level_from_voltage` and use it in `decode_status`
- Test: `tests/test_main.py` — `HeadsetLevelTests`

**Interfaces:**
- Consumes: `HEADSET_LEVEL_STEPS`, `HEADSET_LEVEL_LOW` from `device.py`.
- Produces: `headset_level_from_voltage(mv: int) -> int`.

**Status: SHIPPED (2026-10-21).** The device reports millivolts only; Armoury Crate's quarter steps are the level. The implementation keeps `mv_to_percent` as the uncalibrated helper and adds a separate, verified step map used by `decode_status`, so the two concerns stay independent.

Mapping (from HaloBattery PR #296, G-Helper #6067):

| MSB | millivolt floor | level |
| --- | --- | --- |
| `0x10` | 4100 | 100 |
| `0x0F` | 3800 | 75 |
| — | 3700 | 50 |
| `0x0E` | 3500 | 25 |
| below | — | 10 |

Verified frames:

- `3743 mV` → 50%
- `3831 mV` → 75%
- `3897 mV` → 75% (Armoury held 50% while charging — damped panel)
- `4077 mV` → 75% live (Armoury held 100% after unplug — damped panel)

Byte 13 is a constant `0x40` and is never used as a level.

- [x] **Step 1:** Write the failing tests (done).
- [x] **Step 2:** Run the tests to verify they fail (done).
- [x] **Step 3:** Add `headset_level_from_voltage` and wire it into `decode_status` (done).
- [x] **Step 4:** Run the full suite to verify they pass (done).
- [x] **Step 5:** Commit (done, with version bump to 1.1.0).

## Verification Record

- `mv_to_percent` lives in `strix_battery/battery.py`; the linear map is `int((mv - 3300) / (4150 - 3300) * 100)` clamped at both ends.
- The device's displayed percentage no longer uses `mv_to_percent`; it uses `headset_level_from_voltage` from `strix_battery/device.py`.
- `strix_battery --json` reports `percentage` as an Armoury quarter step and `voltage` as the raw millivolts.
- Wired mode (`0x18D7`) forces `charging = True`.
- The receiver is queried with a 35 ms delay and up to 3 retries on an empty read.

## Out of Scope

- **Charging-voltage behaviour.** `charging` is read from a flag byte (`OFFSET_CHARGING`), not inferred from the curve.
- **Temperature compensation.** The device reports no temperature.
- **The 2% display deadband** (`BATTERY_DISPLAY_DEADBAND_PERCENT`). A recalibrated curve changes the raw percentages the deadband filters; if flicker returns, raise the constant rather than re-tuning the curve.
- **Collection selection by feature-report length.** Linux hidraw probing is hardware-specific; deferred.
- **Everything else.** The HID protocol, the CLI exit codes, the package conversion and the test harness each have their own plan.