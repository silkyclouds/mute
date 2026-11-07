"""
LocalHub: the heart of Mute Lab.

Fed with raw meter readings (any rate), it builds per-second and per-minute statistics,
detects short loud events and sustained noise episodes, tracks meter health, stores
history, pushes notifications, publishes extra MQTT sensors and streams live data to
dashboard clients.
"""

import queue
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from .acoustics import (
    WHO_ROAD_LDEN_DB,
    WHO_ROAD_LNIGHT_DB,
    db_to_energy,
    energy_to_db,
    exceedance_levels,
    in_time_range,
    leq,
    median,
    parse_hhmm,
    round_or_none,
)
from .history import HistoryStore, local_midnight
from .labtests import LabRunner

LIVE_SECONDS = 3600
OFFLINE_AFTER_S = 15.0
STUCK_AFTER_S = 600.0


def _fmt_clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def _fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 90:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d}"


class SecondBucket:
    __slots__ = ("t", "n", "energy", "lmax", "lmin")

    def __init__(self, t: int):
        self.t = t
        self.n = 0
        self.energy = 0.0
        self.lmax = None
        self.lmin = None

    def add(self, db: float):
        self.n += 1
        self.energy += db_to_energy(db)
        if self.lmax is None or db > self.lmax:
            self.lmax = db
        if self.lmin is None or db < self.lmin:
            self.lmin = db

    def finalize(self) -> Dict[str, Any]:
        return {
            "t": self.t,
            "laeq": round(energy_to_db(self.energy / self.n), 1),
            "lmax": self.lmax,
            "lmin": self.lmin,
            "n": self.n,
        }


class MinuteBucket:
    def __init__(self, t: int):
        self.t = t
        self.levels: List[float] = []
        self.energy = 0.0
        self.lmax = None
        self.lmin = None

    def add(self, sec: Dict[str, Any]):
        self.levels.append(sec["laeq"])
        self.energy += db_to_energy(sec["laeq"])
        self.lmax = sec["lmax"] if self.lmax is None else max(self.lmax, sec["lmax"])
        self.lmin = sec["lmin"] if self.lmin is None else min(self.lmin, sec["lmin"])

    def finalize(self) -> Optional[Dict[str, Any]]:
        if not self.levels:
            return None
        stats = exceedance_levels(self.levels)
        energy = self.energy / len(self.levels)
        return {
            "ts": self.t,
            "secs": len(self.levels),
            "energy": energy,
            "laeq": round(energy_to_db(energy), 2),
            "lmin": self.lmin,
            "lmax": self.lmax,
            "l10": round_or_none(stats["l10"], 2),
            "l50": round_or_none(stats["l50"], 2),
            "l90": round_or_none(stats["l90"], 2),
        }


class EventDetector:
    """Short loud events on per-second Lmax with hysteresis and a quiet hold time."""

    MAX_EVENT_S = 600
    PREROLL_S = 5

    def __init__(self):
        self.active = False
        self.start = None
        self.peak = None
        self.peak_t = None
        self.energy = 0.0
        self.secs = 0
        self.quiet = 0
        self.curve: List[List[float]] = []
        self.preroll: deque = deque(maxlen=self.PREROLL_S)

    def update(self, sec: Dict[str, Any], trigger: float, hysteresis: float, hold_s: int):
        point = [sec["t"], sec["lmax"]]
        if not self.active:
            self.preroll.append(point)
            if sec["lmax"] >= trigger:
                self.active = True
                self.start = sec["t"]
                self.peak, self.peak_t = sec["lmax"], sec["t"]
                self.energy, self.secs, self.quiet = db_to_energy(sec["laeq"]), 1, 0
                self.curve = [list(p) for p in self.preroll]
                return "start", {"start": self.start, "peak": self.peak, "trigger": trigger}
            return None
        self.curve.append(point)
        if sec["lmax"] > self.peak:
            self.peak, self.peak_t = sec["lmax"], sec["t"]
        if sec["lmax"] < trigger - hysteresis:
            self.quiet += 1
        else:
            self.quiet = 0
            self.energy += db_to_energy(sec["laeq"])
            self.secs += 1
        too_long = sec["t"] - self.start >= self.MAX_EVENT_S
        if self.quiet >= hold_s or too_long:
            end = sec["t"] - self.quiet + 1
            event = {
                "start": float(self.start),
                "end": float(end),
                "peak": self.peak,
                "peak_t": self.peak_t,
                "laeq": round(energy_to_db(self.energy / max(1, self.secs)), 1),
                "curve": self.curve[-(self.MAX_EVENT_S + self.PREROLL_S):],
                "trigger": trigger,
            }
            self.active = False
            self.preroll.clear()
            self.curve = []
            return "end", event
        return None


