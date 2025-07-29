#!/usr/bin/env python3
"""
Mute Client – lightweight noise-to-MQTT forwarder
Author  : Raphaël Vael
License : CC BY-NC 4.0

Key features
------------
* Reads sound-pressure level from USB (HID) or serial SPL meter.
* Publishes two Home-Assistant sensors     —
    - realtime_noise_level  (every <time_window_duration>)
    - threshold_noise_level (only when ≥ minimum_noise_level)
* Resilient offline queue (SQLite WAL):
    - realtime  messages kept max 1 h
    - threshold messages kept max 48 h
    - all flushed in FIFO order at reconnection.
* No InfluxDB, no Flask, no weather, no Telraam, no Discord.
* Timestamps now include the local timezone offset (RFC‑3339, e.g. 2025‑07‑04T08:17:03+02:00).
"""

# ------------------------------------------------------------------
# CLI arguments
# ------------------------------------------------------------------
import argparse as _argparse

_parser = _argparse.ArgumentParser(description="Mute Client")
_parser.add_argument("--debug", action="store_true",
                     help="enable DEBUG logging (verbose output)")
_args, _unknown = _parser.parse_known_args()

# ------------------------------------------------------------------
# Standard library imports
# ------------------------------------------------------------------
import os
import sys
import json
import time
import signal
import logging
from logging.handlers import RotatingFileHandler
import itertools  # for watchdog back‑off counter
import threading
import sqlite3
from datetime import datetime as dt, timedelta
import base64       # already present further down; remove duplicate later if any

# ------------------------------------------------------------------
# Third-party imports (optional ones handled gracefully)
# ------------------------------------------------------------------
try:
    import usb.core
    import usb.util
except ImportError:
    print("Please install pyusb:  pip install pyusb")
    sys.exit(1)

try:
    import serial
except ImportError:
    serial = None  # serial support disabled if pyserial absent

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("Please install paho-mqtt:  pip install paho-mqtt")
    sys.exit(1)

try:
    import pytz
except ImportError:
    print("Please install pytz:  pip install pytz")
    sys.exit(1)

import base64  # for optional b64‑encoded MQTT password

# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------

# ------------------------------------------------------------------
# Helper to resolve MQTT parameters (env vars override config.json)
# ------------------------------------------------------------------
def _mqtt_param(key: str, default=None):
    """Return MQTT parameter in priority: env var → config.json → default."""
    env_val = os.getenv(f"MQTT_{key.upper()}")
    if env_val is not None:
        return env_val
    val = MQTT_CFG.get(key, default)
    # transparently decode `b64:<…>` passwords and usernames
    if key == "password" and isinstance(val, str) and val.startswith("b64:"):
        try:
            val = base64.b64decode(val[4:]).decode()
        except Exception:
            logger.warning("Unable to decode b64 MQTT password – falling back to raw string")
    if key == "user" and isinstance(val, str) and val.startswith("b64:"):
        try:
            val = base64.b64decode(val[4:]).decode()
        except Exception:
            logger.warning("Unable to decode b64 MQTT user – falling back to raw string")
    return val

# ---------------------------------------------------------------
# Helper to resolve MQTT boolean parameters with proper parsing
# ---------------------------------------------------------------
def _mqtt_bool(key: str, default: bool = False) -> bool:
    """Return MQTT boolean parameter (env var or config) with proper parsing."""
    env_val = os.getenv(f"MQTT_{key.upper()}")
    if env_val is not None:
        return env_val.strip().lower() in ("1", "true", "yes", "on")
    val = MQTT_CFG.get(key, default)
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on")
    return bool(val)

CFG_PATH = os.getenv("MUTE_CFG", "config.json")

# ------------------------------------------------------------------
# Interactive first‑run helper
# ------------------------------------------------------------------
def _prompt_non_empty(label: str, default: str = "") -> str:
    """Prompt until the user enters a non‑blank string (Ctrl‑C exits)."""
    while True:
        val = input(f"{label} [{default}]: ").strip() or default
        if val:
            return val
        print("⚠️  This field is required – please enter a value.")

