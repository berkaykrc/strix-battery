import itertools
import os
import sys

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

# Specs already warned about, so a typo is reported once rather than on every
# poll. Both tray backends poll every 15s; without this the same line lands in
# the journal thousands of times a day and buries itself.
_WARNED_CURVES: set[str] = set()


def parse_discharge_curve(spec: str) -> tuple[tuple[int, int], ...]:
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
    for prev, point in itertools.pairwise(points):
        prev_mv, prev_pct = prev
        mv, pct = point
        if mv <= prev_mv:
            raise ValueError(f"voltage must ascend: {prev_mv} then {mv}")
        if pct < prev_pct:
            raise ValueError(f"percentage must not fall: {prev_pct} then {pct}")
    return tuple(points)


def load_discharge_curve() -> tuple[tuple[int, int], ...]:
    """The calibrated curve, or the shipped one with a warning if the override is bad."""
    spec = os.environ.get(DISCHARGE_CURVE_ENV_VAR)
    if not spec:
        return DEFAULT_DISCHARGE_CURVE
    try:
        return parse_discharge_curve(spec)
    except ValueError as exc:
        if spec not in _WARNED_CURVES:
            _WARNED_CURVES.add(spec)
            print(
                f"Warning: ignoring {DISCHARGE_CURVE_ENV_VAR}={spec!r}: {exc}",
                file=sys.stderr,
            )
        return DEFAULT_DISCHARGE_CURVE


def mv_to_percent(mv: int, curve: tuple[tuple[int, int], ...] | None = None) -> int:
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

    for low, high in itertools.pairwise(points):
        low_mv, low_pct = low
        high_mv, high_pct = high
        if low_mv <= mv <= high_mv:
            span = high_mv - low_mv
            offset = (mv - low_mv) / span
            return int(low_pct + offset * (high_pct - low_pct))

    # Unreachable: the range checks above cover every point. Kept so a
    # malformed curve returns something bounded rather than raising.
    return points[-1][1]