class EpisodeDetector:
    """Sustained noise: rolling 60 s median (L50) above a day/night threshold."""

    WINDOW_S = 60
    MIN_SECS = 30
    END_MARGIN_DB = 3.0

    def __init__(self):
        self.window: deque = deque()
        self.active = False
        self.start = None
        self.peak = None
        self.energy = 0.0
        self.secs = 0
        self.quiet = 0
        self.events = 0
        self.threshold = None
        self.l50 = None

    def update(self, sec: Dict[str, Any], threshold: float, end_minutes: int):
        self.window.append((sec["t"], sec["laeq"], sec["lmax"]))
        while self.window and self.window[0][0] <= sec["t"] - self.WINDOW_S:
            self.window.popleft()
        if len(self.window) < self.MIN_SECS:
            self.l50 = None
            return None
        self.l50 = median([v for _, v, _ in self.window])
        if not self.active:
            if self.l50 >= threshold:
                self.active = True
                self.threshold = threshold
                self.start = self.window[0][0]
                self.peak = max(m for _, _, m in self.window)
                self.energy = sum(db_to_energy(v) for _, v, _ in self.window)
                self.secs = len(self.window)
                self.quiet = 0
                self.events = 0
                return "start", {"start": self.start, "l50": self.l50, "threshold": threshold}
            return None
        self.threshold = threshold
        self.peak = max(self.peak, sec["lmax"])
        self.energy += db_to_energy(sec["laeq"])
        self.secs += 1
        if self.l50 < threshold - self.END_MARGIN_DB:
            self.quiet += 1
        else:
            self.quiet = 0
        if self.quiet >= end_minutes * 60:
            end = sec["t"] - self.quiet
            episode = {
                "start": float(self.start),
                "end": float(max(end, self.start + 1)),
                "peak": self.peak,
                "laeq": round(energy_to_db(self.energy / max(1, self.secs)), 1),
                "events": self.events,
                "threshold": threshold,
            }
            self.active = False
            return "end", episode
        return None