def ensure_complete_config(cfg_path: str, cfg_obj: dict) -> dict:
    """
    Ask missing mandatory values at first run and persist them back to the
    same JSON file.  Also auto‑generates INFLUX_BUCKET_PREFIX when absent.
    """
    changed = False

    # --- Ensure MQTT section exists ---------------------------------
    mqtt = cfg_obj.setdefault("MQTT_CONFIG", {})
    for key, label in [("server", "MQTT server"),
                       ("port",   "MQTT port"),
                       ("user",   "MQTT user (will be stored base64)"),
                       ("password", "MQTT password (will be stored base64)")]:
        if not mqtt.get(key):
            val = _prompt_non_empty(label, "1883" if key == "port" else "")
            if key == "user":
                val = "b64:" + base64.b64encode(val.encode()).decode()
            if key == "password":
                val = "b64:" + base64.b64encode(val.encode()).decode()
            mqtt[key] = val
            changed = True

    if "tls" not in mqtt or not isinstance(mqtt["tls"], bool):
        val = _prompt_non_empty("Enable TLS? (yes/no)", "yes")
        mqtt["tls"] = val.lower() in ("yes", "y", "true", "1")
        changed = True

    # --- Ensure MAP address elements --------------------------------
    map_cfg = cfg_obj.setdefault("MAP_CONFIG", {})
    if "address" not in map_cfg or not map_cfg["address"]:
        print("\nFull device address (e.g., 123 Main Street, Anytown, Country)")
        map_cfg["address"] = _prompt_non_empty("Address")
        changed = True

    # Country, city, street/num + index (01 by default)
    country = map_cfg.get("country_code") or _prompt_non_empty("Country code (2 letters)", "XX")
    city    = map_cfg.get("city") or _prompt_non_empty("City", "Anytown")
    street  = map_cfg.get("street") or _prompt_non_empty("Street and number", "123 Main Street")
    index   = map_cfg.get("device_index") or _prompt_non_empty("Device index (01,02…)", "01")
    if not map_cfg.get("country_code"):   map_cfg["country_code"] = country; changed = True
    if not map_cfg.get("city"):           map_cfg["city"] = city;   changed = True
    if not map_cfg.get("street"):         map_cfg["street"] = street;changed = True
    if not map_cfg.get("device_index"):   map_cfg["device_index"] = index;changed = True

    # --- Generate or retrieve bucket prefix -------------------------
    def _slug(s: str) -> str:
        """Utility to slugify address parts (uppercase, no spaces)."""
        return s.upper().replace(" ", "")

    if not cfg_obj.get("INFLUX_BUCKET_PREFIX"):
        prefix = f"{_slug(country)}_{_slug(city)}_{_slug(street)}_{index}"
        cfg_obj["INFLUX_BUCKET_PREFIX"] = prefix
        changed = True
        print(f"✅ Generated INFLUX_BUCKET_PREFIX: {prefix}")
    else:
        prefix = cfg_obj["INFLUX_BUCKET_PREFIX"]

    # Ensure we also have a device_name – default to the same prefix
    dev_cfg = cfg_obj.setdefault("DEVICE_AND_NOISE_MONITORING_CONFIG", {})
    if not dev_cfg.get("device_name"):
        dev_cfg["device_name"] = prefix
        changed = True

    # Save back if needed
    if changed:
        with open(cfg_path, "w") as f:
            json.dump(cfg_obj, f, indent=2)
        print(f"⚙️  Config file {cfg_path} updated.")

    return cfg_obj


def load_config(path: str):
    with open(path, "r") as f:
        return json.load(f)

try:
    cfg = load_config(CFG_PATH)
except (FileNotFoundError, json.JSONDecodeError):
    print(f"⚠️  Config file {CFG_PATH} not found or invalid. Launching setup assistant...")
    cfg = {}

cfg = ensure_complete_config(CFG_PATH, cfg)

# ------------------------------------------------------------------
# Interactive first‑run helper
# ------------------------------------------------------------------
def _prompt_non_empty(label: str, default: str = "") -> str:
    """Prompt until the user enters a non‑blank string (Ctrl‑C exits)."""
    while True:
        val = input(f"{label} [{default}]: ").strip() or default
        if val:
            return val
        print("⚠️  This field is required – please enter a value.")

