import logging
import os
import signal
import sys
import time
import math
import threading
import hashlib
from collections import deque
from datetime import datetime, timezone
from typing import Dict, Optional
from pathlib import Path
import shutil
import glob
import random

from backend_client import (
    BackendClient,
    HEARTBEAT_SECONDS,
    MINIMUM_NOISE_LEVEL,
    TIME_WINDOW_SECONDS,
)
from client_version import CLIENT_VERSION, BUILD_LABEL
from config_loader import build_backend_pool, load_config, persist_config, validate_config
from mqtt_client import MuteqMqttClient
from security import load_shared_secret
from usb_reader import find_usb_device, read_spl_value

BUFFER_RETENTION_S = 15
AGG_WINDOW_S = 2.0
HIGH_RES_MARGIN_DB = 5.0
HIGH_RES_RISE_DB = 3.0
# Client-side event detection feature flags (default OFF) and bounds to keep memory predictable.
EVENT_CURVES_ENABLED = os.environ.get("MUTE_EVENT_CURVES_ENABLED", "false").lower() in ("1", "true", "yes", "on")
EVENT_CURVES_HZ = float(os.environ.get("MUTE_EVENT_CURVES_HZ", "15"))
EVENT_PREBUFFER_S = float(os.environ.get("MUTE_EVENT_PREBUFFER_S", "2.0"))
EVENT_POSTBUFFER_S = float(os.environ.get("MUTE_EVENT_POSTBUFFER_S", "1.0"))
EVENT_TRIGGER_DB = float(os.environ.get("MUTE_EVENT_TRIGGER_DB", str(MINIMUM_NOISE_LEVEL)))
EVENT_HYSTERESIS_DB = float(os.environ.get("MUTE_EVENT_HYSTERESIS_DB", "3.0"))
# Quiet hold time (ms) below hysteresis before ending event; prefers new var, falls back for compatibility.
EVENT_END_HOLD_MS = int(
    os.environ.get("MUTE_EVENT_END_HOLD_MS", os.environ.get("MUTE_EVENT_MIN_OVER_MS", "700"))
)
MAX_EVENT_BACKLOG = int(os.environ.get("MUTE_MAX_EVENT_BACKLOG", "20"))
MAX_EVENT_SAMPLES = int(os.environ.get("MUTE_MAX_EVENT_SAMPLES", "1200"))
EVENT_BUFFER_MAX_SAMPLES = int(os.environ.get("MUTE_EVENT_BUFFER_MAX_SAMPLES", "5000"))
TEST_SIGNAL = os.environ.get("MUTE_TEST_SIGNAL", "false").lower() in ("1", "true", "yes", "on")

# Debug logging flag
DEBUG_LOGS = os.environ.get("MUTE_DEBUG_LOGS", "false").lower() in ("1", "true", "yes", "on")

ANSI_RESET = "\033[0m"
ANSI_CYAN = "\033[36m"
ANSI_YELLOW = "\033[33m"
ANSI_RED = "\033[31m"
ANSI_GREEN_BOLD = "\033[1;92m"
ANSI_MAGENTA_BOLD = "\033[1;95m"
ANSI_CYAN_BOLD = "\033[1;96m"
ANSI_WHITE = "\033[37m"


def _setup_base_logger(level: str) -> logging.Logger:
    logging.basicConfig(
        level=getattr(logging, (level or "INFO").upper(), logging.INFO),
        format="%(message)s",
    )
    return logging.getLogger("muteq_client")


def _fmt(level_label: str, msg: str) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    level_color = ANSI_YELLOW
    if level_label == "ERROR":
        level_color = ANSI_RED
    elif level_label == "WARN":
        level_color = ANSI_YELLOW
    elif level_label == "SUCCESS":
        level_color = ANSI_GREEN_BOLD
    return f"{ANSI_CYAN}[{ts}]{ANSI_RESET} {level_color}[{level_label}]{ANSI_RESET} {ANSI_WHITE}{msg}{ANSI_RESET}"


def make_log_helpers(logger_obj: logging.Logger):
    def log_info(msg: str):
        logger_obj.info(_fmt("INFO", msg))

    def log_warn(msg: str):
        logger_obj.warning(_fmt("WARN", msg))

    def log_error(msg: str):
        logger_obj.error(_fmt("ERROR", msg))

    def log_success(msg: str):
        logger_obj.info(_fmt("SUCCESS", msg))

    def log_debug(msg: str):
        if DEBUG_LOGS:
            logger_obj.info(_fmt("DEBUG", msg))

    return log_info, log_warn, log_error, log_success, log_debug


