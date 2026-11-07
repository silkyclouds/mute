"""
Settings for Mute Lab.

Two layers:
- LabEnv: infrastructure, read from environment variables only (port, token, ntfy URL...).
- LabSettings: tunable behaviour (thresholds, calibration...). Defaults < env overrides <
  values saved from the dashboard in <data_dir>/mute_lab.json.
"""

import json
import os
import tempfile
import threading
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .acoustics import parse_hhmm, parse_time_range

NOTIFY_KINDS = ("episodes", "events", "health", "daily", "lab")

DEFAULT_SETTINGS: Dict[str, Any] = {
    # Short loud events (door slam, horn, scooter): per-second max above this level.
    "event_trigger_db": 80.0,
    "event_hysteresis_db": 3.0,
    "event_hold_s": 2,
    # Sustained noise "episodes" (party, construction, music): 1-minute rolling L50.
    "episode_db": 65.0,
    "episode_night_db": 55.0,
    "episode_end_minutes": 3,
    "quiet_hours": "22:00-07:00",
    # Added to every reading (set by the lab calibration).
    "calibration_offset_db": 0.0,
    "daily_summary_time": "08:00",
    "notify": ["episodes", "health", "daily", "lab"],
    "event_notify_cooldown_s": 300,
}

_ENV_SETTING_OVERRIDES = {
    "MUTE_LAB_EVENT_DB": ("event_trigger_db", float),
    "MUTE_LAB_EPISODE_DB": ("episode_db", float),
    "MUTE_LAB_EPISODE_NIGHT_DB": ("episode_night_db", float),
    "MUTE_LAB_EPISODE_END_MIN": ("episode_end_minutes", int),
    "MUTE_LAB_QUIET_HOURS": ("quiet_hours", str),
    "MUTE_LAB_CALIBRATION_DB": ("calibration_offset_db", float),
    "MUTE_LAB_DAILY_SUMMARY": ("daily_summary_time", str),
    "MUTE_NTFY_NOTIFY": ("notify", lambda v: [k.strip() for k in v.split(",") if k.strip()]),
}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class LabEnv:
    enabled: bool = False
    port: int = 8080
    bind: str = "0.0.0.0"
    token: str = ""
    data_dir: str = "/config"
    retention_days: int = 400
    ntfy_url: str = ""
    ntfy_token: str = ""
    public_url: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_environ(cls, **overrides) -> "LabEnv":
        env = cls(
            enabled=_env_bool("MUTE_LAB_ENABLED", False),
            port=int(os.environ.get("MUTE_LAB_PORT", "8080")),
            bind=os.environ.get("MUTE_LAB_BIND", "0.0.0.0"),
            token=os.environ.get("MUTE_LAB_TOKEN", "").strip(),
            data_dir=os.environ.get("MUTE_LAB_DATA_DIR", "/config"),
            retention_days=int(os.environ.get("MUTE_LAB_RETENTION_DAYS", "400")),
            ntfy_url=os.environ.get("MUTE_NTFY_URL", "").strip(),
            ntfy_token=os.environ.get("MUTE_NTFY_TOKEN", "").strip(),
            public_url=os.environ.get("MUTE_LAB_PUBLIC_URL", "").strip().rstrip("/"),
        )
        for key, value in overrides.items():
            if value is not None:
                setattr(env, key, value)
        return env


def _validate_value(key: str, value: Any) -> Any:
    if key in ("event_trigger_db", "episode_db", "episode_night_db"):
        v = float(value)
        if not 20.0 <= v <= 140.0:
            raise ValueError(f"{key} must be between 20 and 140 dB")
        return round(v, 1)
    if key == "event_hysteresis_db":
        v = float(value)
        if not 0.5 <= v <= 20.0:
            raise ValueError("event_hysteresis_db must be between 0.5 and 20")
        return round(v, 1)
    if key == "event_hold_s":
        v = int(value)
        if not 1 <= v <= 60:
            raise ValueError("event_hold_s must be between 1 and 60")
        return v
    if key == "episode_end_minutes":
        v = int(value)
        if not 1 <= v <= 120:
            raise ValueError("episode_end_minutes must be between 1 and 120")
        return v
    if key == "calibration_offset_db":
        v = float(value)
        if not -30.0 <= v <= 30.0:
            raise ValueError("calibration_offset_db must be between -30 and +30 dB")
        return round(v, 1)
    if key == "quiet_hours":
        parse_time_range(str(value))
        return str(value).replace(" ", "")
    if key == "daily_summary_time":
        t = parse_hhmm(str(value))
        return f"{t.hour:02d}:{t.minute:02d}"
    if key == "notify":
        if isinstance(value, str):
            value = [k.strip() for k in value.split(",") if k.strip()]
        kinds = [str(k) for k in value]
        unknown = [k for k in kinds if k not in NOTIFY_KINDS]
        if unknown:
            raise ValueError(f"unknown notification kinds: {', '.join(unknown)}")
        return kinds
    if key == "event_notify_cooldown_s":
        v = int(value)
        if not 0 <= v <= 86400:
            raise ValueError("event_notify_cooldown_s must be between 0 and 86400")
        return v
    raise KeyError(key)


class LabSettings:
    """Thread-safe, persisted lab settings."""

    def __init__(self, path: Optional[str], logger=None):
        self.path = path
        self.logger = logger
        self._lock = threading.Lock()
        self._values: Dict[str, Any] = deepcopy(DEFAULT_SETTINGS)
        self._apply_env()
        self._load()

    def _log(self, level: str, msg: str):
        if self.logger:
            getattr(self.logger, level)(msg)

    def _apply_env(self):
        for env_key, (key, cast) in _ENV_SETTING_OVERRIDES.items():
            raw = os.environ.get(env_key)
            if raw is None or raw.strip() == "":
                continue
            try:
                self._values[key] = _validate_value(key, cast(raw))
            except Exception as exc:
                self._log("warning", f"[LAB] Ignoring invalid {env_key}={raw!r}: {exc}")

    def _load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
        except Exception as exc:
            self._log("warning", f"[LAB] Could not read {self.path}: {exc}")
            return
        for key, value in (saved or {}).items():
            if key not in DEFAULT_SETTINGS:
                continue
            try:
                self._values[key] = _validate_value(key, value)
            except Exception as exc:
                self._log("warning", f"[LAB] Ignoring saved {key}={value!r}: {exc}")

    def _save_locked(self):
        if not self.path:
            return
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".mute_lab.", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._values, fh, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def get(self, key: str) -> Any:
        with self._lock:
            return deepcopy(self._values[key])

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return deepcopy(self._values)

    def update(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        """Validate and persist a partial update. Raises ValueError on invalid input."""
        validated = {}
        for key, value in (changes or {}).items():
            if key not in DEFAULT_SETTINGS:
                raise ValueError(f"unknown setting: {key}")
            validated[key] = _validate_value(key, value)
        with self._lock:
            self._values.update(validated)
            self._save_locked()
            return deepcopy(self._values)

    def notify_enabled(self, kind: str) -> bool:
        with self._lock:
            return kind in self._values.get("notify", [])


def notify_kinds() -> List[str]:
    return list(NOTIFY_KINDS)