def ensure_complete_config(cfg_path: str, cfg_obj: dict) -> dict:
    """
    Ask missing mandatory values at first run and persist them back to the
    same JSON file.  Also auto‑generates INFLUX_BUCKET_PREFIX when absent.
    """
    changed = False

    # --- Ensure MQTT section exists ---------------------------------
    mqtt = cfg_obj.setdefault("MQTT_CONFIG", {})
    for key, label in [("server", "MQTT server"),
                       ("port",   "MQTT port"),
                       ("user",   "MQTT user (will be stored base64)"),
                       ("password", "MQTT password (will be stored base64)")]:
        if not mqtt.get(key):
            val = _prompt_non_empty(label, "1883" if key == "port" else "")
            if key == "user":
                val = "b64:" + base64.b64encode(val.encode()).decode()
            if key == "password":
                val = "b64:" + base64.b64encode(val.encode()).decode()
            mqtt[key] = val
            changed = True

    if "tls" not in mqtt or not isinstance(mqtt["tls"], bool):
        val = _prompt_non_empty("Enable TLS? (yes/no)", "yes")
        mqtt["tls"] = val.lower() in ("yes", "y", "true", "1")
        changed = True

    # --- Ensure MAP address elements --------------------------------
    map_cfg = cfg_obj.setdefault("MAP_CONFIG", {})
    if "address" not in map_cfg or not map_cfg["address"]:
        print("\nFull device address (e.g., Avenue Montefiore 106, Esneux, Belgium)")
        map_cfg["address"] = _prompt_non_empty("Address")
        changed = True

    # Country, city, street/num + index (01 by default)
    country = map_cfg.get("country_code") or _prompt_non_empty("Country code (2 letters)", "BE")
    city    = map_cfg.get("city") or _prompt_non_empty("City", "Esneux")
    street  = map_cfg.get("street") or _prompt_non_empty("Street and number", "Avenue Montefiore 106")
    index   = map_cfg.get("device_index") or _prompt_non_empty("Device index (01,02…)", "01")
    if not map_cfg.get("country_code"):   map_cfg["country_code"] = country; changed = True
    if not map_cfg.get("city"):           map_cfg["city"] = city;   changed = True
    if not map_cfg.get("street"):         map_cfg["street"] = street;changed = True
    if not map_cfg.get("device_index"):   map_cfg["device_index"] = index;changed = True

    # --- Generate or retrieve bucket prefix -------------------------
    def _slug(s: str) -> str:
        """Utility to slugify address parts (uppercase, no spaces)."""
        return s.upper().replace(" ", "")

    if not cfg_obj.get("INFLUX_BUCKET_PREFIX"):
        prefix = f"{_slug(country)}_{_slug(city)}_{_slug(street)}_{index}"
        cfg_obj["INFLUX_BUCKET_PREFIX"] = prefix
        changed = True
        print(f"✅ Generated INFLUX_BUCKET_PREFIX: {prefix}")
    else:
        prefix = cfg_obj["INFLUX_BUCKET_PREFIX"]

    # Ensure we also have a device_name – default to the same prefix
    dev_cfg = cfg_obj.setdefault("DEVICE_AND_NOISE_MONITORING_CONFIG", {})
    if not dev_cfg.get("device_name"):
        dev_cfg["device_name"] = prefix
        changed = True

    # Save back if needed
    if changed:
        with open(cfg_path, "w") as f:
            json.dump(cfg_obj, f, indent=2)
        print(f"⚙️  Config file {cfg_path} updated.")

    return cfg_obj

# Call it immediately so that subsequent code has complete config
cfg = ensure_complete_config(CFG_PATH, cfg)

# Sub-sections we keep
DEVICE_CFG = cfg.get("DEVICE_AND_NOISE_MONITORING_CONFIG", {})
SERIAL_CFG = cfg.get("SERIAL_CONFIG", {})
MQTT_CFG = cfg.get("MQTT_CONFIG", {})
TIMEZONE_CFG = cfg.get("TIMEZONE_CONFIG", {})
MAP_CFG = cfg.get("MAP_CONFIG", {"address": ""})

# Thresholds & timings
MIN_NOISE = DEVICE_CFG.get("minimum_noise_level", 80)          # dB
TIME_WINDOW = DEVICE_CFG.get("time_window_duration", 2)        # seconds

# USB IDs
vid_str = DEVICE_CFG.get("usb_vendor_id", "")
pid_str = DEVICE_CFG.get("usb_product_id", "")
USB_VID = int(vid_str, 16) if vid_str else None
USB_PID = int(pid_str, 16) if pid_str else None

