import json
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Deque, Dict, List, Optional, Tuple

import requests

from config_loader import DEFAULT_CONFIG
from security import build_ingest_signature, build_registration_signature

API_TIMEOUT_SECONDS = 5
HEARTBEAT_SECONDS = 90
TIME_WINDOW_SECONDS = 2.0
MINIMUM_NOISE_LEVEL = 80.0
INGEST_ENDPOINTS = {
    "realtime": "/api/ingest/realtime",
    "threshold": "/api/ingest/event",
    "heartbeat": "/api/ingest/heartbeat"
}


class RetryQueue:
    """Thread-safe retry queue."""

    def __init__(self):
        self._queue: Deque[Tuple[str, dict, dict]] = deque()
        self._lock = threading.Lock()

    def put(self, item: Tuple[str, dict, dict]) -> None:
        with self._lock:
            self._queue.append(item)

    def pop(self) -> Optional[Tuple[str, dict, dict]]:
        with self._lock:
            if self._queue:
                return self._queue.popleft()
        return None

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
            path, payload, headers = item
            success = self.client._post(path, payload, headers, enqueue_on_fail=False)
            if success:
                self.delay = 2
                continue
            self.queue.put(item)
            time.sleep(self.delay)
            self.delay = min(self.delay * 2, 60)


class BackendClient:
    """HTTP client handling registration and ingestion with retry support."""

    def __init__(self, secret: str, backend_pool: List[str], logger, backend_preference_index: int = 0):
        self.secret = secret
        pool = list(backend_pool or [])
        if not pool:
            pool = list(DEFAULT_CONFIG.get("backend_failover", []))
        self.backend_pool = pool
        self.logger = logger
        self.session = requests.Session()
        self.retry_queue = RetryQueue()
        self.stop_event = threading.Event()
        self.worker = RetryWorker(self, self.retry_queue, self.stop_event, logger)
        self.device_id: Optional[str] = None
        self.device_token: Optional[str] = None
        self.backend_preference_index = backend_preference_index or 0
        self.logger.info(f"[INFO] Using backend endpoints: {', '.join(self.backend_pool)}")

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
        if not self.backend_pool:
            return []
        rotated = self.backend_pool[self.backend_preference_index :] + self.backend_pool[: self.backend_preference_index]
        return rotated

    def _post(self, path: str, payload: dict, headers: dict, enqueue_on_fail: bool = True) -> bool:
        for base in self._backend_candidates():
            url = f"{base}{path}"
            try:
                resp = self.session.post(url, json=payload, headers=headers, timeout=API_TIMEOUT_SECONDS)
                self.logger.info(f"[INGEST] POST {path} -> {resp.status_code}")
                if 200 <= resp.status_code < 300:
                    return True
                if resp.status_code in (401, 403):
                    return False
            except requests.RequestException as exc:
                self.logger.warning(f"HTTP request failed for {url}: {exc}")
        if enqueue_on_fail:
            self.retry_queue.put((path, payload, headers))
        return False

    def register_device(self, device_name: str, env_profile: str, custom_label: str, client_version: str) -> Tuple[Optional[str], Optional[str]]:
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
            for endpoint in ("/api/register", "/api/client/register"):
                url = f"{base}{endpoint}"
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
                            return parsed.get("device_id"), parsed.get("device_token")
                        try:
                            data = resp.json()
                            return data.get("device_id"), data.get("device_token")
                        except ValueError:
                            self.logger.warning(f"Registration succeeded with {resp.status_code} but response not JSON at {url}")
                    else:
                        self.logger.warning(f"Registration failed at {url} status={resp.status_code} body={parsed}")
                except requests.RequestException as exc:
                    self.logger.warning(f"Registration request failed for {url}: {exc}")
        return None, None

    def _build_headers(self, timestamp_iso: str) -> dict:
        if not self.device_id or not self.device_token:
            return {}
        sig = build_ingest_signature(self.secret, self.device_id, timestamp_iso)
        return {
            "Authorization": f"Bearer {self.device_token}",
            "X-MUTE-TIMESTAMP": timestamp_iso,
            "X-MUTE-SIGNATURE": sig
        }

    def send_payload(self, event_type: str, payload: dict, timestamp_iso: str) -> bool:
        headers = self._build_headers(timestamp_iso)
        if not headers:
            self.logger.error("Missing device credentials; cannot send payload.")
            return False
        path = INGEST_ENDPOINTS.get(event_type)
        if not path:
            self.logger.error(f"Unknown event_type '{event_type}'")
            return False
        return self._post(path, payload, headers, enqueue_on_fail=True)

    def enqueue(self, event_type: str, payload: dict, timestamp_iso: str):
        headers = self._build_headers(timestamp_iso)
        path = INGEST_ENDPOINTS.get(event_type)
        if not headers or not path:
            return
        self.retry_queue.put((path, payload, headers))


# Delayed import to avoid circular dependency with secrets
import secrets  # noqa: E402
