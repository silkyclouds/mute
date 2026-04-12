import json
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Deque, Dict, List, Optional, Tuple
import os
import hashlib

import requests

from config_loader import DEFAULT_CONFIG
from security import build_ingest_signature, build_registration_signature
from client_version import CLIENT_VERSION

API_TIMEOUT_SECONDS = 5
HEARTBEAT_SECONDS = 30
TIME_WINDOW_SECONDS = 2.0
MINIMUM_NOISE_LEVEL = 80.0
BASE_API_URL = "https://api.muteq.eu"
REALTIME_TIMEOUT_SECONDS = float(os.environ.get("MUTE_REALTIME_TIMEOUT_S", "1.0"))
REALTIME_KEEPALIVE = os.environ.get("MUTE_REALTIME_KEEPALIVE", "true").lower() in ("1", "true", "yes", "on")
HEARTBEAT_RETRY_MAX_AGE_SECONDS = int(os.environ.get("MUTE_HEARTBEAT_RETRY_MAX_AGE_S", "180"))
GENERIC_RETRY_MAX_AGE_SECONDS = int(os.environ.get("MUTE_RETRY_MAX_AGE_S", "86400"))
INGEST_ENDPOINTS = {
    "realtime": "/v2/devices/{device_id}/ingest/realtime_batch",
    "threshold": "/v2/devices/{device_id}/ingest/threshold",
    "heartbeat": "/v2/devices/{device_id}/ingest/heartbeat",
    "event": "/v2/devices/{device_id}/ingest/event",
    "event_batch": "/v2/devices/{device_id}/ingest/events_batch"
}


class RetryQueue:
    """Thread-safe retry queue."""

    def __init__(self):
        self._queue: Deque[Tuple[str, str, dict, float]] = deque()
        self._lock = threading.Lock()

    def put(self, item: Tuple[str, str, dict, float]) -> None:
        with self._lock:
            self._queue.append(item)

    def pop(self) -> Optional[Tuple[str, str, dict, float]]:
        with self._lock:
            if self._queue:
                return self._queue.popleft()
        return None

    def drop_oldest(self) -> bool:
        with self._lock:
            if self._queue:
                self._queue.popleft()
                return True
        return False

    def __len__(self) -> int:
        with self._lock:
            return len(self._queue)


class RetryWorker(threading.Thread):
    """Background worker that flushes the retry queue with backoff."""

    def __init__(self, client, queue: RetryQueue, stop_event: threading.Event, logger):
        super().__init__(daemon=True)
        self.client = client
        self.queue = queue
        self.stop_event = stop_event
        self.logger = logger
        self.delay = 2

    def run(self):
        while not self.stop_event.is_set():
            item = self.queue.pop()
            if item is None:
                time.sleep(1)
                continue
            path, event_type, payload, enqueued_at = item
            max_age = HEARTBEAT_RETRY_MAX_AGE_SECONDS if event_type == "heartbeat" else GENERIC_RETRY_MAX_AGE_SECONDS
            age_seconds = max(0.0, time.time() - enqueued_at)
            if max_age > 0 and age_seconds > max_age:
                self.logger.warning(
                    f"[RETRY] Dropping stale queued payload: type={event_type} age_s={age_seconds:.1f} max_age_s={max_age}"
                )
                self.delay = 2
                continue

            ts_retry = datetime.now(timezone.utc).isoformat()
            headers = self.client._build_headers(ts_retry)
            if not headers:
                self.logger.warning(f"[RETRY] Missing credentials while retrying {event_type}; dropping queued payload.")
                self.delay = 2
                continue

            success, non_retryable = self.client._post(path, payload, headers)
            if success:
                self.delay = 2
                continue
            if non_retryable:
                self.logger.warning(f"[RETRY] Non-retryable response for {event_type}; dropping queued payload.")
                self.delay = 2
                continue
            self.queue.put(item)
            time.sleep(self.delay)
            self.delay = min(self.delay * 2, 60)