# Bucket prefix (e.g. BE_ESNEUX_MONTEFIORE106_01)
BUCKET_PREFIX = cfg.get("INFLUX_BUCKET_PREFIX")
DEVICE_ID = BUCKET_PREFIX  # unique identifier reused in MQTT & payloads

# Device identity
DEVICE_NAME = DEVICE_CFG.get("device_name", BUCKET_PREFIX).replace(" ", "_")

REALTIME_BUCKET  = f"{BUCKET_PREFIX}_REALTIME"
THRESHOLD_BUCKET = f"{BUCKET_PREFIX}_THRESHOLD"

# ------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------
logger = logging.getLogger("mute_client")
logger.setLevel(logging.DEBUG if _args.debug else logging.INFO)

_sh = logging.StreamHandler()
_sh.setLevel(logging.DEBUG if _args.debug else logging.INFO)
_sh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(_sh)

# Rotating file handler: 5 files × 2 MB
_lh = RotatingFileHandler("mute_client.log", maxBytes=2_000_000, backupCount=5)
_lh.setLevel(logging.DEBUG if _args.debug else logging.INFO)
_lh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(_lh)

# ------------------------------------------------------------------
# Basic config validation
# ------------------------------------------------------------------
def validate_config():
    if not _mqtt_param("server"):
        logger.critical("MQTT server is not set (env var MQTT_SERVER or config).")
        sys.exit(4)
    if "tls" not in MQTT_CFG:
        logger.critical("MQTT TLS setting is missing (env var MQTT_TLS or config).")
        sys.exit(4)
    if serial is None and (USB_VID is None or USB_PID is None):
        logger.critical("Neither serial nor USB VID/PID provided – nothing to read.")
        sys.exit(5)
validate_config()
logger.info("Configuration validated – all mandatory settings present.")

# Timezone
TZ = pytz.timezone(TIMEZONE_CFG.get("timezone", "UTC"))
logger.info(f"Using timezone: {TZ}")

# ------------------------------------------------------------------
# SQLite offline queue
# ------------------------------------------------------------------
QUEUE_DB = "mute_queue.db"
DB_LOCK = threading.Lock()


def init_queue_db():
    with DB_LOCK, sqlite3.connect(QUEUE_DB) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS unsent_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT NOT NULL,
                payload TEXT NOT NULL,
                msg_type TEXT NOT NULL,      -- 'realtime' | 'threshold'
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
    logger.info("SQLite queue initialised.")


def queue_message(topic: str, payload: str, msg_type: str):
    ts = dt.utcnow().isoformat(timespec="seconds") + "Z"
    with DB_LOCK, sqlite3.connect(QUEUE_DB) as conn:
        conn.execute(
            "INSERT INTO unsent_messages (topic, payload, msg_type, created_at) "
            "VALUES (?,?,?,?)",
            (topic, payload, msg_type, ts),
        )
        conn.commit()
    logger.debug(f"Buffered {msg_type} → {topic} [{payload}]")


def flush_queue(client):
    """Replay queued messages (obeying age limits) after reconnect."""
    with DB_LOCK, sqlite3.connect(QUEUE_DB) as conn:
        cur = conn.execute(
            "SELECT id, topic, payload, msg_type, created_at "
            "FROM unsent_messages ORDER BY id ASC"
        )
        rows = cur.fetchall()
        for row_id, topic, payload, mtype, created in rows:
            age = dt.utcnow() - dt.fromisoformat(created.rstrip("Z"))
            limit = timedelta(hours=1) if mtype == "realtime" else timedelta(hours=48)
            if age > limit:
                conn.execute("DELETE FROM unsent_messages WHERE id=?", (row_id,))
                conn.commit()
                logger.info(f"Drop expired {mtype} #{row_id} ({age}).")
                continue
            res = client.publish(topic, payload, qos=1, retain=False)
            res.wait_for_publish()
            if res.rc == mqtt.MQTT_ERR_SUCCESS:
                conn.execute("DELETE FROM unsent_messages WHERE id=?", (row_id,))
                conn.commit()
                logger.info(f"Flushed {mtype} #{row_id} to {topic}")
            else:
                logger.warning("Broker unreachable – stop flushing.")
                break


