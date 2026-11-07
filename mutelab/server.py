"""Dashboard HTTP server: static UI, JSON API and a Server-Sent Events live stream (stdlib only)."""

import csv
import io
import json
import os
import queue
import re
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlsplit

from .labtests import PROGRAMS, render_wav
from .settings import DEFAULT_SETTINGS, notify_kinds

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
MAX_BODY = 64 * 1024

MANIFEST = {
    "name": "Mute Lab",
    "short_name": "Mute",
    "start_url": "./",
    "display": "standalone",
    "background_color": "#0b1014",
    "theme_color": "#0b1014",
    "icons": [{"src": "icon.svg", "sizes": "any", "type": "image/svg+xml"}],
}

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="14" fill="#0b1014"/>
<path d="M12 40 L20 40 L24 24 L30 48 L36 16 L42 44 L46 34 L52 34" fill="none" stroke="#5b9a9a"
 stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/></svg>"""


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _float_param(qs, name, default, lo=None, hi=None):
    try:
        v = float(qs.get(name, [default])[0])
    except (TypeError, ValueError):
        raise ApiError(400, f"invalid {name}")
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def bucket_for_hours(hours: float) -> int:
    if hours <= 6:
        return 60
    if hours <= 24:
        return 300
    if hours <= 24 * 7:
        return 600
    if hours <= 24 * 45:
        return 3600
    return 86400


def ha_yaml(hub) -> str:
    dev = getattr(hub.mqtt, "device_id", None) or "<device_id>"
    slug = re.sub(r"[^a-z0-9_]+", "_", (hub.device_name or "mute box").lower()).strip("_")
    url = hub.env.public_url or "http://<mute-box-ip>:%d" % hub.env.port
    return f"""# Home Assistant — Mute Lab
# Sensors are created automatically through MQTT discovery (device: "{hub.device_name}", id {dev}).
# 1) Dashboard card: live view on a wall tablet (Settings > Dashboards > add a "Webpage" card)
type: iframe
url: {url}/?kiosk=1
aspect_ratio: 60%