class BackendClient:
    """HTTP client handling registration and ingestion with retry support."""

    def __init__(self, secret: str, backend_pool: List[str], logger, backend_preference_index: int = 0):
        self.secret = secret
        # Single, fixed backend base URL
        self.base_url = BASE_API_URL
        self.logger = logger
        # Use a shared Session for keep-alive; can be disabled via env.
        self.session = requests.Session() if REALTIME_KEEPALIVE else requests
        self.retry_queue = RetryQueue()
        self.stop_event = threading.Event()
        self.worker = RetryWorker(self, self.retry_queue, self.stop_event, logger)
        self.device_id: Optional[str] = None
        self.device_token: Optional[str] = None
        self.backend_preference_index = 0
        self._last_realtime_post = 0.0
        self._last_realtime_window: Optional[int] = None
        self.logger.info(f"[INFO] Using backend endpoint: {self.base_url}")

    def start(self):
        self.worker.start()

    def stop(self):
        self.stop_event.set()
        self.worker.join(timeout=2)

    def set_credentials(self, device_id: str, device_token: str, preference_index: int = 0):
        self.device_id = device_id
        self.device_token = device_token
        self.backend_preference_index = preference_index or 0

    def _backend_candidates(self) -> List[str]:
        return [self.base_url]

    def _post(self, path: str, payload: dict, headers: dict, *, timeout: float = API_TIMEOUT_SECONDS) -> Tuple[bool, bool]:
        for base in self._backend_candidates():
            url = f"{base}{path}"
            try:
                resp = self.session.post(url, json=payload, headers=headers, timeout=timeout)
                self.logger.info(f"[INGEST] POST {url} -> {resp.status_code}")
                if 200 <= resp.status_code < 300:
                    return True, False
                if resp.status_code in (401, 403):
                    # Invalid token/signature are not recoverable by retrying the same payload.
                    return False, True
            except requests.RequestException as exc:
                self.logger.warning(f"HTTP request failed for {url}: {exc}")
        return False, False

    def fetch_device_config(self, device_id: str) -> Optional[dict]:
        """
        Fetch device configuration from backend (sampling/emission timing source of truth).
        Returns None if device not found (404) or other error.
        """
        if not device_id:
            return None
        headers = {}
        if self.device_token:
            headers["Authorization"] = f"Bearer {self.device_token}"
        for base in self._backend_candidates():
            url = f"{base}/v2/devices/{device_id}"
            try:
                resp = self.session.get(url, headers=headers, timeout=API_TIMEOUT_SECONDS)
                self.logger.debug(f"[CONFIG] GET {url} -> {resp.status_code}")
                if 200 <= resp.status_code < 300:
                    try:
                        return resp.json()
                    except ValueError:
                        return None
                elif resp.status_code == 403:
                    # Forbidden - authentication/authorization issue
                    self.logger.warning(f"[CONFIG] Device {device_id} access forbidden (403) - authentication issue")
                    return {"_forbidden": True}
                elif resp.status_code == 404:
                    # Device not found - return special marker
                    self.logger.warning(f"[CONFIG] Device {device_id} not found in backend (404)")
                    return {"_not_found": True}
            except requests.RequestException as exc:
                self.logger.warning(f"[CONFIG] request failed for {url}: {exc}")
        return None

    def register_device(self, device_name: str, env_profile: str, custom_label: str, client_version: str) -> Dict:
        payload = {
            "device_name": device_name,
            "environment_profile": env_profile,
            "custom_environment_label": custom_label,
            "client_version": client_version
        }
        for base in self._backend_candidates():
            nonce = secrets.token_hex(16)
            headers = {
                "X-MUTE-REGISTER": "1",
                "X-MUTE-NONCE": nonce,
                "X-MUTE-SIGNATURE": build_registration_signature(self.secret, device_name, nonce)
            }
            url = f"{base}/v2/devices/register"
            if "/v2/devices/register" not in url:
                raise RuntimeError("Registration URL must use V2 enrollment API")
            self.logger.info("Registering device using V2 enrollment API")
            self.logger.info(f"Client version: {client_version}")
            self.logger.info(f"POST {url}")
            try:
                resp = self.session.post(url, json=payload, headers=headers, timeout=API_TIMEOUT_SECONDS)
                parsed = None
                try:
                    parsed = resp.json()
                except ValueError:
                    parsed = resp.text
                self.logger.info(f"[REGISTER] Response from {url}: {resp.status_code} body={parsed}")
                if resp.status_code in (200, 201):
                    if isinstance(parsed, dict):
                        # Frontend onboarding handled by dash.muteq.eu
                        device_id_val = parsed.get("device_id")
                        onboarding_url = None
                        if device_id_val:
                            onboarding_url = f"https://dash.muteq.eu/devices/{device_id_val}/claim"
                            self.logger.info(f"Onboarding URL (frontend): {onboarding_url}")
                        return parsed
                    try:
                        data = resp.json()
                        return data
                    except ValueError:
                        self.logger.warning(f"Registration succeeded with {resp.status_code} but response not JSON at {url}")
                else:
                    # All non-2xx are fatal; no legacy fallback.
                    raise RuntimeError(f"V2 registration failed: {resp.status_code} {parsed}")
            except requests.RequestException as exc:
                raise RuntimeError(f"V2 registration request failed for {url}: {exc}")
        raise RuntimeError("V2 registration could not be completed")

    def _build_headers(self, timestamp_iso: str) -> dict:
        if not self.device_id or not self.device_token:
            return {}
        sig = build_ingest_signature(self.secret, self.device_id, timestamp_iso)
        return {
            "Authorization": f"Bearer {self.device_token}",
            "X-MUTE-TIMESTAMP": timestamp_iso,
            "X-MUTE-SIGNATURE": sig
        }

    def send_payload(self, event_type: str, payload: dict, timestamp_iso: str, *, window_id: Optional[int] = None) -> bool:
        if not self.device_id:
            self.logger.error("Missing device_id; cannot build V2 ingest URL.")
            return False
        payload = dict(payload or {})
        payload["device_id"] = self.device_id
        path_template = INGEST_ENDPOINTS.get(event_type)
        if not path_template:
            self.logger.error(f"Unknown event_type '{event_type}'")
            return False
        path = path_template.format(device_id=self.device_id)
        headers = self._build_headers(timestamp_iso)
        if not headers:
            self.logger.error("Missing device credentials; cannot send payload.")
            return False
        # Realtime batches are sent at a controlled cadence by caller; no internal rate limit needed here.
        enqueue_flag = False if event_type == "realtime" else True
        timeout_val = REALTIME_TIMEOUT_SECONDS if event_type == "realtime" else API_TIMEOUT_SECONDS
        sent, non_retryable = self._post(path, payload, headers, timeout=timeout_val)
        if (not sent) and enqueue_flag and (not non_retryable):
            self.enqueue(event_type, payload, timestamp_iso)
        return sent

    def enqueue(self, event_type: str, payload: dict, timestamp_iso: str, *, maxlen: Optional[int] = None):
        if not self.device_id:
            return
        path_template = INGEST_ENDPOINTS.get(event_type)
        if not path_template:
            return
        path = path_template.format(device_id=self.device_id)
        if not path:
            return
        payload = dict(payload or {})
        payload["device_id"] = self.device_id
        if maxlen is not None and len(self.retry_queue) >= maxlen:
            if self.retry_queue.drop_oldest():
                self.logger.warning(f"[INGEST_BACKLOG] Dropping oldest queued payload: type={event_type} device={self.device_id} maxlen={maxlen}")
        # Keep only payload + metadata in queue. Signatures are regenerated at retry time.
        _ = timestamp_iso
        self.retry_queue.put((path, event_type, payload, time.time()))


# Delayed import to avoid circular dependency with secrets
import secrets  # noqa: E402
