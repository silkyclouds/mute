import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Dict, Optional
from pathlib import Path
import shutil

from backend_client import (
    BackendClient,
    HEARTBEAT_SECONDS,
    MINIMUM_NOISE_LEVEL,
    TIME_WINDOW_SECONDS,
)
from config_loader import build_backend_pool, load_config, persist_config, validate_config
from mqtt_client import MuteqMqttClient
from security import load_shared_secret
from usb_reader import find_usb_device, read_spl_value

CLIENT_VERSION = "0.0.26"


def setup_logging(level: str) -> logging.Logger:
    logging.basicConfig(
        level=getattr(logging, (level or "INFO").upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    return logging.getLogger("muteq_client")


class MuteqClientApp:
    """Main client application orchestrating registration, ingest, and MQTT."""

    def __init__(self, config_path: str):
        self.logger = setup_logging("INFO")
        self.config_path = Path(config_path)
        self._ensure_persistent_config()
        self.secret = load_shared_secret()
        self.cfg, needs_reg = load_config(self.config_path, self.logger)
        self._apply_local_mqtt_overrides()
        self.cfg = validate_config(self.cfg, self.logger)
        self.logger = setup_logging(self.cfg.get("log_level", "INFO"))
        self.logger.info(f"Loaded configuration from {self.config_path}")
        self.needs_registration = needs_reg
        self.stop_event = False
        self.backend_client: Optional[BackendClient] = None
        self.mqtt_client: Optional[MuteqMqttClient] = None
        self.usb_device = None

    def register_signals(self):
        def handler(signum, frame):
            self.logger.info("Shutdown signal received. Exiting...")
            self.stop_event = True

        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    def ensure_registration(self):
        backend_pool = build_backend_pool(self.cfg)
        preference_index = self.cfg.get("backend_preference_index", 0)
        self.backend_client = BackendClient(self.secret, backend_pool, self.logger, preference_index)
        while (self.cfg.get("assigned_device_id") is None) or (self.cfg.get("device_token") is None) or self.needs_registration:
            device_name = self.cfg.get("device_name") or "MUTEq Sensor"
            env_profile = self.cfg.get("environment_profile") or "traffic_roadside"
            custom_label = self.cfg.get("custom_environment_label") or ""
            device_id, token = self.backend_client.register_device(device_name, env_profile, custom_label, CLIENT_VERSION)
            if device_id and token:
                self.cfg["assigned_device_id"] = device_id
                self.cfg["device_token"] = token
                persist_config(self.config_path, self.cfg, self.logger)
                self.logger.info(f"[REGISTER] Device registered successfully: device_id={device_id}")
                self.needs_registration = False
            else:
                self.logger.error("Registration failed. Retrying in 10 seconds.")
                time.sleep(10)
        self.backend_client.set_credentials(
            self.cfg.get("assigned_device_id"),
            self.cfg.get("device_token"),
            self.cfg.get("backend_preference_index", 0),
        )
        self.backend_client.start()

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
        if now - last_heartbeat < HEARTBEAT_SECONDS:
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
            self.logger.info("[INGEST] Heartbeat sent successfully")
            if self.mqtt_client:
                self.mqtt_client.publish_availability("online")
            return now
        self.logger.warning("Heartbeat send failed; enqueued for retry.")
        self.backend_client.enqueue("heartbeat", payload, ts)
        return now

    def measurement_loop(self):
        self.logger.info("Starting measurement loop.")
        last_heartbeat = 0.0
        while not self.stop_event:
            window_start = time.time()
            current_peak = 0.0
            latest_value = 0.0
            while (time.time() - window_start) < TIME_WINDOW_SECONDS and not self.stop_event:
                value = read_spl_value(self.usb_device, self.logger)
                if value is None:
                    continue
                latest_value = value
                if value > current_peak:
                    current_peak = value
                time.sleep(0.1)
            ts = datetime.now(timezone.utc).isoformat()
            realtime_payload = self.build_payload_base(ts, current_peak, current_peak)
            sent = self.backend_client.send_payload("realtime", realtime_payload, ts)
            if not sent:
                self.backend_client.enqueue("realtime", realtime_payload, ts)
            if self.mqtt_client:
                self.mqtt_client.publish_realtime(current_peak)
            if current_peak >= MINIMUM_NOISE_LEVEL:
                threshold_payload = self.build_payload_base(ts, latest_value, current_peak)
                sent_thr = self.backend_client.send_payload("threshold", threshold_payload, ts)
                if not sent_thr:
                    self.backend_client.enqueue("threshold", threshold_payload, ts)
                if self.mqtt_client:
                    self.mqtt_client.publish_threshold(current_peak, latest_value)
            last_heartbeat = self.send_heartbeat_if_needed(last_heartbeat)

    def shutdown(self):
        if self.backend_client:
            self.backend_client.stop()
        if self.mqtt_client:
            self.mqtt_client.disconnect()
        self.logger.info("Shutdown complete.")

    def _ensure_persistent_config(self):
        config_dir = Path("/config")
        if not (config_dir.exists() and config_dir.is_dir() and os.access(config_dir, os.W_OK)):
            self.logger.error("[FATAL] /config is missing or not writable. You must mount a persistent volume.")
            sys.exit(1)
        config_file = config_dir / "config_client.json"
        if not config_file.exists():
            template_path = Path("/app/client_config.json")
            try:
                shutil.copyfile(template_path, config_file)
            except Exception as exc:
                self.logger.error(f"[FATAL] Unable to initialize config at {config_file}: {exc}")
                sys.exit(1)
        self.config_path = config_file

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

    def run(self):
        self.register_signals()
        self.ensure_registration()
        self.init_usb()
        self.init_mqtt()
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
