"""
Simulated sound meter: realistic-ish urban soundscape for demos and tests.

- day/night background with a slow random walk
- passing vehicles, impulsive events (door slam, horn), occasional long episodes (music, works)
- Fast time weighting (125 ms) applied in the energy domain like a real meter
- "hears" the lab test programs, so the whole lab can be tried without hardware
"""

import math
import random
import time
from datetime import datetime
from typing import Callable, List, Optional

from .acoustics import db_to_energy, energy_to_db, in_time_range
from .history import HistoryStore
from .hub import EpisodeDetector, EventDetector, MinuteBucket


def _base_level(hour: float) -> float:
    # quiet night, morning rush, busy day, calmer evening
    if hour < 5:
        return 36.0
    if hour < 7:
        return 36.0 + (hour - 5) * 6.0
    if hour < 9:
        return 50.0
    if hour < 18:
        return 47.0
    if hour < 20:
        return 49.0
    if hour < 23:
        return 44.0 - (hour - 20) * 2.0
    return 37.0


class _Source:
    __slots__ = ("start", "end", "level", "shape")

    def __init__(self, start, duration, level, shape):
        self.start, self.end, self.level, self.shape = start, start + duration, level, shape

    def level_at(self, t: float) -> Optional[float]:
        if not self.start <= t < self.end:
            return None
        x = (t - self.start) / (self.end - self.start)
        if self.shape == "pass":  # vehicle passing by: bell curve
            return self.level - 18.0 * (2 * x - 1) ** 2
        if self.shape == "impulse":
            return self.level - 6.0 * x
        return self.level  # flat (episodes)


class Soundscape:
    def __init__(self, seed: int = 1361, lively: float = 1.0, trend_db_per_day: float = 0.0, t_origin: float = None):
        self.rng = random.Random(seed)
        self.lively = lively
        self.trend = trend_db_per_day
        self.t_origin = t_origin if t_origin is not None else time.time()
        self.sources: List[_Source] = []
        self.walk = 0.0
        self._last_t: Optional[float] = None

    def _spawn(self, t: float, dt: float, hour: float):
        night = hour < 6 or hour >= 23
        rates = {  # events per hour
            "pass": (8 if night else 50) * self.lively,
            "impulse": (0.2 if night else 1.2) * self.lively,
            "episode": (0.05 if night else 0.15) * self.lively,
        }
        r = self.rng
        if r.random() < rates["pass"] * dt / 3600:
            self.sources.append(_Source(t, r.uniform(5, 10), _base_level(hour) + r.uniform(12, 24), "pass"))
        if r.random() < rates["impulse"] * dt / 3600:
            self.sources.append(_Source(t, r.uniform(0.3, 1.0), r.uniform(74, 92), "impulse"))
        if r.random() < rates["episode"] * dt / 3600:
            self.sources.append(_Source(t, r.uniform(240, 1500), r.uniform(62, 72), "flat"))

    def level(self, t: float) -> float:
        dt = 0.1 if self._last_t is None else max(0.0, min(5.0, t - self._last_t))
        self._last_t = t
        hour = datetime.fromtimestamp(t)
        hour = hour.hour + hour.minute / 60.0
        self._spawn(t, dt, hour)
        self.walk += self.rng.gauss(0, 0.05) * math.sqrt(max(dt, 1e-3)) - self.walk * 0.002 * dt
        self.walk = max(-4.0, min(4.0, self.walk))
        days = (t - self.t_origin) / 86400.0
        energy = db_to_energy(_base_level(hour) + self.walk + self.trend * days + self.rng.gauss(0, 1.2))
        alive = []
        for s in self.sources:
            if t < s.end:
                alive.append(s)
                lv = s.level_at(t)
                if lv is not None:
                    energy += db_to_energy(lv)
        self.sources = alive
        return energy_to_db(energy)


class SimulatedMeter:
    """Drop-in replacement for a USB meter: `read()` returns the current reading."""

    TAU_FAST = 0.125

    def __init__(self, seed: int = 1361, lively: float = 3.0,
                 playback: Optional[Callable[[float], Optional[float]]] = None,
                 clock: Callable[[], float] = time.time):
        self.scape = Soundscape(seed=seed, lively=lively)
        self.playback = playback
        self.clock = clock
        self._energy: Optional[float] = None
        self._last: Optional[float] = None

    def read(self, t: Optional[float] = None) -> float:
        t = self.clock() if t is None else t
        energy = db_to_energy(self.scape.level(t))
        if self.playback:
            extra = self.playback(t)
            if extra is not None:
                energy += db_to_energy(extra)
        if self._energy is None:
            self._energy = energy
        else:
            dt = max(1e-3, t - (self._last or t))
            alpha = 1.0 - math.exp(-dt / self.TAU_FAST)
            self._energy += (energy - self._energy) * alpha
        self._last = t
        value = energy_to_db(self._energy)
        return round(max(30.0, min(130.0, value)), 1)

    def __str__(self) -> str:
        return "Simulated sound meter (demo)"


def seed_history(store: HistoryStore, days: int = 14, now: Optional[float] = None, seed: int = 7,
                 event_trigger_db: float = 80.0, episode_db: float = 65.0, episode_night_db: float = 55.0,
                 quiet_hours: str = "22:00-07:00", progress: Optional[Callable[[float], None]] = None) -> int:
    """Fill the history with `days` of synthetic data (1 reading/s) so charts have something to show."""
    now = now or time.time()
    start = int(now - days * 86400)
    start -= start % 60
    end = int(now) - int(now) % 60
    scape = Soundscape(seed=seed, lively=1.0, trend_db_per_day=-0.12, t_origin=start)
    detector = EventDetector()
    episodes = EpisodeDetector()
    rows = []
    t = start
    threshold = episode_db
    minute = MinuteBucket(start)
    count = 0
    while t < end:
        db = round(scape.level(float(t)), 1)
        sec = {"t": t, "laeq": db, "lmax": db, "lmin": db, "n": 1}
        if t - t % 60 != minute.t:
            row = minute.finalize()
            if row:
                rows.append(row)
            minute = MinuteBucket(t - t % 60)
            if len(rows) >= 2000:
                store.add_minutes(rows)
                count += len(rows)
                rows = []
                if progress:
                    progress((t - start) / float(end - start))
        minute.add(sec)
        res = detector.update(sec, event_trigger_db, 3.0, 2)
        if res and res[0] == "end":
            ev = res[1]
            store.add_event("event", ev["start"], ev["end"], ev["peak"], ev["laeq"], curve=ev["curve"],
                            meta={"trigger": event_trigger_db, "simulated": True})
        if t % 60 == 0:
            night = in_time_range(datetime.fromtimestamp(t), quiet_hours)
            threshold = episode_night_db if night else episode_db
        res = episodes.update(sec, threshold, 3)
        if res and res[0] == "end":
            ep = res[1]
            store.add_event("episode", ep["start"], ep["end"], ep["peak"], ep["laeq"],
                            meta={"events": ep["events"], "threshold": ep["threshold"], "simulated": True})
        t += 1
    row = minute.finalize()
    if row:
        rows.append(row)
    store.add_minutes(rows)
    return count + len(rows)