class MuteqClientApp:
    """Main client application orchestrating registration, ingest, and MQTT."""

    def __init__(self, config_path: str):
        self.logger = _setup_base_logger("INFO")
        self.log_info, self.log_warn, self.log_error, self.log_success, self.log_debug = make_log_helpers(self.logger)
        self.config_path = Path(config_path)
        self._ensure_persistent_config()
        self.secret = load_shared_secret()
        self.cfg, needs_reg = load_config(self.config_path, self.logger)
        self._apply_local_mqtt_overrides()
        self.cfg = validate_config(self.cfg, self.logger)
        self.logger = _setup_base_logger(self.cfg.get("log_level", "INFO"))
        self.log_info, self.log_warn, self.log_error, self.log_success, self.log_debug = make_log_helpers(self.logger)
        self.log_info("===============================")
        self.log_info("MUTE CLIENT STARTING")
        self.log_info(f"Client version: {CLIENT_VERSION}")
        self.log_info(f"Build: {BUILD_LABEL}")
        self.log_info("===============================")
        self.log_info(f"Loaded configuration from {self.config_path}")
        self.needs_registration = needs_reg
        self.stop_event = False
        self.backend_client: Optional[BackendClient] = None
        self.mqtt_client: Optional[MuteqMqttClient] = None
        self.usb_device = None
        self.window_duration_s = 1.0
        self.window_lock = threading.Lock()
        self.window_start: Optional[float] = None
        self.window_end: Optional[float] = None
        self.window_max: Optional[float] = None
        self.last_realtime_sent_monotonic: float = 0.0
        self.pending_setup: bool = True
        self.configured_ready: bool = False
        self.realtime_batch: list = []
        self.realtime_batch_lock = threading.Lock()
        # Realtime batching schedule (10s) uses monotonic clock to avoid wall-clock jumps.
        self.batch_interval_s: float = 10.0
        self.last_realtime_batch_send_mono: float = time.monotonic()
        self._last_batch_debug_log_ts: float = time.monotonic()
        # Status tracking for summary display
        self.usb_detected: bool = False
        self.backend_connected: bool = False
        self.first_send_success: bool = False
        self.reset_code: Optional[str] = None
        self._status_summary_displayed: bool = False
        self._last_status_summary_ts: float = 0.0
        self._last_loop_sentinel_ts: float = time.monotonic()
        self.threshold_window_marker: Optional[float] = None
        self.last_realtime_window_id: Optional[int] = None
        # Event buffering (feature-flagged)
        self.event_enabled = EVENT_CURVES_ENABLED
        self.event_buffer = deque(maxlen=EVENT_BUFFER_MAX_SAMPLES)
        self.event_active = False
        self.event_samples: list = []
        self.event_start_ts: Optional[float] = None
        self.event_peak_db: float = 0.0
        self.event_peak_ts: Optional[float] = None
        self.event_quiet_start: Optional[float] = None
        self.event_end_deadline: Optional[float] = None
        # SPL diagnostics
        self.last_spl_ok_ts: float = time.time()
        self.none_spl_count: int = 0
        self.last_spl_value: Optional[float] = None
        if TEST_SIGNAL:
            self.log_info("[SPL] TEST MODE enabled")

    def register_signals(self):
        def handler(signum, frame):
            self.log_info("Shutdown signal received. Exiting...")
            self.stop_event = True

        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    def _fetch_reset_code_from_backend(self):
        """
        Fetch reset_code from backend API instead of local DB.
        """
        device_id = self.cfg.get("assigned_device_id")
        if not device_id or not self.backend_client:
            return None
        try:
            remote_cfg = self.backend_client.fetch_device_config(device_id)
            if isinstance(remote_cfg, dict) and remote_cfg.get("reset_code"):
                code = str(remote_cfg["reset_code"]).strip()
                if code:
                    self.reset_code = code
                    return code
        except Exception as exc:
            self.log_debug(f"[RESET_CODE] Unable to load reset code from backend: {exc}")
        return None

    def ensure_registration(self):
        backend_pool = build_backend_pool(self.cfg)
        preference_index = self.cfg.get("backend_preference_index", 0)
        self.backend_client = BackendClient(self.secret, backend_pool, self.logger, preference_index)
        # Conserve deviceID even if token is missing (we can recover it)
        existing_device_id = self.cfg.get("assigned_device_id")
        if existing_device_id and self.cfg.get("device_token"):
            self.log_info(f"[BOOT] Using existing device identity: device_id={existing_device_id} (skip register)")
            self.needs_registration = False
        elif existing_device_id:
            self.log_info(f"[BOOT] Device ID exists but token missing: device_id={existing_device_id} (will try to recover)")
            # Keep the device_id, only register if we can't recover
        while (self.cfg.get("assigned_device_id") is None) or (self.cfg.get("device_token") is None) or self.needs_registration:
            device_name = self.cfg.get("device_name") or "MUTE Box"
            env_profile = self.cfg.get("environment_profile") or "traffic_roadside"
            custom_label = self.cfg.get("custom_environment_label") or ""
            reg_data = self.backend_client.register_device(device_name, env_profile, custom_label, CLIENT_VERSION) or {}
            device_id = reg_data.get("device_id")
            token = reg_data.get("device_token")
            if device_id and token:
                # Frontend onboarding handled by dash.muteq.eu
                onboarding_url = f"https://dash.muteq.eu/devices/{device_id}/claim"
                banner_lines = [
                    "–––––––––––  DEVICE ONBOARDING  —————–",
                    f"{ANSI_GREEN_BOLD}Onboarding URL: {onboarding_url or 'N/A'}{ANSI_RESET}",
                    "–––––––––––––––––––––––––––––––––––––––",
                ]
                for line in banner_lines:
                    self.log_success(line)
                self.cfg["assigned_device_id"] = device_id
                self.cfg["device_token"] = token
                # Newly registered devices start in pending setup; realtime ingestion remains disabled until cleared.
                self.pending_setup = True
                persist_config(self.config_path, self.cfg, self.logger)
                self.log_success(f"Device registered successfully: device_id={device_id}")
                self.needs_registration = False
            else:
                self.log_error("Registration failed. Retrying in 10 seconds.")
                time.sleep(10)
        device_id = self.cfg.get("assigned_device_id")
        self.log_info(f"[BOOT] Device ID: {device_id}")
        
        self.backend_client.set_credentials(
            device_id,
            self.cfg.get("device_token"),
            self.cfg.get("backend_preference_index", 0),
        )
        self.backend_client.start()
        
        # Try to fetch reset code from backend
        self._fetch_reset_code_from_backend()
        
        # Display onboarding info if pending_setup
        if self.pending_setup:
            self._display_onboarding_info()

    def sync_backend_config(self):
        """
        Fetch device config from backend and apply timing (window_duration_s).
        """
        device_id = self.cfg.get("assigned_device_id")
        if not self.backend_client:
            return
        try:
            remote_cfg = self.backend_client.fetch_device_config(device_id)
            self.backend_connected = True
            # If device not found (404), trigger re-registration
            if remote_cfg is None:
                self.log_warn(f"[CONFIG] Failed to fetch device config (no response)")
                return
            if isinstance(remote_cfg, dict) and remote_cfg.get("_forbidden"):
                # 403 Forbidden - authentication/authorization issue
                self.log_warn(f"[CONFIG] Device {device_id} access forbidden (403). Authentication issue - may need re-registration.")
                self.log_warn(f"[CONFIG] Clearing invalid credentials and triggering re-registration on next cycle.")
                self.needs_registration = True
                # Clear invalid credentials
                self.cfg["device_token"] = None
                persist_config(self.config_path, self.cfg, self.logger)
                # Set pending_setup to show enrollment URL
                self.pending_setup = True
                return
            if isinstance(remote_cfg, dict) and remote_cfg.get("_not_found"):
                # Device not found in backend - need to re-register
                self.log_warn(f"[CONFIG] Device {device_id} not found in backend (404). Device needs to be re-registered.")
                self.log_warn(f"[CONFIG] Clearing invalid credentials and triggering re-registration on next cycle.")
                self.needs_registration = True
                # Clear invalid credentials
                self.cfg["device_token"] = None
                persist_config(self.config_path, self.cfg, self.logger)
                # Set pending_setup to show enrollment URL
                self.pending_setup = True
                return
        except Exception as exc:
            self.backend_connected = False
            self.log_debug(f"[CONFIG] Failed to fetch config: {exc}")
            return
        if isinstance(remote_cfg, dict):
            # Fetch reset code if available
            if remote_cfg.get("reset_code"):
                new_reset_code = str(remote_cfg["reset_code"]).strip()
                if new_reset_code != self.reset_code:
                    self.reset_code = new_reset_code
                    # Display onboarding info again if reset code just became available
                    if self.pending_setup:
                        self._display_onboarding_info()
            # Readiness priority: pending_setup -> status -> claimed -> configured fallback.
            reason = "configured_fallback"
            ready = False
            if "pending_setup" in remote_cfg:
                ready = not bool(remote_cfg.get("pending_setup"))
                reason = "pending_setup"
            elif "status" in remote_cfg:
                ready = remote_cfg.get("status") == "claimed"
                reason = "status"
            elif "claimed" in remote_cfg:
                ready = bool(remote_cfg.get("claimed"))
                reason = "claimed"
            else:
                monitoring_type = remote_cfg.get("monitoring_type")
                thresholds = remote_cfg.get("thresholds") or {}
                location = remote_cfg.get("location") or {}
                timezone_val = remote_cfg.get("timezone")
                thresholds_ok = isinstance(thresholds, dict) and isinstance(thresholds.get("legal_db"), (int, float)) and isinstance(thresholds.get("tolerance_db"), (int, float))
                location_ok = isinstance(location.get("lat"), (int, float)) and isinstance(location.get("lon"), (int, float))
                configured = bool(monitoring_type) and thresholds_ok and location_ok and bool(timezone_val)
                ready = configured
                reason = "configured_fallback"
            prev_ready = self.configured_ready
            self.configured_ready = bool(ready)
            self.pending_setup = not self.configured_ready
            if self.configured_ready and not prev_ready:
                self.log_info(f"[CONFIG] Device READY, switching to NORMAL mode (reason={reason})")
                # Prime the realtime batch schedule so the first flush happens ASAP after READY.
                try:
                    self.last_realtime_batch_send_mono = time.monotonic() - self.batch_interval_s
                except Exception:
                    pass
            self.log_debug(f"[CONFIG] poll device_id={device_id} pending_setup={self.pending_setup} status={remote_cfg.get('status') if remote_cfg else 'N/A'} claimed={remote_cfg.get('claimed') if remote_cfg else 'N/A'} ready={self.configured_ready} reason={reason}")
            try:
                remote_window = float(remote_cfg.get("window_duration_s"))
                if remote_window > 0:
                    # Realtime cadence is fixed to 1s; backend window hints are ignored for rate limiting.
                    self.log_debug(f"[CONFIG] Backend window_duration_s={remote_window}s ignored; client enforces 1s windows")
            except Exception:
                pass
            # Display onboarding info if pending_setup
            if self.pending_setup:
                self._display_onboarding_info()
        else:
            # remote_cfg is not a dict or is None - set default reason
            reason = "no_config"
        self.log_debug(f"[CONFIG] poll device_id={device_id} pending_setup={self.pending_setup} status={remote_cfg.get('status') if isinstance(remote_cfg, dict) else 'N/A'} claimed={remote_cfg.get('claimed') if isinstance(remote_cfg, dict) else 'N/A'} ready={self.configured_ready} reason={reason}")

    def build_device_meta(self) -> Dict:
        location = self.cfg.get("location") or {}
        return {
            "device_id": self.cfg.get("assigned_device_id"),
            "device_name": self.cfg.get("device_name"),
            "address": location.get("address") or "",
            "lat": location.get("lat"),
            "lon": location.get("lon"),
            "country": location.get("country") or "unknown",
            "minimum_noise_level": MINIMUM_NOISE_LEVEL,
            "noise_tolerance_db": MINIMUM_NOISE_LEVEL,
            "environment_profile": self.cfg.get("environment_profile") or "traffic_roadside",
            "custom_usage": self.cfg.get("custom_environment_label") or ""
        }

    def init_mqtt(self):
        if not self.cfg.get("mqtt_enabled"):
            return
        location = self.cfg.get("location") or {}
        self.mqtt_client = MuteqMqttClient(
            device_id=self.cfg.get("assigned_device_id"),
            device_name=self.cfg.get("device_name"),
            address=location.get("address") or "",
            env_profile=self.cfg.get("environment_profile") or "traffic_roadside",
            server=self.cfg.get("mqtt_server") or "",
            port=self.cfg.get("mqtt_port") or 1883,
            username=self.cfg.get("mqtt_user") or "",
            password=self.cfg.get("mqtt_pass") or "",
            tls=bool(self.cfg.get("mqtt_tls")),
            logger=self.logger,
        )
        self.mqtt_client.connect()

    def init_usb(self):
        usb_override = self.cfg.get("usb_override") or {}
        vendor_id = usb_override.get("vendor_id")
        product_id = usb_override.get("product_id")
        try:
            vendor_id_int = int(str(vendor_id), 0) if vendor_id is not None else None
        except Exception:
            vendor_id_int = None
        try:
            product_id_int = int(str(product_id), 0) if product_id is not None else None
        except Exception:
            product_id_int = None
        self.usb_device = find_usb_device(vendor_id_int, product_id_int, self.logger)
        self.usb_detected = self.usb_device is not None
        if self.usb_device:
            self.log_info(f"[USB] Device detected: {self.usb_device}")
        else:
            self.log_warn("[USB] No USB device found")

    def build_payload_base(self, timestamp_iso: str, noise_value: float, peak_value: Optional[float]) -> Dict:
        meta = self.build_device_meta()
        payload = {
            "device_id": meta["device_id"],
            "device_name": meta["device_name"],
            "timestamp": timestamp_iso,
            "noise_value": noise_value,
            "peak_value": peak_value,
            "address": meta["address"],
            "lat": meta["lat"],
            "lon": meta["lon"],
            "country": meta["country"],
            "minimum_noise_level": meta["minimum_noise_level"],
            "noise_tolerance_db": meta["noise_tolerance_db"],
            "environment_profile": meta["environment_profile"],
            "custom_usage": meta["custom_usage"]
        }
        return payload

    def send_heartbeat_if_needed(self, last_heartbeat: float) -> float:
        now = time.time()
        hb_interval = 60 if self.pending_setup else HEARTBEAT_SECONDS
        if now - last_heartbeat < hb_interval:
            return last_heartbeat
        ts = datetime.now(timezone.utc).isoformat()
        meta = self.build_device_meta()
        payload = {
            "device_id": meta["device_id"],
            "device_name": meta["device_name"],
            "timestamp": ts,
            "address": meta["address"],
            "lat": meta["lat"],
            "lon": meta["lon"],
            "country": meta["country"],
            "environment_profile": meta["environment_profile"],
            "custom_usage": meta["custom_usage"]
        }
        if self.backend_client.send_payload("heartbeat", payload, ts):
            self.log_info("[INGEST] Heartbeat sent successfully")
            if self.mqtt_client:
                self.mqtt_client.publish_availability("online")
            # Refresh pending_setup flag periodically via device config to re-enable realtime when completed.
            if self.pending_setup:
                self.sync_backend_config()
            return now
        self.log_warn("Heartbeat send failed; enqueued for retry.")
        self.backend_client.enqueue("heartbeat", payload, ts)
        return now

    def _reset_window_state(self):
        with self.window_lock:
            self.window_start = None
            self.window_end = None
            self.window_max = None
            self.threshold_window_marker = None

    def _emit_window_locked(self, window_start: float, window_end: float, max_db: float):
        start_iso = datetime.fromtimestamp(window_start, tz=timezone.utc).isoformat()
        end_iso = datetime.fromtimestamp(window_end, tz=timezone.utc).isoformat()
        status = "dropped"
        reason = ""
        window_id = int(window_end)
        # Batch realtime points; actual send happens on a timer to avoid burst on backend.
        if self.configured_ready:
            # One point per completed window; do not send here, only buffer.
            with self.realtime_batch_lock:
                self.realtime_batch.append({"ts": int(window_end * 1000), "db": float(max_db)})
            status = "buffered"
        else:
            status = "setup_skip"
        phase_label = "setup" if self.pending_setup else "normal"
        self.log_debug(f"[REALTIME] window={int(window_start)}-{int(window_end)} max_db={max_db:.2f} phase={phase_label} status={status}")

    def _advance_windows(self, current_time: float):
        with self.window_lock:
            if self.window_start is None or self.window_end is None:
                return
            while current_time >= self.window_end:
                if self.window_max is not None:
                    self._emit_window_locked(self.window_start, self.window_end, self.window_max)
                # Move to next window; partial windows with no samples are skipped silently.
                self.window_start = self.window_end
                self.window_end = self.window_start + self.window_duration_s
                self.window_max = None
                self.threshold_window_marker = None

    def _add_sample(self, value: float, sample_time: float):
        with self.window_lock:
            if self.window_start is None or self.window_end is None:
                self.window_start = math.floor(sample_time)
                self.window_end = self.window_start + self.window_duration_s
                self.window_max = value
                self.threshold_window_marker = None
                return
            # Close any elapsed windows before adding the sample.
            while sample_time >= self.window_end:
                if self.window_max is not None:
                    self._emit_window_locked(self.window_start, self.window_end, self.window_max)
                self.window_start = self.window_end
                self.window_end = self.window_start + self.window_duration_s
                self.window_max = None
                self.threshold_window_marker = None
            if self.window_max is None or value > self.window_max:
                self.window_max = value

    def _record_raw_sample(self, ts: float, db_val: float):
        # Ring buffer for event detection; bounded to avoid unbounded RAM growth.
        self.event_buffer.append((ts, db_val))

    def _downsample_event(self, samples: list, start_ts: float, target_hz: float, max_samples: int) -> list:
        """Downsample event samples to target_hz while keeping chronological order and bounded size."""
        if target_hz <= 0:
            return [db for _, db in samples[:max_samples]]
        interval = 1.0 / target_hz
        out = []
        next_cutoff = start_ts
        for ts, db in samples:
            if ts < next_cutoff and out:
                continue
            out.append(db)
            next_cutoff = ts + interval
            if len(out) >= max_samples:
                break
        if len(out) > max_samples:
            out = out[:max_samples]
        return out

    def _start_event(self, ts: float, db_val: float):
        self.event_active = True
        self.event_start_ts = ts
        self.event_peak_db = db_val
        self.event_peak_ts = ts
        self.event_quiet_start = None
        self.event_end_deadline = None
        # Include pre-buffer samples
        pre_start = ts - EVENT_PREBUFFER_S
        pre_samples = [(t, v) for (t, v) in list(self.event_buffer) if t >= pre_start]
        pre_samples.append((ts, db_val))
        self.event_samples = pre_samples

    def _reset_event_state(self):
        self.event_active = False
        self.event_samples = []
        self.event_start_ts = None
        self.event_peak_db = 0.0
        self.event_peak_ts = None
        self.event_quiet_start = None
        self.event_end_deadline = None

    def _finalize_event(self, end_ts: float):
        if not self.event_active or self.event_start_ts is None:
            return
        samples = self.event_samples
        if not samples:
            self._reset_event_state()
            return
        start_ts = self.event_start_ts
        target_hz = EVENT_CURVES_HZ
        ds_samples = self._downsample_event(samples, start_ts, target_hz, MAX_EVENT_SAMPLES)
        duration_ms = int(max(0, (end_ts - start_ts) * 1000))
        # Baseline from lowest prebuffer sample to avoid inflated rise when prebuffer is already loud.
        pre_slice_len = max(1, int(EVENT_PREBUFFER_S * EVENT_CURVES_HZ))
        baseline = min(db for _, db in samples[:pre_slice_len]) if samples else 0.0
        rise_db = self.event_peak_db - baseline
        event_id_src = f"{self.cfg.get('assigned_device_id')}:{int(start_ts * 1000)}"
        event_id = hashlib.sha256(event_id_src.encode("utf-8")).hexdigest()
        payload = {
            "event_id": event_id,
            "start_time": float(start_ts),
            "end_time": float(end_ts),
            "sample_hz": int(target_hz),
            "samples": ds_samples,
            "metrics": {
                "peak_db": float(self.event_peak_db),
                "duration_ms": duration_ms,
                "rise_db": float(rise_db),
                "severity": None  # intentionally unset client-side; server may compute severity later
            }
        }
        ts_iso = datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat()
        sent = self.backend_client.send_payload("event", payload, ts_iso)
        if not sent:
            # Enqueue with bounded backlog; drop oldest if full to avoid RAM growth.
            self.backend_client.enqueue("event", payload, ts_iso, maxlen=MAX_EVENT_BACKLOG)
        self.log_info(f"[EVENT] start={start_ts:.3f} end={end_ts:.3f} peak={self.event_peak_db:.1f} samples={len(ds_samples)} sent={sent}")
        self._reset_event_state()

    def _process_event_detection(self, ts: float, db_val: float):
        if not self.event_enabled:
            return
        # Event detection intentionally decoupled from realtime windowing; do not rely on 1s windows.
        self._record_raw_sample(ts, db_val)
        if not self.event_active:
            if db_val >= EVENT_TRIGGER_DB:
                self._start_event(ts, db_val)
            return
        # Active event: track peak and collect samples
        self.event_samples.append((ts, db_val))
        if db_val > self.event_peak_db:
            self.event_peak_db = db_val
            self.event_peak_ts = ts
        below_thresh = db_val < (self.event_peak_db - EVENT_HYSTERESIS_DB)
        if below_thresh:
            if self.event_quiet_start is None:
                self.event_quiet_start = ts
        else:
            self.event_quiet_start = None
        if self.event_quiet_start and (ts - self.event_quiet_start) >= (EVENT_END_HOLD_MS / 1000.0):
            if self.event_end_deadline is None:
                self.event_end_deadline = ts + EVENT_POSTBUFFER_S
        if self.event_end_deadline and ts >= self.event_end_deadline:
            self._finalize_event(ts)

    def realtime_batch_flusher_loop(self):
        self.log_info("[REALTIME_BATCH] flusher thread started")
        while not self.stop_event:
            try:
                if self.configured_ready:
                    now_mono = time.monotonic()
                    elapsed = now_mono - self.last_realtime_batch_send_mono
                    points = []
                    with self.realtime_batch_lock:
                        queued_points = len(self.realtime_batch)
                        if elapsed >= self.batch_interval_s and queued_points > 0:
                            points = list(self.realtime_batch)
                            self.realtime_batch.clear()
                    if points:
                        if not self.backend_client:
                            self.log_warn("[REALTIME_BATCH] backend_client is None; dropping points")
                        else:
                            now_ts = time.time()
                            self.log_debug(f"[REALTIME_BATCH] sending points={len(points)} elapsed={elapsed:.1f}s interval={self.batch_interval_s}s device_id={self.cfg.get('assigned_device_id')}")
                            payload = {
                                "window_start": int((now_ts - self.batch_interval_s) * 1000),
                                "window_end": int(now_ts * 1000),
                                "sample_hz": 1,
                                "points": points,
                            }
                            ts_iso = datetime.now(timezone.utc).isoformat()
                            sent = self.backend_client.send_payload("realtime", payload, ts_iso)
                            if sent:
                                if not self.first_send_success:
                                    self.first_send_success = True
                                    self.log_success("✅ First data sent successfully to backend")
                                    self._display_status_summary()
                                else:
                                    self.log_debug("[REALTIME_BATCH] send success endpoint=realtime_batch")
                            else:
                                self.log_warn("[REALTIME_BATCH] send failed endpoint=realtime_batch (dropping batch)")
                        self.last_realtime_batch_send_mono = now_mono
                    dbg_now = time.monotonic()
                    if (dbg_now - self._last_batch_debug_log_ts) >= 60.0:
                        with self.realtime_batch_lock:
                            queued_points = len(self.realtime_batch)
                        dbg_elapsed = dbg_now - self.last_realtime_batch_send_mono
                        self.log_debug(f"[REALTIME_BATCH] queued_points={queued_points} elapsed={dbg_elapsed:.1f}s interval={self.batch_interval_s}s ready=True")
                        self._last_batch_debug_log_ts = dbg_now
            except Exception as exc:
                # If this thread dies silently, you'll never see realtime in the dashboard.
                self.log_error(f"[REALTIME_BATCH] flusher crashed: {exc}")
            time.sleep(0.2)

    def measurement_loop(self):
        self.log_info("Starting measurement loop...")
        last_heartbeat = 0.0
        last_spl_log = time.time()
        while not self.stop_event:
            now_ts = time.time()
            if TEST_SIGNAL:
                value = 45.0 + 10.0 * math.sin(now_ts) + (5.0 if random.random() < 0.05 else 0.0)
            else:
                value = read_spl_value(self.usb_device, self.logger)
            if value is None:
                self.none_spl_count += 1
                if (now_ts - self.last_spl_ok_ts) > 5.0:
                    self.log_error(f"[SPL] No samples for 5s (none_count={self.none_spl_count}, usb_present={self._usb_debug_state()})")
                    sys.exit(2)
            else:
                self.last_spl_ok_ts = now_ts
                self.last_spl_value = value
                if (now_ts - last_spl_log) >= 10.0:
                    self.log_info(f"[SPL] OK latest={value:.1f} dB")
                    last_spl_log = now_ts
            if self.pending_setup:
                # During setup: only heartbeat; skip realtime/threshold/events to stay silent.
                last_heartbeat = self.send_heartbeat_if_needed(last_heartbeat)
                # Keep windowing running for visibility; do not early-continue.
            if value is not None:
                self._add_sample(value, now_ts)
                self._process_event_detection(now_ts, value)
            self._advance_windows(now_ts)
            if self.mqtt_client and value is not None:
                self.mqtt_client.publish_realtime(value)
            now_mono = time.monotonic()
            with self.window_lock:
                # Use window max to trigger threshold once per window.
                current_max = self.window_max
                window_start = self.window_start
                threshold_marker = self.threshold_window_marker
            if current_max is not None and window_start is not None and current_max >= MINIMUM_NOISE_LEVEL and threshold_marker != window_start and self.configured_ready:
                ts_iso = datetime.fromtimestamp(window_start + self.window_duration_s, tz=timezone.utc).isoformat()
                threshold_payload = self.build_payload_base(ts_iso, current_max, current_max)
                sent_thr = self.backend_client.send_payload("threshold", threshold_payload, ts_iso)
                if not sent_thr:
                    self.backend_client.enqueue("threshold", threshold_payload, ts_iso)
                if self.mqtt_client:
                    self.mqtt_client.publish_threshold(current_max, current_max)
                with self.window_lock:
                    self.threshold_window_marker = window_start
            if self.configured_ready and (now_mono - self._last_loop_sentinel_ts) >= 60.0:
                with self.realtime_batch_lock:
                    queued_points = len(self.realtime_batch)
                self.log_debug(f"[LOOP] alive queued_points={queued_points} elapsed={now_mono - self.last_realtime_batch_send_mono:.1f}s ready=True")
                self._last_loop_sentinel_ts = now_mono
            last_heartbeat = self.send_heartbeat_if_needed(last_heartbeat)
            time.sleep(0.01)

    def shutdown(self):
        if self.usb_device and hasattr(self.usb_device, "close"):
            try:
                self.usb_device.close()
            except Exception:
                pass
        if self.backend_client:
            self.backend_client.stop()
        if self.mqtt_client:
            self.mqtt_client.disconnect()
        self.log_info("Shutdown complete.")

    def _usb_debug_state(self) -> str:
        bus_exists = os.path.exists("/dev/bus/usb")
        try:
            nodes = len(glob.glob("/dev/bus/usb/*/*"))
        except Exception:
            nodes = 0
        try:
            hidraw = len(glob.glob("/dev/hidraw*"))
        except Exception:
            hidraw = 0
        return f"bus_usb={'Y' if bus_exists else 'N'} nodes={nodes} hidraw={hidraw} uid={os.getuid()} gid={os.getgid()}"

    def _ensure_persistent_config(self):
        config_dir = Path("/config")
        if not (config_dir.exists() and config_dir.is_dir() and os.access(config_dir, os.W_OK)):
            self.log_error("[FATAL] /config is missing or not writable. You must mount a persistent volume.")
            sys.exit(1)
        config_file = config_dir / "config_client.json"
        if not config_file.exists():
            template_path = Path("/app/client_config.json")
            try:
                shutil.copyfile(template_path, config_file)
            except Exception as exc:
                self.log_error(f"[FATAL] Unable to initialize config at {config_file}: {exc}")
                sys.exit(1)
        self.config_path = config_file

    def config_poller_loop(self):
        while not self.stop_event:
            if self.backend_client and self.cfg.get("assigned_device_id"):
                try:
                    self.sync_backend_config()
                except Exception as exc:
                    self.log_warn(f"[CONFIG] poll failed: {exc}")
            interval = 5.0 if self.pending_setup else 30.0
            slept = 0.0
            while not self.stop_event and slept < interval:
                time.sleep(0.2)
                slept += 0.2

    def _apply_local_mqtt_overrides(self):
        def parse_bool(val: str):
            return val.strip().lower() in ("1", "true", "yes", "on")

        overrides = {
            "LOCAL_MQTT_ENABLED": ("mqtt_enabled", "bool"),
            "LOCAL_MQTT_SERVER": ("mqtt_server", "str"),
            "LOCAL_MQTT_PORT": ("mqtt_port", "int"),
            "LOCAL_MQTT_USER": ("mqtt_user", "str"),
            "LOCAL_MQTT_PASS": ("mqtt_pass", "str"),
            "LOCAL_MQTT_TLS": ("mqtt_tls", "bool"),
        }
        for env_key, (cfg_key, kind) in overrides.items():
            raw = os.environ.get(env_key)
            if raw is None:
                continue
            try:
                if kind == "bool":
                    self.cfg[cfg_key] = parse_bool(raw)
                elif kind == "int":
                    self.cfg[cfg_key] = int(raw)
                else:
                    self.cfg[cfg_key] = raw
            except Exception:
                continue

    def _display_onboarding_info(self):
        """Display enrollment URL and reset code if device is pending setup."""
        device_id = self.cfg.get("assigned_device_id")
        if not device_id:
            return
        
        onboarding_url = f"https://dash.muteq.eu/devices/{device_id}/claim"
        
        # Only display once or when reset code becomes available
        if self._status_summary_displayed and not self.reset_code:
            return
            
        separator = "────────────────────────────────────────────────"
        self.log_info("")
        self.log_info(separator)
        self.log_success(f"🌐 Enrollment URL: {ANSI_GREEN_BOLD}{onboarding_url}{ANSI_RESET}")
        
        if self.reset_code:
            self.log_success(f"🔑 Reset Code: {ANSI_GREEN_BOLD}{self.reset_code}{ANSI_RESET}")
            self.log_info(f"{ANSI_YELLOW}⚠️  Save this reset code! You will need it to reset your device later.{ANSI_RESET}")
        else:
            self.log_info("🔑 Reset Code: (fetching from backend...)")
        
        self.log_info(separator)
        self.log_info("")
        
        self._status_summary_displayed = True

    def _display_status_summary(self):
        """Display a clear status summary with USB, backend, first send, reset code, and enrollment URL."""
        now = time.monotonic()
        # Only display once or every 60 seconds if pending_setup
        if self._status_summary_displayed and (now - self._last_status_summary_ts) < 60.0:
            return
        
        separator = "────────────────────────────────────────────────"
        self.log_info("")
        self.log_info(separator)
        self.log_info("STATUS SUMMARY")
        self.log_info(separator)
        
        # USB Detection
        usb_status = f"{ANSI_GREEN_BOLD}✅ OK{ANSI_RESET}" if self.usb_detected else f"{ANSI_RED}❌ FAIL{ANSI_RESET}"
        usb_info = f"USB Device: {usb_status}"
        if self.usb_device:
            usb_info += f" ({self.usb_device})"
        self.log_info(usb_info)
        
        # Backend Connection
        backend_status = f"{ANSI_GREEN_BOLD}✅ OK{ANSI_RESET}" if self.backend_connected else f"{ANSI_RED}❌ FAIL{ANSI_RESET}"
        backend_url = self.cfg.get("backend_failover", [""])[0] or "N/A"
        self.log_info(f"Backend Connection: {backend_status} ({backend_url})")
        
        # First Send
        if self.first_send_success:
            self.log_success("✅ First data sent successfully to backend")
        else:
            self.log_info("⏳ Waiting for first data send...")
        
        # Reset Code and Enrollment URL
        device_id = self.cfg.get("assigned_device_id")
        if device_id:
            if self.reset_code:
                self.log_success(f"🔑 Reset Code: {ANSI_GREEN_BOLD}{self.reset_code}{ANSI_RESET}")
            else:
                self.log_info("🔑 Reset Code: (fetching from backend...)")
            
            if self.pending_setup:
                onboarding_url = f"https://dash.muteq.eu/devices/{device_id}/claim"
                self.log_success(f"🌐 Enrollment URL: {ANSI_GREEN_BOLD}{onboarding_url}{ANSI_RESET}")
        
        self.log_info(separator)
        self.log_info("")
        
        self._status_summary_displayed = True
        self._last_status_summary_ts = now

    def run(self):
        self.register_signals()
        self.ensure_registration()
        self.init_usb()
        self.init_mqtt()
        self.sync_backend_config()
        
        # Display initial status summary
        self._display_status_summary()
        self.log_debug("[BOOT] Starting realtime batch flusher thread")
        threading.Thread(target=self.realtime_batch_flusher_loop, daemon=True).start()
        threading.Thread(target=self.config_poller_loop, daemon=True).start()
        try:
            self.measurement_loop()
        finally:
            self.shutdown()


def main():
    config_path = "/config/config_client.json"
    app = MuteqClientApp(config_path)
    app.run()


if __name__ == "__main__":
    main()
