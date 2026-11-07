"""Wiring: builds the whole Mute Lab station (settings, history, ntfy, hub, dashboard)."""

import hashlib
import os
import socket
from typing import Optional

from .history import HistoryStore
from .hub import LocalHub
from .notify import Notifier
from .server import DashboardServer
from .settings import LabEnv, LabSettings


class LabStation:
    def __init__(self, env: LabEnv, logger, mqtt=None, device_name: str = "Mute Box", mode: str = "cloud",
                 version: str = "", serve: bool = True):
        self.env = env
        self.logger = logger
        os.makedirs(env.data_dir, exist_ok=True)
        self.settings = LabSettings(os.path.join(env.data_dir, "mute_lab.json"), logger)
        self.store = HistoryStore(os.path.join(env.data_dir, "mute_history.db"))
        click = env.public_url + "/" if env.public_url else ""
        self.notifier = Notifier(env.ntfy_url, env.ntfy_token, click_url=click, logger=logger,
                                 is_enabled=self.settings.notify_enabled)
        self.mqtt = mqtt
        if mqtt is not None and hasattr(mqtt, "enable_lab_entities"):
            mqtt.enable_lab_entities()
            mqtt.publish_lab_settings(self.settings.to_dict())
        self.hub = LocalHub(self.settings, env, self.store, notifier=self.notifier, logger=logger, mqtt=mqtt,
                            device_name=device_name, mode=mode, version=version)
        self.server: Optional[DashboardServer] = None
        if serve:
            try:
                self.server = DashboardServer(self.hub, self.settings, env, logger)
            except OSError as exc:
                logger.warning(f"[LAB] Dashboard disabled: cannot listen on {env.bind}:{env.port} ({exc})")

    def start(self):
        self.hub.start()
        if self.server:
            self.server.start()
        ntfy = f"ntfy topic '{self.notifier.topic}'" if self.notifier.configured else "ntfy off (set MUTE_NTFY_URL)"
        self.logger.info(f"[LAB] Mute Lab ready — history in {self.store.path}, {ntfy}, "
                         f"calibration offset {self.settings.get('calibration_offset_db'):+.1f} dB")
        return self

    def stop(self):
        if self.server:
            self.server.stop()
        self.hub.stop()
        self.store.close()

    # Measurement-loop API -------------------------------------------------------
    def apply_calibration(self, db: float) -> float:
        return self.hub.apply_calibration(db)

    def on_sample(self, ts: float, db: float):
        self.hub.on_sample(ts, db)

    def report_fatal(self, reason: str):
        try:
            self.hub.report_fatal(reason)
        except Exception:
            pass


def local_device_id(data_dir: str) -> str:
    """Stable id for standalone mode (no cloud enrollment)."""
    seed = f"{socket.gethostname()}:{os.path.abspath(data_dir)}"
    return "mute_local_" + hashlib.sha256(seed.encode()).hexdigest()[:8]


def maybe_start_lab(cfg: dict, mqtt_client, logger, version: str) -> Optional[LabStation]:
    """Entry point used by the cloud client (main.py). Returns None unless MUTE_LAB_ENABLED=true."""
    env = LabEnv.from_environ()
    if not env.enabled:
        return None
    try:
        station = LabStation(env, logger, mqtt=mqtt_client, device_name=cfg.get("device_name") or "Mute Box",
                             mode="cloud", version=version)
        return station.start()
    except Exception as exc:  # the lab must never take the cloud client down
        logger.warning(f"[LAB] Mute Lab failed to start: {exc}")
        return None