def prune_old_messages():
    """Hourly job — delete messages older than their retention window."""
    cutoff_rt = (dt.utcnow() - timedelta(hours=1)).isoformat(timespec="seconds") + "Z"
    cutoff_thr = (dt.utcnow() - timedelta(hours=48)).isoformat(timespec="seconds") + "Z"
    with DB_LOCK, sqlite3.connect(QUEUE_DB) as conn:
        cur = conn.execute(
            "DELETE FROM unsent_messages "
            "WHERE (msg_type='realtime'  AND created_at < ?) "
            "   OR (msg_type='threshold' AND created_at < ?)",
            (cutoff_rt, cutoff_thr),
        )
        conn.commit()
    if cur.rowcount:
        logger.info(f"Pruned {cur.rowcount} expired queued message(s).")

# ------------------------------------------------------------------
# MQTT
# ------------------------------------------------------------------
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)
MQTT_CLIENT = None
MQTT_CONNECTED = False
FIRST_RT_SENT = False  # controls one-shot INFO log for first realtime point

HA_BASE = f"homeassistant/sensor/{DEVICE_NAME}"
AVAIL_TOPIC = f"{HA_BASE}/status/availability"


def mqtt_on_connect(client, userdata, flags, rc, props=None):
    global MQTT_CONNECTED
    if rc == 0:
        MQTT_CONNECTED = True
        logger.info("Connected to MQTT broker.")
        # Publish discovery topics first
        publish_ha_config(client)
        # Then signal availability
        client.publish(AVAIL_TOPIC, "online", qos=1, retain=True)
        client.loop_write()
        # Finally flush any queued state messages
        flush_queue(client)
    else:
        logger.error(f"MQTT connect failed (rc={rc}).")


def mqtt_on_disconnect(client, userdata, rc, props=None):
    global MQTT_CONNECTED
    MQTT_CONNECTED = False
    try:
        client.publish(AVAIL_TOPIC, "offline", qos=1, retain=True)
        client.loop_write()
    except Exception:
        pass
    logger.warning(f"MQTT disconnected (rc={rc}).")


def build_sensor_cfg(suffix: str, friendly: str):
    state_topic = f"{HA_BASE}/{suffix}/state"
    unique_id = f"{DEVICE_NAME}_{suffix}"
    cfg_obj = {
        "name": f"{DEVICE_NAME} {friendly}",
        "state_topic": state_topic,
        "unique_id": unique_id,
        "availability_topic": AVAIL_TOPIC,
        "payload_available": "online",
        "payload_not_available": "offline",
        "device_class": "sound_pressure",
        "unit_of_measurement": "dB",
        "device": {
            "identifiers": [f"{DEVICE_NAME}_sensor"],
            "name": f"{DEVICE_NAME} Noise Sensor",
            "model": "Mute Client",
            "manufacturer": "MUTEq",
        },
    }
    # Expose JSON attributes and extract numeric state
    cfg_obj["value_template"] = "{{ value_json.noise_level }}"
    # For realtime sensor, expose all other JSON fields as attributes
    if suffix == "realtime_noise_level":
        cfg_obj["json_attributes_topic"] = state_topic
    elif suffix == "threshold_noise_level":
        # threshold already uses attributes topic, ensure attributes
        cfg_obj["json_attributes_topic"] = state_topic
    return cfg_obj


def publish_ha_config(client):
    for suffix, name in [
        ("realtime_noise_level", "Realtime Noise Level"),
        ("threshold_noise_level", "Threshold Noise Level"),
    ]:
        cfg_json = json.dumps(build_sensor_cfg(suffix, name))
        client.publish(f"{HA_BASE}/{suffix}/config", cfg_json, retain=True)
    logger.info("Home-Assistant discovery topics published.")


def init_mqtt():
    global MQTT_CLIENT
    MQTT_CLIENT = mqtt.Client(client_id=f"MUTE_{DEVICE_NAME}", protocol=mqtt.MQTTv311, transport="tcp", userdata=None, clean_session=True)
    MQTT_CLIENT.on_connect = mqtt_on_connect
    MQTT_CLIENT.on_disconnect = mqtt_on_disconnect

    logger.info("MQTT client initialized and attempting to connect...")

    # TLS ?
    tls_enabled = _mqtt_bool("tls", True)
    if tls_enabled:
        MQTT_CLIENT.tls_set()   # default certificates

    user = _mqtt_param("user")
    pwd  = _mqtt_param("password")
    if user and pwd:
        MQTT_CLIENT.username_pw_set(user, pwd)

    MQTT_CLIENT.will_set(AVAIL_TOPIC, "offline", retain=True)

    port_value = int(_mqtt_param("port", 8883 if tls_enabled else 1883))
    MQTT_CLIENT.connect(
        _mqtt_param("server", "localhost"),
        port_value,
        keepalive=60,
    )
    # Ensure loop_start is present
    MQTT_CLIENT.loop_start()
    return MQTT_CLIENT


