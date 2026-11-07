"""Acoustic maths shared by the hub, the history store and the lab tests."""

import math
from datetime import datetime, time as dtime
from typing import Iterable, List, Optional, Sequence, Tuple

# EU Environmental Noise Directive 2002/49/EC default periods (local time).
DAY_START_H = 7
EVENING_START_H = 19
NIGHT_START_H = 23

# WHO Environmental Noise Guidelines for the European Region (2018), road traffic.
WHO_ROAD_LDEN_DB = 53.0
WHO_ROAD_LNIGHT_DB = 45.0


def db_to_energy(db: float) -> float:
    return 10.0 ** (db / 10.0)


def energy_to_db(energy: float) -> Optional[float]:
    if energy is None or energy <= 0:
        return None
    return 10.0 * math.log10(energy)


def leq(values: Iterable[float]) -> Optional[float]:
    """Energetic (logarithmic) mean of dB values."""
    total = 0.0
    count = 0
    for v in values:
        total += db_to_energy(v)
        count += 1
    if count == 0:
        return None
    return energy_to_db(total / count)


def weighted_leq(pairs: Iterable[Tuple[float, float]]) -> Optional[float]:
    """Energetic mean of (db, weight) pairs."""
    total = 0.0
    weight = 0.0
    for db, w in pairs:
        if db is None or not w:
            continue
        total += db_to_energy(db) * w
        weight += w
    if weight <= 0:
        return None
    return energy_to_db(total / weight)


def percentile(sorted_values: Sequence[float], pct: float) -> Optional[float]:
    """Linear-interpolated percentile of an already sorted sequence (pct in 0..100)."""
    n = len(sorted_values)
    if n == 0:
        return None
    if n == 1:
        return float(sorted_values[0])
    rank = (pct / 100.0) * (n - 1)
    lo = int(math.floor(rank))
    hi = min(lo + 1, n - 1)
    frac = rank - lo
    return float(sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac)


def exceedance_levels(values: Iterable[float]) -> dict:
    """
    Statistical levels: LN is the level exceeded N % of the time.
    L10 ~ "busy" peaks, L50 ~ typical, L90 ~ background.
    """
    ordered = sorted(values)
    return {
        "l10": percentile(ordered, 90.0),
        "l50": percentile(ordered, 50.0),
        "l90": percentile(ordered, 10.0),
    }


def lden(l_day: Optional[float], l_evening: Optional[float], l_night: Optional[float]) -> Optional[float]:
    """Day-evening-night level (12 h day, 4 h evening +5 dB, 8 h night +10 dB)."""
    if l_day is None or l_evening is None or l_night is None:
        return None
    total = (
        12.0 * db_to_energy(l_day)
        + 4.0 * db_to_energy(l_evening + 5.0)
        + 8.0 * db_to_energy(l_night + 10.0)
    ) / 24.0
    return energy_to_db(total)


def period_of_hour(hour: int) -> str:
    if DAY_START_H <= hour < EVENING_START_H:
        return "day"
    if EVENING_START_H <= hour < NIGHT_START_H:
        return "evening"
    return "night"


PERIOD_SECONDS = {
    "day": (EVENING_START_H - DAY_START_H) * 3600,
    "evening": (NIGHT_START_H - EVENING_START_H) * 3600,
    "night": (24 - NIGHT_START_H + DAY_START_H) * 3600,
}


def parse_hhmm(value: str) -> dtime:
    hh, mm = str(value).strip().split(":", 1)
    h, m = int(hh), int(mm)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"invalid time {value!r}")
    return dtime(h, m)


def parse_time_range(spec: str) -> Tuple[dtime, dtime]:
    start, end = str(spec).replace(" ", "").split("-", 1)
    return parse_hhmm(start), parse_hhmm(end)


def in_time_range(moment: datetime, spec: str) -> bool:
    """True when the local time of `moment` falls inside 'HH:MM-HH:MM' (may wrap midnight)."""
    try:
        start, end = parse_time_range(spec)
    except Exception:
        return False
    now = moment.time()
    if start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end


def round_or_none(value: Optional[float], digits: int = 1) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), digits)


def median(values: List[float]) -> Optional[float]:
    return percentile(sorted(values), 50.0)
