"""Local history store (SQLite). Survives restarts, lives next to the client config."""

import json
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from .acoustics import (
    PERIOD_SECONDS,
    energy_to_db,
    lden,
    period_of_hour,
    round_or_none,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS minutes (
    ts INTEGER PRIMARY KEY,      -- minute start, unix seconds (UTC)
    secs INTEGER NOT NULL,       -- seconds with data in this minute
    energy REAL NOT NULL,        -- mean of 10^(L/10) over those seconds
    laeq REAL NOT NULL,
    lmin REAL, lmax REAL,
    l10 REAL, l50 REAL, l90 REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,          -- 'event' (short & loud) or 'episode' (sustained)
    start REAL NOT NULL,
    end REAL NOT NULL,
    peak REAL, laeq REAL, duration_s REAL,
    curve TEXT,                  -- JSON [[t, db], ...] (events only)
    meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(start);
CREATE TABLE IF NOT EXISTS lab_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    test TEXT NOT NULL,
    verdict TEXT,
    summary TEXT,
    result TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def local_midnight(ts: float) -> float:
    d = datetime.fromtimestamp(ts).date()
    return datetime(d.year, d.month, d.day).timestamp()


class HistoryStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ writes
    def add_minute(self, row: Dict[str, Any]):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO minutes (ts, secs, energy, laeq, lmin, lmax, l10, l50, l90) "
                "VALUES (:ts, :secs, :energy, :laeq, :lmin, :lmax, :l10, :l50, :l90)",
                row,
            )
            self._conn.commit()

    def add_minutes(self, rows: List[Dict[str, Any]]):
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO minutes (ts, secs, energy, laeq, lmin, lmax, l10, l50, l90) "
                "VALUES (:ts, :secs, :energy, :laeq, :lmin, :lmax, :l10, :l50, :l90)",
                rows,
            )
            self._conn.commit()

    def add_event(self, kind: str, start: float, end: float, peak: Optional[float], laeq: Optional[float],
                  curve: Optional[list] = None, meta: Optional[dict] = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO events (kind, start, end, peak, laeq, duration_s, curve, meta) VALUES (?,?,?,?,?,?,?,?)",
                (
                    kind,
                    float(start),
                    float(end),
                    peak,
                    laeq,
                    max(0.0, float(end) - float(start)),
                    json.dumps(curve) if curve is not None else None,
                    json.dumps(meta) if meta is not None else None,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def add_lab_run(self, test: str, verdict: str, summary: str, result: dict, ts: Optional[float] = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO lab_runs (ts, test, verdict, summary, result) VALUES (?,?,?,?,?)",
                (ts or time.time(), test, verdict, summary, json.dumps(result)),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def set_meta(self, key: str, value: Any):
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, json.dumps(value)))
            self._conn.commit()

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except Exception:
            return default

    def purge(self, retention_days: int) -> int:
        if retention_days <= 0:
            return 0
        cutoff = time.time() - retention_days * 86400
        with self._lock:
            n1 = self._conn.execute("DELETE FROM minutes WHERE ts < ?", (cutoff,)).rowcount
            n2 = self._conn.execute("DELETE FROM events WHERE start < ?", (cutoff,)).rowcount
            self._conn.commit()
        return n1 + n2

    # ------------------------------------------------------------------- reads
    def minutes(self, start: float, end: float) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM minutes WHERE ts >= ? AND ts < ? ORDER BY ts", (int(start), int(end))
            ).fetchall()
        return [dict(r) for r in rows]

    def minute_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM minutes").fetchone()
        return int(row["n"])

    def series(self, start: float, end: float, bucket_s: int) -> List[Dict[str, Any]]:
        """Bucketed history: energetic LAeq per bucket, max of Lmax, mean of L90 / L10."""
        bucket_s = max(60, int(bucket_s))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT (ts / ?) * ? AS b,
                       SUM(energy * secs) / SUM(secs) AS e,
                       MAX(lmax) AS lmax, MIN(lmin) AS lmin,
                       AVG(l90) AS l90, AVG(l10) AS l10, SUM(secs) AS secs
                FROM minutes WHERE ts >= ? AND ts < ? AND secs > 0
                GROUP BY b ORDER BY b
                """,
                (bucket_s, bucket_s, int(start), int(end)),
            ).fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "t": int(r["b"]),
                    "laeq": round_or_none(energy_to_db(r["e"])),
                    "lmax": round_or_none(r["lmax"]),
                    "lmin": round_or_none(r["lmin"]),
                    "l90": round_or_none(r["l90"]),
                    "l10": round_or_none(r["l10"]),
                    "coverage": round(min(1.0, r["secs"] / float(bucket_s)), 3),
                }
            )
        return out

    def events(self, start: Optional[float] = None, end: Optional[float] = None, kind: Optional[str] = None,
               limit: int = 100, with_curve: bool = False) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if start is not None:
            clauses.append("start >= ?")
            params.append(float(start))
        if end is not None:
            clauses.append("start < ?")
            params.append(float(end))
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        cols = "*" if with_curve else "id, kind, start, end, peak, laeq, duration_s, meta"
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {cols} FROM events {where} ORDER BY start DESC LIMIT ?", (*params, int(limit))
            ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            for key in ("curve", "meta"):
                if item.get(key):
                    try:
                        item[key] = json.loads(item[key])
                    except Exception:
                        item[key] = None
            out.append(item)
        return out

    def event(self, event_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM events WHERE id = ?", (int(event_id),)).fetchone()
        if not row:
            return None
        item = dict(row)
        for key in ("curve", "meta"):
            if item.get(key):
                item[key] = json.loads(item[key])
        return item

    def count_events(self, start: float, kind: str = "event") -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM events WHERE start >= ? AND kind = ?", (float(start), kind)
            ).fetchone()
        return int(row["n"])

    def lab_runs(self, limit: int = 30, test: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            if test:
                rows = self._conn.execute(
                    "SELECT * FROM lab_runs WHERE test = ? ORDER BY ts DESC LIMIT ?", (test, int(limit))
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM lab_runs ORDER BY ts DESC LIMIT ?", (int(limit),)).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            try:
                item["result"] = json.loads(item["result"]) if item.get("result") else None
            except Exception:
                item["result"] = None
            out.append(item)
        return out

    # --------------------------------------------------------------- reports
    def day_summary(self, day: date) -> Dict[str, Any]:
        """Lday / Levening / Lnight / Lden for one local calendar day."""
        start = datetime(day.year, day.month, day.day).timestamp()
        end = (datetime(day.year, day.month, day.day) + timedelta(days=1)).timestamp()
        rows = self.minutes(start, end)
        buckets = {"day": [0.0, 0], "evening": [0.0, 0], "night": [0.0, 0]}
        all_energy, all_secs = 0.0, 0
        lmax = None
        for r in rows:
            secs = int(r["secs"] or 0)
            if secs <= 0:
                continue
            period = period_of_hour(datetime.fromtimestamp(r["ts"]).hour)
            buckets[period][0] += r["energy"] * secs
            buckets[period][1] += secs
            all_energy += r["energy"] * secs
            all_secs += secs
            if r["lmax"] is not None and (lmax is None or r["lmax"] > lmax):
                lmax = r["lmax"]
        levels, coverage = {}, {}
        for period, (energy, secs) in buckets.items():
            levels[period] = energy_to_db(energy / secs) if secs else None
            coverage[period] = round(secs / float(PERIOD_SECONDS[period]), 3)
        complete = all(coverage[p] >= 0.3 for p in coverage)
        events = self.events(start, end, kind="event", limit=10000)
        episodes = self.events(start, end, kind="episode", limit=10000)
        loudest = max(events, key=lambda e: e.get("peak") or 0, default=None)
        return {
            "date": day.isoformat(),
            "laeq_24h": round_or_none(energy_to_db(all_energy / all_secs) if all_secs else None),
            "lday": round_or_none(levels["day"]),
            "levening": round_or_none(levels["evening"]),
            "lnight": round_or_none(levels["night"]),
            "lden": round_or_none(lden(levels["day"], levels["evening"], levels["night"]) if complete else None),
            "lmax": round_or_none(lmax),
            "coverage": coverage,
            "events": len(events),
            "episodes": len(episodes),
            "episode_minutes": round(sum((e.get("duration_s") or 0) for e in episodes) / 60.0, 1),
            "loudest_event": {"t": loudest["start"], "peak": loudest["peak"]} if loudest else None,
        }

    def daily(self, days: int = 14, today: Optional[date] = None) -> List[Dict[str, Any]]:
        today = today or date.today()
        return [self.day_summary(today - timedelta(days=i)) for i in range(days)]

    def first_minute_ts(self) -> Optional[int]:
        with self._lock:
            row = self._conn.execute("SELECT MIN(ts) AS t FROM minutes").fetchone()
        return int(row["t"]) if row and row["t"] is not None else None
