import json
import re
import threading
import time
from typing import Optional

try:
    from client_version import CLIENT_VERSION
except ImportError:  # pragma: no cover
    CLIENT_VERSION = "unknown"

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover - dependency is installed in the image
    mqtt = None


class MuteqMqttClient:
    """
    Minimal MQTT wrapper that publishes Home Assistant discovery
    and state updates for realtime and threshold events.
    """

    def __init__(
        self,
        device_id: str,
        device_name: str,
        address: str,
        env_profile: str,
        server: str,
        port: int,
        username: str,
        password: str,
        tls: bool,
        logger,
    ):
        self.device_id = device_id
        self.device_name = device_name
        self.address = address
        self.env_profile = env_profile
        self.server = server
        self.port = port
        self.username = username
        self.password = password
        self.tls = tls
        self.logger = logger
        self.client: Optional["mqtt.Client"] = None
        self.connected = False
        self._lock = threading.Lock()
        # Extra Home Assistant entities published by Mute Lab (opt-in).
        self.lab_entities = False
        self._last_lab_settings: Optional[dict] = None

    def connect(self):
        if mqtt is None:
            self.logger.warning("paho-mqtt is not installed; MQTT disabled.")
            return
        try:
            if hasattr(mqtt, "CallbackAPIVersion"):  # paho-mqtt >= 2.0
                self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311)
            else:  # pragma: no cover - paho-mqtt 1.x
                self.client = mqtt.Client(protocol=mqtt.MQTTv311)
            if self.tls:
                self.client.tls_set()
            if self.username:
                self.client.username_pw_set(self.username, self.password or None)
            availability_topic = self._availability_topic()
            self.client.will_set(availability_topic, "offline", qos=1, retain=True)
            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect
            self.client.connect(self.server, int(self.port or 1883), keepalive=30)
            self.client.loop_start()
        except Exception as exc:
            self.logger.warning(f"MQTT connection failed: {exc}")

    def _on_connect(self, client, userdata, flags, rc, properties=None):  # pragma: no cover - callback
        failed = getattr(rc, "is_failure", None)
        if (failed if failed is not None else rc != 0):
            self.logger.warning(f"MQTT connection refused by {self.server}:{self.port}: {rc}")
            return
        with self._lock:
            self.connected = True
        self.logger.info(f"MQTT connected to {self.server}:{self.port}")
        self.publish_discovery()
        self.publish_availability("online")
        if self._last_lab_settings:
            self.publish_lab_settings(self._last_lab_settings)

    def _on_disconnect(self, client, userdata, *args):  # pragma: no cover - callback
        with self._lock:
            self.connected = False
        self.logger.warning(f"MQTT disconnected from {self.server}:{self.port}; paho will reconnect automatically")

    def _availability_topic(self) -> str:
        return f"muteq/{self.device_id}/availability"

    def _realtime_topic(self) -> str:
        return f"muteq/{self.device_id}/noise/realtime"

    def _threshold_topic(self) -> str:
        return f"muteq/{self.device_id}/noise/threshold"

    def publish_discovery(self):
        if not self.connected or not self.client:
            return
        discovery_base = f"homeassistant/sensor/{self.device_id}"
        payload_common = {
            "device": {
                "identifiers": [self.device_id],
                "name": self.device_name,
                "manufacturer": "MUTEq",
                "model": self.env_profile or "Noise monitor",
                "sw_version": CLIENT_VERSION
            },
            "availability_topic": self._availability_topic(),
            "unit_of_measurement": "dB",
            "state_class": "measurement",
        }
        realtime_payload = {
            **payload_common,
            "name": f"{self.device_name} Realtime SPL",
            "state_topic": self._realtime_topic(),
            "unique_id": f"{self.device_id}_realtime",
            "value_template": "{{ value_json.value }}"
        }
        threshold_payload = {
            **payload_common,
            "name": f"{self.device_name} Threshold SPL",
            "state_topic": self._threshold_topic(),
            "unique_id": f"{self.device_id}_threshold",
            "value_template": "{{ value_json.peak }}"
        }
        try:
            self.client.publish(f"{discovery_base}_realtime/config", json.dumps(realtime_payload), qos=1, retain=True)
            self.client.publish(f"{discovery_base}_threshold/config", json.dumps(threshold_payload), qos=1, retain=True)
            if self.lab_entities:
                for component, object_id, payload in self._lab_discovery(payload_common["device"]):
                    self.client.publish(
                        f"homeassistant/{component}/{self.device_id}_{object_id}/config",
                        json.dumps(payload),
                        qos=1,
                        retain=True,
                    )
        except Exception as exc:
            self.logger.warning(f"Failed to publish discovery: {exc}")

    # ------------------------------------------------------------------ Mute Lab
    def _lab_topic(self, suffix: str) -> str:
        return f"muteq/{self.device_id}/lab/{suffix}"

    def _lab_discovery(self, device: dict):
        """(component, object_id, payload) tuples for the Mute Lab entities."""
        stats_topic = self._lab_topic("stats")
        base = {"device": device, "availability_topic": self._availability_topic()}

        def level(object_id, name, key, icon="mdi:waveform", category=None):
            payload = {
                **base,
                "name": name,
                "unique_id": f"{self.device_id}_{object_id}",
                "object_id": f"{self._slug()}_{object_id}",
                "state_topic": stats_topic,
                "value_template": "{{ value_json.%s }}" % key,
                "unit_of_measurement": "dB",
                "device_class": "sound_pressure",
                "state_class": "measurement",
                "icon": icon,
            }
            if category:
                payload["entity_category"] = category
            return ("sensor", object_id, payload)

        entities = [
            level("laeq_1m", "LAeq 1 min", "laeq_1m"),
            level("laeq_15m", "LAeq 15 min", "laeq_15m"),
            level("lmax_1m", "Lmax 1 min", "lmax_1m", icon="mdi:volume-high"),
            level("l90_15m", "Background L90", "l90_15m", icon="mdi:volume-low"),
            level("l10_15m", "Busy level L10", "l10_15m", icon="mdi:volume-medium"),
            level("calibration_offset", "Calibration offset", "calibration_offset_db", icon="mdi:tune",
                  category="diagnostic"),
            ("sensor", "events_today", {
                **base,
                "name": "Loud events today",
                "unique_id": f"{self.device_id}_events_today",
                "object_id": f"{self._slug()}_events_today",
                "state_topic": stats_topic,
                "value_template": "{{ value_json.events_today }}",
                "state_class": "total_increasing",
                "icon": "mdi:alert-decagram",
            }),
            ("sensor", "meter_rate", {
                **base,
                "name": "Meter update rate",
                "unique_id": f"{self.device_id}_meter_rate",
                "object_id": f"{self._slug()}_meter_rate",
                "state_topic": stats_topic,
                "value_template": "{{ value_json.rate_hz }}",
                "unit_of_measurement": "Hz",
                "state_class": "measurement",
                "entity_category": "diagnostic",
                "icon": "mdi:speedometer",
            }),
            ("binary_sensor", "noise_episode", {
                **base,
                "name": "Noise episode",
                "unique_id": f"{self.device_id}_noise_episode",
                "object_id": f"{self._slug()}_noise_episode",
                "state_topic": self._lab_topic("episode"),
                "payload_on": "ON",
                "payload_off": "OFF",
                "device_class": "sound",
            }),
            ("binary_sensor", "meter_problem", {
                **base,
                "name": "Meter problem",
                "unique_id": f"{self.device_id}_meter_problem",
                "object_id": f"{self._slug()}_meter_problem",
                "state_topic": stats_topic,
                "value_template": "{{ 'OFF' if value_json.meter_ok else 'ON' }}",
                "payload_on": "ON",
                "payload_off": "OFF",
                "device_class": "problem",
                "entity_category": "diagnostic",
            }),
        ]
        daily_topic = self._lab_topic("daily")
        for key, name in (("lden", "Lden yesterday"), ("lnight", "Lnight yesterday"), ("laeq_24h", "LAeq 24 h yesterday")):
            entities.append(("sensor", f"{key}_yesterday", {
                **base,
                "name": name,
                "unique_id": f"{self.device_id}_{key}_yesterday",
                "object_id": f"{self._slug()}_{key}_yesterday",
                "state_topic": daily_topic,
                "value_template": "{{ value_json.%s }}" % key,
                "unit_of_measurement": "dB",
                "device_class": "sound_pressure",
                "icon": "mdi:calendar-today",
            }))
        return entities

    def _slug(self) -> str:
        return re.sub(r"[^a-z0-9_]+", "_", (self.device_name or "mute box").lower()).strip("_") or "mute_box"

    def enable_lab_entities(self):
        self.lab_entities = True
        if self.connected:
            self.publish_discovery()

    def _publish_lab(self, suffix: str, payload, retain: bool = False):
        if not self.connected or not self.client:
            return
        try:
            body = payload if isinstance(payload, str) else json.dumps(payload)
            self.client.publish(self._lab_topic(suffix), body, qos=0, retain=retain)
        except Exception as exc:
            self.logger.warning(f"Failed to publish lab MQTT message ({suffix}): {exc}")

    def publish_lab_stats(self, payload: dict):
        self._publish_lab("stats", payload)

    def publish_lab_episode(self, active: bool):
        self._publish_lab("episode", "ON" if active else "OFF", retain=True)

    def publish_lab_daily(self, summary: dict):
        self._publish_lab("daily", summary, retain=True)

    def publish_lab_settings(self, settings: dict):
        self._last_lab_settings = dict(settings)
        self._publish_lab("settings", settings, retain=True)

    def publish_availability(self, state: str):
        if not self.connected or not self.client:
            return
        try:
            self.client.publish(self._availability_topic(), state, qos=1, retain=True)
        except Exception as exc:
            self.logger.warning(f"Failed to publish availability: {exc}")

    def publish_realtime(self, value: float):
        if not self.connected or not self.client:
            return
        try:
            self.client.publish(self._realtime_topic(), json.dumps({"value": value}), qos=0, retain=False)
        except Exception as exc:
            self.logger.warning(f"Failed to publish realtime MQTT message: {exc}")

    def publish_threshold(self, peak: float, latest: float):
        if not self.connected or not self.client:
            return
        try:
            self.client.publish(
                self._threshold_topic(),
                json.dumps({"peak": peak, "latest": latest}),
                qos=0,
                retain=False,
            )
        except Exception as exc:
            self.logger.warning(f"Failed to publish threshold MQTT message: {exc}")

    def disconnect(self):
        if not self.client:
            return
        try:
            self.publish_availability("offline")
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass
        with self._lock:
            self.connected = False
