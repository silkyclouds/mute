import json
import os
from copy import deepcopy
from typing import Any, Dict, Tuple

DEFAULT_CONFIG: Dict[str, Any] = {
    "config_version": 2,
    "device_name": "MUTEq Sensor",
    "assigned_device_id": None,
    "device_token": None,
    "location": {
        "address": "",
        "lat": None,
        "lon": None,
        "country": ""
    },
    "environment_profile": "traffic_roadside",
    "custom_environment_label": "",
    "backend_preference_index": 0,
    "backend_failover": [
        "https://dash.muteq.eu"
    ],
    "usb_override": {
        "vendor_id": None,
        "product_id": None
    },
    "mqtt_enabled": False,
    "mqtt_server": "",
    "mqtt_port": 1883,
    "mqtt_user": "",
    "mqtt_pass": "",
    "mqtt_tls": False,
    "log_level": "INFO"
}


def sanitize_device_name(name: str) -> str:
    """Return a sanitized, bounded device name."""
    clean = (name or "MUTEq Sensor").strip()
    if not clean:
        clean = "MUTEq Sensor"
    return clean[:64]


def merge_defaults(cfg: Dict[str, Any]) -> Dict[str, Any]:
    merged = deepcopy(DEFAULT_CONFIG)
    for key, value in cfg.items():
        if isinstance(value, dict) and key in merged and isinstance(merged[key], dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


def load_config(path: str, logger) -> Tuple[Dict[str, Any], bool]:
    """
    Load configuration from disk.
    Returns (config, needs_registration).
    """
    cfg: Dict[str, Any]
    needs_registration = False
    if not os.path.exists(path):
        logger.warning(f"Config file not found at {path}; using defaults and triggering registration.")
        cfg = deepcopy(DEFAULT_CONFIG)
        return cfg, True
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        logger.error("Failed to read or parse config file; falling back to defaults and re-registering.")
        cfg = deepcopy(DEFAULT_CONFIG)
        needs_registration = True
    cfg = merge_defaults(cfg)
    if not cfg.get("assigned_device_id") or not cfg.get("device_token"):
        needs_registration = True
    cfg["device_name"] = sanitize_device_name(cfg.get("device_name"))
    return cfg, needs_registration


def persist_config(path: str, cfg: Dict[str, Any], logger) -> None:
    """Persist configuration to disk."""
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        logger.info(f"[CONFIG] Saved config to {path}")
    except Exception as exc:
        logger.error(f"Failed to save config to {path}: {exc}")


def validate_config(cfg: Dict[str, Any], logger) -> Dict[str, Any]:
    """
    Apply minimal validation and defaults.
    Does not raise; returns a sanitized config dict.
    """
    cfg = merge_defaults(cfg)
    cfg["device_name"] = sanitize_device_name(cfg.get("device_name"))
    if not isinstance(cfg.get("mqtt_port"), int):
        try:
            cfg["mqtt_port"] = int(cfg.get("mqtt_port", 1883))
        except Exception:
            cfg["mqtt_port"] = 1883
    if not isinstance(cfg.get("backend_preference_index"), int):
        cfg["backend_preference_index"] = 0
    if not isinstance(cfg.get("backend_failover"), list):
        cfg["backend_failover"] = deepcopy(DEFAULT_CONFIG["backend_failover"])
    else:
        cleaned_backend = []
        for url in cfg.get("backend_failover", []):
            val = str(url).strip().rstrip("/")
            if val:
                cleaned_backend.append(val)
        if cleaned_backend:
            cfg["backend_failover"] = cleaned_backend
        else:
            cfg["backend_failover"] = deepcopy(DEFAULT_CONFIG["backend_failover"])
    if cfg.get("assigned_device_id"):
        cfg["assigned_device_id"] = str(cfg["assigned_device_id"]).strip()
    if cfg.get("device_token"):
        cfg["device_token"] = str(cfg["device_token"]).strip()
    return cfg


def build_backend_pool(cfg: Dict[str, Any]) -> list:
    """
    Build an ordered list of backend base URLs from config and env.
    """
    env_urls = os.environ.get("MUTE_BACKEND_URLS")
    if env_urls:
        pool = [u.strip() for u in env_urls.split(",") if u.strip()]
    else:
        pool = list(cfg.get("backend_failover") or [])
    cleaned = []
    for url in pool:
        val = url.rstrip("/")
        if val:
            cleaned.append(val)
    if not cleaned:
        cleaned = deepcopy(DEFAULT_CONFIG["backend_failover"])
    return cleaned