class LocalHub:
    def __init__(self, settings, env, store: HistoryStore, notifier=None, logger=None, mqtt=None,
                 device_name: str = "Mute Box", mode: str = "cloud", version: str = "",
                 clock: Callable[[], float] = time.time):
        self.settings = settings
        self.env = env
        self.store = store
        self.notifier = notifier
        self.logger = logger
        self.mqtt = mqtt
        self.device_name = device_name
        self.mode = mode
        self.version = version
        self.clock = clock
        self.started_at = clock()
        self.lab = LabRunner(store, settings, notifier, logger, history_levels=self.history_levels)

        self._in_lock = threading.Lock()
        self._incoming: deque = deque(maxlen=50000)
        self._state_lock = threading.RLock()
        self.seconds: deque = deque(maxlen=LIVE_SECONDS)
        self._cur_sec: Optional[SecondBucket] = None
        self._cur_min: Optional[MinuteBucket] = None
        self._sample_times: deque = deque()
        self.latest: Optional[float] = None
        self.latest_ts: Optional[float] = None
        self._last_change_ts: Optional[float] = None
        self._last_value: Optional[float] = None
        self.total_samples = 0

        self.events = EventDetector()
        self.episodes = EpisodeDetector()
        self.health = {"online": False, "stuck": False, "rate_hz": 0.0}
        self._offline_since: Optional[float] = None

        day0 = local_midnight(clock())
        self._today = datetime.fromtimestamp(clock()).date()
        self.events_today = store.count_events(day0, "event")
        self.episodes_today = store.count_events(day0, "episode")

        self._subscribers: List[queue.Queue] = []
        self._sub_lock = threading.Lock()
        self._last_broadcast = 0.0
        self._last_mqtt = 0.0
        self._last_daily_check = 0.0
        self._last_purge = 0.0
        self._offset = float(settings.get("calibration_offset_db") or 0.0)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.settings_rev = 0

    # ----------------------------------------------------------------- inputs
    def apply_calibration(self, db: float) -> float:
        off = self._offset
        if not off:
            return db
        return round(db + off, 1)

    def on_sample(self, ts: float, db: float):
        """Thread-safe and cheap: called from the measurement loop for every reading."""
        if db is None:
            return
        with self._in_lock:
            self._incoming.append((ts, float(db)))

    def settings_changed(self):
        self._offset = float(self.settings.get("calibration_offset_db") or 0.0)
        self.settings_rev += 1
        if self.mqtt and hasattr(self.mqtt, "publish_lab_settings"):
            self.mqtt.publish_lab_settings(self.settings.to_dict())

    # -------------------------------------------------------------- lifecycle
    def start(self):
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run, name="mutelab-hub", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self._close_minute()
        if self.notifier:
            self.notifier.flush(2.0)
            self.notifier.stop()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.tick(self.clock())
            except Exception as exc:  # never let the hub thread die
                if self.logger:
                    self.logger.warning(f"[LAB] hub tick failed: {exc}")
            self._stop.wait(0.2)

    # ------------------------------------------------------------- processing
    def tick(self, now: float):
        with self._in_lock:
            batch = list(self._incoming)
            self._incoming.clear()
        new_seconds = []
        with self._state_lock:
            for ts, db in batch:
                self._ingest(ts, db, new_seconds)
            if self._cur_sec is not None and now - self._cur_sec.t >= 2.0:
                new_seconds.append(self._close_second())
            self._update_health(now)
        for sec in new_seconds:
            self._on_second(sec)
        self.lab.tick(now)
        if new_seconds or now - self._last_broadcast >= 1.0:
            self._broadcast(now, new_seconds)
        if now - self._last_mqtt >= 10.0:
            self._last_mqtt = now
            self._publish_mqtt()
        if now - self._last_daily_check >= 30.0:
            self._last_daily_check = now
            self._daily_housekeeping(now)

    def _ingest(self, ts: float, db: float, new_seconds: list):
        self.total_samples += 1
        self.latest, self.latest_ts = db, ts
        self._sample_times.append(ts)
        if self._last_value is None or abs(db - self._last_value) > 0.001:
            self._last_change_ts = ts
        self._last_value = db
        self.lab.on_sample(ts, db)
        sec_t = int(ts)
        if self._cur_sec is not None and sec_t > self._cur_sec.t:
            new_seconds.append(self._close_second())
        if self._cur_sec is None:
            self._cur_sec = SecondBucket(sec_t)
        self._cur_sec.add(db)

    def _close_second(self) -> Dict[str, Any]:
        sec = self._cur_sec.finalize()
        self._cur_sec = None
        self.seconds.append(sec)
        return sec

    def _close_minute(self):
        if self._cur_min is None:
            return
        row = self._cur_min.finalize()
        self._cur_min = None
        if row:
            try:
                self.store.add_minute(row)
            except Exception as exc:
                if self.logger:
                    self.logger.warning(f"[LAB] could not store minute: {exc}")

    def _threshold_for(self, ts: float) -> Tuple[float, bool]:
        night = in_time_range(datetime.fromtimestamp(ts), self.settings.get("quiet_hours"))
        key = "episode_night_db" if night else "episode_db"
        return float(self.settings.get(key)), night

    def _on_second(self, sec: Dict[str, Any]):
        if self.lab.in_test_window(sec["t"]):
            # Lab test signals are not environmental noise: keep them out of history and alerts.
            return
        minute_t = sec["t"] - sec["t"] % 60
        if self._cur_min is not None and self._cur_min.t != minute_t:
            self._close_minute()
        if self._cur_min is None:
            self._cur_min = MinuteBucket(minute_t)
        self._cur_min.add(sec)

        day = datetime.fromtimestamp(sec["t"]).date()
        if day != self._today:
            self._today = day
            self.events_today = 0
            self.episodes_today = 0

        s = self.settings
        res = self.events.update(sec, float(s.get("event_trigger_db")), float(s.get("event_hysteresis_db")),
                                 int(s.get("event_hold_s")))
        if res:
            self._handle_event(*res)
        threshold, night = self._threshold_for(sec["t"])
        res = self.episodes.update(sec, threshold, int(s.get("episode_end_minutes")))
        if res:
            self._handle_episode(*res, night=night)

    def _handle_event(self, kind: str, info: Dict[str, Any]):
        if kind == "start":
            if self.episodes.active:
                self.episodes.events += 1
            return
        self.events_today += 1
        event_id = self.store.add_event("event", info["start"], info["end"], info["peak"], info["laeq"],
                                        curve=info["curve"], meta={"trigger": info["trigger"]})
        if self.logger:
            self.logger.info(f"[LAB] event #{event_id}: peak {info['peak']:.1f} dB, "
                             f"{info['end'] - info['start']:.0f} s")
        if self.notifier:
            self.notifier.send(
                "events",
                f"💥 {info['peak']:.0f} dB at {_fmt_clock(info['start'])}",
                f"Loud event on {self.device_name}: peak {info['peak']:.1f} dB, lasted "
                f"{_fmt_duration(info['end'] - info['start'])} (trigger {info['trigger']:.0f} dB).",
                priority="default",
                tags=["loud_sound"],
                cooldown_key="event",
                cooldown_s=float(self.settings.get("event_notify_cooldown_s")),
            )

    def _handle_episode(self, kind: str, info: Dict[str, Any], night: bool):
        if kind == "start":
            when = "night" if night else "day"
            if self.logger:
                self.logger.info(f"[LAB] episode started (L50 {info['l50']:.1f} ≥ {info['threshold']:.0f} dB)")
            if self.notifier:
                self.notifier.send(
                    "episodes",
                    f"🔊 Noise started on {self.device_name}",
                    f"Sustained noise since {_fmt_clock(info['start'])}: 1-min L50 {info['l50']:.1f} dB "
                    f"(> {info['threshold']:.0f} dB {when} threshold). I'll tell you when it stops.",
                    priority="high" if night else "default",
                    tags=["loudspeaker", "crescent_moon"] if night else ["loudspeaker"],
                )
            if self.mqtt and hasattr(self.mqtt, "publish_lab_episode"):
                self.mqtt.publish_lab_episode(True)
            return
        self.episodes_today += 1
        duration = info["end"] - info["start"]
        self.store.add_event("episode", info["start"], info["end"], info["peak"], info["laeq"],
                             meta={"events": info["events"], "threshold": info["threshold"]})
        if self.logger:
            self.logger.info(f"[LAB] episode ended: {_fmt_duration(duration)}, LAeq {info['laeq']:.1f} dB")
        if self.notifier:
            self.notifier.send(
                "episodes",
                f"✅ Quiet again on {self.device_name}",
                f"Noise lasted {_fmt_duration(duration)} ({_fmt_clock(info['start'])}–{_fmt_clock(info['end'])}): "
                f"LAeq {info['laeq']:.1f} dB, peak {info['peak']:.1f} dB, {info['events']} loud event(s).",
                tags=["white_check_mark"],
            )
        if self.mqtt and hasattr(self.mqtt, "publish_lab_episode"):
            self.mqtt.publish_lab_episode(False)

    def _update_health(self, now: float):
        while self._sample_times and self._sample_times[0] < now - 10.0:
            self._sample_times.popleft()
        rate = len(self._sample_times) / 10.0
        online = self.latest_ts is not None and now - self.latest_ts < OFFLINE_AFTER_S
        stuck = bool(online and self._last_change_ts is not None and now - self._last_change_ts > STUCK_AFTER_S)
        prev = dict(self.health)
        self.health = {"online": online, "stuck": stuck, "rate_hz": round(rate, 1)}
        if prev.get("online") and not online:
            self._offline_since = now
            self._notify_health("📴 Meter offline",
                                f"No readings from the sound meter on {self.device_name} for {OFFLINE_AFTER_S:.0f} s. "
                                "Check the USB cable or power.", "high")
        elif online and not prev.get("online") and self._offline_since:
            gone = now - self._offline_since
            self._offline_since = None
            self._notify_health("🔌 Meter back online", f"Readings resumed after {_fmt_duration(gone)}.", "low")
        if stuck and not prev.get("stuck"):
            self._notify_health("🧊 Meter looks frozen",
                                f"The reading has not changed for {STUCK_AFTER_S / 60:.0f} min "
                                f"({self.latest} dB). Unplug/replug the meter.", "high")

    def _notify_health(self, title: str, message: str, priority: str):
        if self.logger:
            self.logger.warning(f"[LAB] {title}: {message}")
        if self.notifier:
            self.notifier.send("health", title, message, priority=priority, tags=["warning"],
                               cooldown_key=f"health:{title}", cooldown_s=600)

    def report_fatal(self, reason: str):
        """Called right before the client exits (e.g. meter unplugged). Rate-limited to once per hour."""
        if not self.notifier or not self.notifier.is_enabled("health"):
            return
        last = self.store.get_meta("last_fatal_notify", 0) or 0
        if time.time() - float(last) < 3600:
            return
        self.store.set_meta("last_fatal_notify", time.time())
        self.notifier.send("health", "📴 Mute client stopped", f"{self.device_name}: {reason}", priority="high",
                           tags=["warning"])
        self.notifier.flush(3.0)

    # ----------------------------------------------------------------- stats
    def stats(self) -> Dict[str, Any]:
        with self._state_lock:
            secs = list(self.seconds)
        if not secs:
            return {}
        t_ref = secs[-1]["t"]

        def window(seconds):
            return [s for s in secs if s["t"] > t_ref - seconds]

        w1, w15, w60 = window(60), window(900), secs
        lv15 = [s["laeq"] for s in w15]
        ex = exceedance_levels(lv15)
        return {
            "laeq_1m": round_or_none(leq(s["laeq"] for s in w1)),
            "laeq_15m": round_or_none(leq(lv15)),
            "laeq_1h": round_or_none(leq(s["laeq"] for s in w60)),
            "lmax_1m": max((s["lmax"] for s in w1), default=None),
            "lmax_15m": max((s["lmax"] for s in w15), default=None),
            "lmin_15m": min((s["lmin"] for s in w15), default=None),
            "l10_15m": round_or_none(ex["l10"]),
            "l50_15m": round_or_none(ex["l50"]),
            "l90_15m": round_or_none(ex["l90"]),
        }

    def snapshot(self, now: Optional[float] = None) -> Dict[str, Any]:
        now = now or self.clock()
        threshold, night = self._threshold_for(now)
        ep = self.episodes
        ev = self.events
        return {
            "t": now,
            "db": self.latest,
            "db_t": self.latest_ts,
            "stats": self.stats(),
            "event": {"active": ev.active, "since": ev.start if ev.active else None,
                      "peak": ev.peak if ev.active else None},
            "episode": {
                "active": ep.active,
                "since": ep.start if ep.active else None,
                "l50_1m": round_or_none(ep.l50),
                "threshold": threshold,
                "night": night,
                "peak": ep.peak if ep.active else None,
            },
            "today": {"events": self.events_today, "episodes": self.episodes_today},
            "health": {**self.health, "uptime_s": int(now - self.started_at), "samples": self.total_samples},
            "calibration_offset_db": self._offset,
            "lab": self.lab.current.to_dict() if self.lab.current else None,
            "settings_rev": self.settings_rev,
        }

    def live_series(self, seconds: int = 900) -> List[List[float]]:
        with self._state_lock:
            secs = list(self.seconds)
        if not secs:
            return []
        t_ref = secs[-1]["t"]
        return [[s["t"], s["laeq"], s["lmax"]] for s in secs if s["t"] > t_ref - seconds]

    def history_levels(self, days: int = 7) -> Dict[str, Optional[Dict[str, float]]]:
        """Typical day / night statistical levels from stored minutes (for auto-tuning)."""
        end = self.clock()
        rows = self.store.minutes(end - days * 86400, end)
        quiet = self.settings.get("quiet_hours")
        groups: Dict[str, Dict[str, list]] = {"day": {"l10": [], "l50": [], "l90": []},
                                              "night": {"l10": [], "l50": [], "l90": []}}
        for r in rows:
            if r["l50"] is None:
                continue
            g = "night" if in_time_range(datetime.fromtimestamp(r["ts"]), quiet) else "day"
            for k in ("l10", "l50", "l90"):
                groups[g][k].append(r[k])
        out: Dict[str, Optional[Dict[str, float]]] = {}
        for g, vals in groups.items():
            if len(vals["l50"]) >= 180:  # at least 3 hours of data
                out[g] = {k: round(median(v), 1) for k, v in vals.items()}
            else:
                out[g] = None
        return out

    def trend(self, today: Optional[date] = None) -> Dict[str, Any]:
        days = self.store.daily(14, today=today)
        recent = [d for d in days[1:8] if d["lden"] is not None]
        previous = [d for d in days[8:14] if d["lden"] is not None]
        recent_lden = leq(d["lden"] for d in recent) if recent else None
        previous_lden = leq(d["lden"] for d in previous) if previous else None
        delta = None
        if recent_lden is not None and previous_lden is not None:
            delta = round(recent_lden - previous_lden, 1)
        return {
            "last7_lden": round_or_none(recent_lden),
            "prev7_lden": round_or_none(previous_lden),
            "delta_db": delta,
            "days_used": [len(recent), len(previous)],
            "who_lden_db": WHO_ROAD_LDEN_DB,
            "who_lnight_db": WHO_ROAD_LNIGHT_DB,
        }

    # ------------------------------------------------------- outputs & chores
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=50)
        with self._sub_lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._sub_lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def _broadcast(self, now: float, new_seconds: List[Dict[str, Any]]):
        self._last_broadcast = now
        with self._sub_lock:
            subs = list(self._subscribers)
        if not subs:
            return
        msg = self.snapshot(now)
        msg["seconds"] = [[s["t"], s["laeq"], s["lmax"]] for s in new_seconds]
        for q in subs:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass

    def _publish_mqtt(self):
        if not self.mqtt or not hasattr(self.mqtt, "publish_lab_stats"):
            return
        st = self.stats()
        if not st:
            return
        payload = {
            **st,
            "events_today": self.events_today,
            "episodes_today": self.episodes_today,
            "episode_active": self.episodes.active,
            "rate_hz": self.health.get("rate_hz"),
            "meter_ok": bool(self.health.get("online") and not self.health.get("stuck")),
            "calibration_offset_db": self._offset,
        }
        self.mqtt.publish_lab_stats(payload)

    def _daily_housekeeping(self, now: float):
        if now - self._last_purge > 86400:
            self._last_purge = now
            try:
                removed = self.store.purge(int(self.env.retention_days))
                if removed and self.logger:
                    self.logger.info(f"[LAB] purged {removed} old history rows")
            except Exception:
                pass
        try:
            at = parse_hhmm(self.settings.get("daily_summary_time"))
        except Exception:
            return
        local = datetime.fromtimestamp(now)
        if (local.hour, local.minute) < (at.hour, at.minute):
            return
        key = local.date().isoformat()
        if self.store.get_meta("daily_summary_sent") == key:
            return
        self.store.set_meta("daily_summary_sent", key)
        self.send_daily_summary(local.date() - timedelta(days=1))

    def daily_summary_text(self, day: date) -> Optional[Tuple[str, str]]:
        d = self.store.day_summary(day)
        if d["laeq_24h"] is None:
            return None
        parts = []
        if d["lden"] is not None:
            flag = "above" if d["lden"] > WHO_ROAD_LDEN_DB else "below"
            parts.append(f"Lden {d['lden']:.1f} dB ({flag} WHO {WHO_ROAD_LDEN_DB:.0f})")
        else:
            parts.append(f"LAeq,24h {d['laeq_24h']:.1f} dB")
        if d["lnight"] is not None:
            parts.append(f"Lnight {d['lnight']:.1f} dB")
        parts.append(f"{d['events']} loud event(s)")
        if d["loudest_event"]:
            le = d["loudest_event"]
            parts.append(f"max {le['peak']:.1f} dB at {_fmt_clock(le['t'])}")
        if d["episodes"]:
            parts.append(f"{d['episodes']} episode(s), {d['episode_minutes']:.0f} min of sustained noise")
        tr = self.trend(today=day + timedelta(days=1))
        if tr["delta_db"] is not None:
            arrow = "quieter" if tr["delta_db"] < 0 else "louder"
            parts.append(f"week trend {tr['delta_db']:+.1f} dB ({arrow})")
        title = f"📊 {self.device_name} — {day.strftime('%a %d %b')}"
        return title, " · ".join(parts)

    def send_daily_summary(self, day: date) -> bool:
        text = self.daily_summary_text(day)
        if not text:
            return False
        if self.mqtt and hasattr(self.mqtt, "publish_lab_daily"):
            self.mqtt.publish_lab_daily(self.store.day_summary(day))
        if self.notifier:
            return self.notifier.send("daily", text[0], text[1], priority="low", tags=["bar_chart"])
        return False