# 2) Automation example: flash a light while a night-time noise episode is running
# automation:
#   - alias: Night noise episode
#     trigger:
#       - platform: state
#         entity_id: binary_sensor.{slug}_noise_episode
#         to: "on"
#     condition:
#       - condition: time
#         after: "22:00:00"
#         before: "07:00:00"
#     action:
#       - service: notify.mobile_app_your_phone
#         data:
#           message: "Noise episode: {{{{ states('sensor.{slug}_laeq_1_min') }}}} dB"
"""


def make_handler(hub, settings, env):
    token = env.token

    class Handler(BaseHTTPRequestHandler):
        server_version = "MuteLab/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # keep the client logs clean
            return

        # ------------------------------------------------------------ helpers
        def _send(self, status: int, body: bytes, content_type: str, extra_headers: Optional[dict] = None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, data, status: int = 200):
            self._send(status, json.dumps(data, separators=(",", ":")).encode("utf-8"), "application/json")

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ApiError(413, "body too large")
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                raise ApiError(400, "invalid JSON")
            if not isinstance(data, dict):
                raise ApiError(400, "JSON object expected")
            return data

        def _authorized(self, qs) -> bool:
            if not token:
                return True
            supplied = self.headers.get("X-Mute-Token") or (qs.get("token") or [""])[0]
            return supplied == token

        # ------------------------------------------------------------- routing
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method: str):
            parts = urlsplit(self.path)
            path = parts.path.rstrip("/") or "/"
            qs = parse_qs(parts.query)
            try:
                if method == "GET" and path in ("/", "/index.html"):
                    return self._static("index.html", "text/html; charset=utf-8")
                if method == "GET" and path == "/manifest.webmanifest":
                    return self._send(200, json.dumps(MANIFEST).encode(), "application/manifest+json")
                if method == "GET" and path == "/icon.svg":
                    return self._send(200, ICON_SVG.encode(), "image/svg+xml")
                if not path.startswith("/api/"):
                    raise ApiError(404, "not found")
                if not self._authorized(qs):
                    raise ApiError(401, "token required")
                route = getattr(self, "_" + method.lower() + "_" + self._route_name(path), None)
                if route is None:
                    raise ApiError(404, "unknown endpoint")
                return route(path, qs)
            except ApiError as exc:
                self._json({"error": exc.message}, exc.status)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:  # pragma: no cover - defensive
                try:
                    self._json({"error": f"internal error: {exc}"}, 500)
                except Exception:
                    pass

        @staticmethod
        def _route_name(path: str) -> str:
            segs = path[len("/api/"):].split("/")
            norm = []
            for s in segs:
                if re.fullmatch(r"[0-9a-f]{6,}|\d+", s):
                    norm.append("id")
                elif s.endswith(".wav"):
                    norm.append("file")
                else:
                    norm.append(s.replace(".", "_").replace("-", "_"))
            return "_".join(norm)

        def _static(self, name: str, ctype: str):
            with open(os.path.join(WEB_DIR, name), "rb") as fh:
                body = fh.read()
            self._send(200, body, ctype)

        # ------------------------------------------------------------------ GET
        def _get_status(self, path, qs):
            snap = hub.snapshot()
            snap["info"] = info()
            self._json(snap)

        def _get_live(self, path, qs):
            seconds = int(_float_param(qs, "seconds", 900, 10, 3600))
            self._json({"seconds": hub.live_series(seconds)})

        def _get_history(self, path, qs):
            hours = _float_param(qs, "hours", 24, 1, 24 * 400)
            end = time.time()
            bucket = bucket_for_hours(hours)
            self._json({"bucket": bucket, "series": hub.store.series(end - hours * 3600, end, bucket)})

        def _get_days(self, path, qs):
            n = int(_float_param(qs, "n", 14, 1, 120))
            self._json({"days": hub.store.daily(n), "trend": hub.trend()})

        def _get_events(self, path, qs):
            hours = _float_param(qs, "hours", 24 * 7, 1, 24 * 400)
            kind = (qs.get("kind") or [None])[0]
            limit = int(_float_param(qs, "limit", 100, 1, 1000))
            self._json({"events": hub.store.events(time.time() - hours * 3600, None, kind=kind, limit=limit)})

        def _get_events_id(self, path, qs):
            item = hub.store.event(int(path.rsplit("/", 1)[-1]))
            if not item:
                raise ApiError(404, "event not found")
            self._json(item)

        def _get_export_csv(self, path, qs):
            days = _float_param(qs, "days", 7, 1, 400)
            end = time.time()
            out = io.StringIO()
            w = csv.writer(out)
            w.writerow(["local_time", "unix_ts", "seconds", "LAeq", "Lmin", "Lmax", "L10", "L50", "L90"])
            for r in hub.store.minutes(end - days * 86400, end):
                w.writerow([datetime.fromtimestamp(r["ts"]).isoformat(timespec="minutes"), r["ts"], r["secs"],
                            r["laeq"], r["lmin"], r["lmax"], r["l10"], r["l50"], r["l90"]])
            fname = f"mute-history-{datetime.now().strftime('%Y%m%d')}.csv"
            self._send(200, out.getvalue().encode("utf-8"), "text/csv; charset=utf-8",
                       {"Content-Disposition": f'attachment; filename="{fname}"'})

        def _get_events_csv(self, path, qs):
            days = _float_param(qs, "days", 30, 1, 400)
            out = io.StringIO()
            w = csv.writer(out)
            w.writerow(["kind", "start_local", "end_local", "duration_s", "peak_db", "laeq_db"])
            for e in reversed(hub.store.events(time.time() - days * 86400, None, limit=100000)):
                w.writerow([e["kind"], datetime.fromtimestamp(e["start"]).isoformat(timespec="seconds"),
                            datetime.fromtimestamp(e["end"]).isoformat(timespec="seconds"),
                            round(e["duration_s"] or 0, 1), e["peak"], e["laeq"]])
            self._send(200, out.getvalue().encode("utf-8"), "text/csv; charset=utf-8",
                       {"Content-Disposition": 'attachment; filename="mute-events.csv"'})

        def _get_settings(self, path, qs):
            self._json({"settings": settings.to_dict(), "defaults": DEFAULT_SETTINGS,
                        "notify_kinds": notify_kinds(), "info": info()})

        def _get_lab_programs(self, path, qs):
            self._json({"programs": [p.to_dict() for p in PROGRAMS.values()]})

        def _get_lab_runs(self, path, qs):
            runs = hub.store.lab_runs(limit=int(_float_param(qs, "limit", 30, 1, 200)))
            for r in runs:
                if r.get("result"):
                    r["result"].pop("samples", None)
            self._json({"runs": runs, "current": hub.lab.current.to_dict() if hub.lab.current else None})

        def _get_lab_runs_id(self, path, qs):
            run = hub.lab.get(path.rsplit("/", 1)[-1])
            if not run:
                raise ApiError(404, "run not found")
            self._json(run.to_dict())

        def _get_lab_wav_file(self, path, qs):
            name = path.rsplit("/", 1)[-1][:-4]
            program = PROGRAMS.get(name)
            if not program or program.mode != "play":
                raise ApiError(404, "no such test signal")
            body = render_wav(program, lead_in_s=2.0)
            self._send(200, body, "audio/wav",
                       {"Content-Disposition": f'attachment; filename="mute-{program.id}.wav"'})

        def _get_ha_yaml(self, path, qs):
            self._send(200, ha_yaml(hub).encode("utf-8"), "text/plain; charset=utf-8")

        def _get_stream(self, path, qs):
            q = hub.subscribe()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                self.close_connection = True
                first = hub.snapshot()
                first["seconds"] = []
                self.wfile.write(f"data: {json.dumps(first)}\n\n".encode())
                self.wfile.flush()
                last_ping = time.time()
                while not hub.stopped:
                    try:
                        msg = q.get(timeout=1.0)
                        self.wfile.write(f"data: {json.dumps(msg)}\n\n".encode())
                        self.wfile.flush()
                    except queue.Empty:
                        pass
                    if time.time() - last_ping > 15:
                        last_ping = time.time()
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                hub.unsubscribe(q)

        # ----------------------------------------------------------------- POST
        def _post_settings(self, path, qs):
            body = self._body()
            try:
                updated = settings.update(body)
            except (ValueError, KeyError) as exc:
                raise ApiError(400, str(exc))
            hub.settings_changed()
            self._json({"settings": updated})

        def _post_lab_runs(self, path, qs):
            body = self._body()
            test = body.get("test")
            params = body.get("params") or {}
            if not isinstance(params, dict):
                raise ApiError(400, "params must be an object")
            if params.get("reference_db") is not None:
                try:
                    params["reference_db"] = float(params["reference_db"])
                except (TypeError, ValueError):
                    raise ApiError(400, "invalid reference_db")
            try:
                run = hub.lab.start(test, params)
            except KeyError as exc:
                raise ApiError(404, str(exc))
            except RuntimeError as exc:
                raise ApiError(409, str(exc))
            data = run.to_dict()
            data["program"] = run.program.to_dict()
            data["server_time"] = time.time()
            self._json(data, 201)

        def _post_lab_runs_id_cancel(self, path, qs):
            run_id = path.split("/")[-2]
            if not hub.lab.cancel(run_id):
                raise ApiError(404, "no running test with that id")
            self._json({"cancelled": run_id})

        def _post_lab_runs_id_reference(self, path, qs):
            run_id = path.split("/")[-2]
            body = self._body()
            try:
                ref = float(body.get("reference_db"))
            except (TypeError, ValueError):
                raise ApiError(400, "reference_db is required")
            try:
                result = hub.lab.set_reference(run_id, ref)
            except KeyError as exc:
                raise ApiError(404, str(exc))
            except ValueError as exc:
                raise ApiError(400, str(exc))
            self._json({"result": result})

        def _post_notify_test(self, path, qs):
            n = hub.notifier
            if not n or not n.configured:
                raise ApiError(400, "ntfy is not configured (set MUTE_NTFY_URL)")
            n.send("lab", f"🔔 Test from {hub.device_name}", "Notifications work. You'll hear from me when it gets loud.",
                   tags=["bell"], force=True)
            n.flush(8.0)
            if n.last_error:
                raise ApiError(502, f"ntfy error: {n.last_error}")
            self._json({"sent": True})

        def _post_summary_send(self, path, qs):
            from datetime import date, timedelta
            if not hub.send_daily_summary(date.today() - timedelta(days=1)):
                raise ApiError(400, "no data for yesterday or ntfy not configured")
            self._json({"sent": True})

    def info():
        n = hub.notifier
        mq = hub.mqtt
        return {
            "device_name": hub.device_name,
            "mode": hub.mode,
            "version": hub.version,
            "ntfy": {
                "configured": bool(n and n.configured),
                "topic": n.topic if n and n.configured else None,
                "server": n.server if n and n.configured else None,
                "error": (n.error or n.last_error) if n else None,
                "recent": list(n.sent[-5:]) if n else [],
            },
            "mqtt": {"enabled": bool(mq), "connected": bool(mq and getattr(mq, "connected", False)),
                     "device_id": getattr(mq, "device_id", None)},
            "history_since": hub.store.first_minute_ts(),
            "token_required": bool(token),
            "public_url": env.public_url,
        }

    return Handler


class DashboardServer:
    def __init__(self, hub, settings, env, logger=None):
        self.hub = hub
        self.env = env
        self.logger = logger
        handler = make_handler(hub, settings, env)
        self.httpd = ThreadingHTTPServer((env.bind, int(env.port)), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="mutelab-http", daemon=True)

    def start(self):
        self._thread.start()
        if self.logger:
            self.logger.info(f"[LAB] Dashboard listening on http://{self.env.bind}:{self.port}/")

    def stop(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass
