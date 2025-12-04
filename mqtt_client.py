import json
import threading
import time
from typing import Optional

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

    def connect(self):
        if mqtt is None:
            self.logger.warning("paho-mqtt is not installed; MQTT disabled.")
            return
        try:
            self.client = mqtt.Client(protocol=mqtt.MQTTv311)
            if self.tls:
                self.client.tls_set()
            if self.username:
                self.client.username_pw_set(self.username, self.password or None)
            availability_topic = self._availability_topic()
            self.client.will_set(availability_topic, "offline", qos=1, retain=True)
            self.client.on_connect = self._on_connect
            self.client.connect(self.server, int(self.port or 1883), keepalive=30)
            self.client.loop_start()
        except Exception as exc:
            self.logger.warning(f"MQTT connection failed: {exc}")

    def _on_connect(self, client, userdata, flags, rc, properties=None):  # pragma: no cover - callback
        with self._lock:
            self.connected = True
        self.logger.info(f"MQTT connected to {self.server}:{self.port}")
        self.publish_discovery()
        self.publish_availability("online")

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
                "sw_version": "0.0.26"
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
        except Exception as exc:
            self.logger.warning(f"Failed to publish discovery: {exc}")

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