def send_or_queue(topic: str, payload: str):
    """Publish if possible, else queue locally."""
    msg_type = "threshold" if topic.endswith("/threshold_noise_level/state") else "realtime"
    if MQTT_CONNECTED:
        # State publishes should not be retained
        res = MQTT_CLIENT.publish(topic, payload, qos=1, retain=False)
        res.wait_for_publish()
        if res.rc == mqtt.MQTT_ERR_SUCCESS:
            # Decide logging level
            if msg_type == "threshold":
                logger.info(f"THRESHOLD {payload}")
            elif msg_type == "realtime":
                if _args.debug:
                    logger.info(f"realtime {payload}")
                else:
                    global FIRST_RT_SENT
                    if not FIRST_RT_SENT:
                        logger.info(f"Test MQTT publish: topic={topic}, payload={payload}")
                        logger.info(f"First realtime measurement ({json.loads(payload).get('noise_level')} dB) successfully published to MQTT.")
                        FIRST_RT_SENT = True
                    else:
                        logger.debug(f"realtime {payload}")
            return
    queue_message(topic, payload, msg_type)

# ------------------------------------------------------------------
# Periodic availability keep‑alive
# ------------------------------------------------------------------
def publish_alive():
    if MQTT_CONNECTED:
        MQTT_CLIENT.publish(AVAIL_TOPIC, "online", qos=1, retain=True)

def alive_loop():
    while True:
        time.sleep(60)
        publish_alive()

# ------------------------------------------------------------------
# Device detection
# ------------------------------------------------------------------
def detect_serial_device():
    if not serial or not SERIAL_CFG.get("enabled", False):
        return None
    port = SERIAL_CFG.get("port", "/dev/ttyUSB0")
    baud = SERIAL_CFG.get("baudrate", 115200)
    try:
        s = serial.Serial(port=port, baudrate=baud, timeout=1)
        logger.info(f"Serial device detected on {port} @ {baud} baud.")
        return s
    except Exception as e:
        logger.error(f"Serial error: {e}")
    return None


def detect_usb_device():
    devs = usb.core.find(find_all=True)
    for d in devs:
        if USB_VID and USB_PID and (d.idVendor, d.idProduct) != (USB_VID, USB_PID):
            continue
        logger.info(
            f"USB SPL meter found (VID=0x{d.idVendor:x} PID=0x{d.idProduct:x})"
        )
        return d
    logger.error("No USB SPL meter detected.")
    return None

# ------------------------------------------------------------------
# Acquisition loop
# ------------------------------------------------------------------
LAST_RT_TS = time.time()

