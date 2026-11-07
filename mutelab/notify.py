"""ntfy push notifications (https://ntfy.sh or self-hosted), sent from a background thread."""

import json
import queue
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, List, Optional
from urllib.parse import urlsplit

PRIORITY = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}


def split_topic_url(url: str):
    """'https://ntfy.sh/my-topic' -> ('https://ntfy.sh', 'my-topic')."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("MUTE_NTFY_URL must look like https://ntfy.sh/your-topic")
    path = parts.path.strip("/")
    if not path:
        raise ValueError("MUTE_NTFY_URL is missing the topic, e.g. https://ntfy.sh/your-topic")
    base_path, _, topic = path.rpartition("/")
    server = f"{parts.scheme}://{parts.netloc}"
    if base_path:
        server += f"/{base_path}"
    return server, topic


class Notifier:
    """
    Fire-and-forget notifier. `send()` never blocks the measurement loop.
    `is_enabled(kind)` lets the caller filter by the user's notification preferences.
    """

    def __init__(self, url: str, token: str = "", click_url: str = "", logger=None,
                 is_enabled: Optional[Callable[[str], bool]] = None, transport=None):
        self.logger = logger
        self.click_url = click_url
        self.token = token
        self._is_enabled = is_enabled or (lambda kind: True)
        self._transport = transport or self._http_post
        self.server = self.topic = None
        self.error: Optional[str] = None
        if url:
            try:
                self.server, self.topic = split_topic_url(url)
            except ValueError as exc:
                self.error = str(exc)
                self._log("warning", f"[NTFY] {exc}")
        self._queue: "queue.Queue[dict]" = queue.Queue(maxsize=100)
        self._cooldowns: Dict[str, float] = {}
        self._stop = threading.Event()
        self.sent: List[dict] = []  # last messages, for the dashboard
        self.last_error: Optional[str] = None
        self._thread = threading.Thread(target=self._run, name="mutelab-ntfy", daemon=True)
        if self.configured:
            self._thread.start()

    @property
    def configured(self) -> bool:
        return bool(self.server and self.topic)

    def _log(self, level: str, msg: str):
        if self.logger:
            getattr(self.logger, level)(msg)

    def is_enabled(self, kind: str) -> bool:
        return self.configured and self._is_enabled(kind)

    def send(self, kind: str, title: str, message: str, priority: str = "default", tags: Optional[List[str]] = None,
             cooldown_key: Optional[str] = None, cooldown_s: float = 0.0, force: bool = False) -> bool:
        if not self.configured:
            return False
        if not force and not self._is_enabled(kind):
            return False
        now = time.time()
        if cooldown_key and cooldown_s > 0:
            last = self._cooldowns.get(cooldown_key, 0.0)
            if now - last < cooldown_s:
                return False
            self._cooldowns[cooldown_key] = now
        payload = {
            "topic": self.topic,
            "title": title,
            "message": message,
            "priority": PRIORITY.get(priority, 3),
            "tags": tags or [],
        }
        if self.click_url:
            payload["click"] = self.click_url
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            self._log("warning", "[NTFY] queue full, dropping notification")
            return False
        return True

    def _http_post(self, payload: dict):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.server, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status >= 300:
                raise RuntimeError(f"ntfy returned HTTP {resp.status}")

    def _run(self):
        while not self._stop.is_set():
            try:
                payload = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._deliver(payload)
            finally:
                self._queue.task_done()

    def _deliver(self, payload: dict):
        for attempt in range(3):
            try:
                self._transport(payload)
                self.last_error = None
                self.sent.append({"t": time.time(), "title": payload["title"], "message": payload["message"]})
                del self.sent[:-20]
                self._log("info", f"[NTFY] sent: {payload['title']}")
                return
            except Exception as exc:  # network errors must never kill the thread
                self.last_error = str(exc)
                self._log("warning", f"[NTFY] send failed (attempt {attempt + 1}/3): {exc}")
                if self._stop.wait(2 ** attempt):
                    return

    def flush(self, timeout: float = 5.0):
        deadline = time.time() + timeout
        while self._queue.unfinished_tasks and time.time() < deadline:
            time.sleep(0.02)

    def stop(self):
        self._stop.set()