def noise_monitor_loop():
    ser_dev = detect_serial_device()
    usb_dev = None if ser_dev else detect_usb_device()
    if not (ser_dev or usb_dev):
        logger.critical("No sound level meter found – exiting.")
        os._exit(3)
    if usb_dev:
        try:
            usb_dev.set_configuration()
            usb.util.claim_interface(usb_dev, 0)
        except Exception as e:
            logger.debug(f"USB configuration/claim failed: {e}")
        try:
            if usb_dev.is_kernel_driver_active(0):
                usb_dev.detach_kernel_driver(0)
                logger.info("Detached kernel driver from USB device.")
        except Exception as e:
            logger.debug(f"Kernel driver detach not needed or failed: {e}")
        try:
            ret_test = usb_dev.ctrl_transfer(0xC0, 4, 0, 0, 200)
            logger.info(f"Initial USB raw bytes: {list(ret_test)}")
            initial_db = (ret_test[0] + ((ret_test[1] & 3) << 8)) * 0.1 + 30
            logger.info(f"Initial computed dB: {round(initial_db,1)}")
        except Exception as e:
            logger.error(f"Initial USB transfer failed: {e}")

    window_start = time.time() - TIME_WINDOW
    current_peak = 0.0

    while True:
        # 1) Sample as fast as possible
        try:
            if ser_dev:
                line = ser_dev.readline().decode().strip()
                if line:
                    dB = float(line)
            else:
                ret = usb_dev.ctrl_transfer(0xC0, 4, 0, 0, 200)
                dB = (ret[0] + ((ret[1] & 3) << 8)) * 0.1 + 30
            if _args.debug:
                logger.debug(f"Sample dB reading: {dB}")
            if dB > current_peak:
                current_peak = dB
        except usb.core.USBError as ue:
            logger.error(f"USB read error: {ue} – retrying.")
            time.sleep(1)
            usb_dev = detect_usb_device()
        except Exception as e:
            logger.error(f"Acquisition error: {e}")
            time.sleep(1)

        # 2) Heartbeat
        if _args.debug:
            logger.debug("noise_monitor_loop heartbeat")

        # 3) Time-window check
        now = time.time()
        if now - window_start >= TIME_WINDOW:
            now_utc = dt.utcnow().replace(tzinfo=pytz.UTC)
            ts_local = now_utc.astimezone(TZ)
            ts_iso = ts_local.isoformat(timespec="seconds")
            ts_utc = now_utc.isoformat(timespec="seconds").replace("+00:00", "Z")

            rt_payload = json.dumps({
                "noise_level": round(current_peak, 1),
                "timestamp": ts_iso,
                "_time": ts_utc,
                "device_id": DEVICE_ID,
            })
            if _args.debug:
                logger.debug(f"Realtime reading: {rt_payload}")
            send_or_queue(f"{HA_BASE}/realtime_noise_level/state", rt_payload)
            global LAST_RT_TS
            LAST_RT_TS = time.time()

            if current_peak >= MIN_NOISE:
                thr_payload = json.dumps({
                    "noise_level": round(current_peak, 1),
                    "timestamp": ts_iso,
                    "_time": ts_utc,
                    "address": MAP_CFG.get("address", ""),
                    "device_id": DEVICE_ID,
                })
                if _args.debug:
                    logger.debug(f"Threshold reading: {thr_payload}")
                send_or_queue(f"{HA_BASE}/threshold_noise_level/state", thr_payload)
                logger.warning(f"Threshold event detected: {round(current_peak,1)} dB at {ts_iso}")

            current_peak = 0.0
            window_start = now

        time.sleep(0.05)

# ------------------------------------------------------------------
# House-keeping threads
# ------------------------------------------------------------------
def prune_loop():
    while True:
        time.sleep(3600)  # hourly
        prune_old_messages()

def watchdog_loop():
    """
    Restarts the whole process if no realtime measurement has been sent
    within 10 s.  Uses an exec‑style replacement so that the PID stays the
    same for systemd / supervisord while the Python interpreter and module
    state are fully reset.
    """
    while True:
        time.sleep(5)
        if time.time() - LAST_RT_TS > 10:
            logger.critical("Watchdog: no realtime data for >10 s – restarting process.")
            try:
                if MQTT_CLIENT:
                    MQTT_CLIENT.publish(AVAIL_TOPIC, "offline", retain=True)
                    MQTT_CLIENT.disconnect()
                    MQTT_CLIENT.loop_stop()
            except Exception:
                pass
            # Re‑exec the current script with the absolute path and same arguments
            script_path = os.path.abspath(sys.argv[0])
            os.execv(sys.executable, [sys.executable, script_path] + sys.argv[1:])

# ------------------------------------------------------------------
# Signal handling
# ------------------------------------------------------------------
def signal_handler(sig, frame):
    logger.info("Signal received – shutting down.")
    if MQTT_CLIENT:
        MQTT_CLIENT.publish(AVAIL_TOPIC, "offline", retain=True)
        MQTT_CLIENT.disconnect()
    sys.exit(0)

# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
def main():
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    init_queue_db()
    init_mqtt()

    threading.Thread(target=noise_monitor_loop, daemon=True).start()
    threading.Thread(target=prune_loop, daemon=True).start()
    threading.Thread(target=watchdog_loop, daemon=True).start()

    logger.info("Mute Client started – Ctrl-C to quit.")
    # Schedule periodic availability keep-alive every 60s
    threading.Thread(target=alive_loop, daemon=True).start()
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
