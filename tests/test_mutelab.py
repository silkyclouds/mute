"""Mute Lab tests. Run with:  python -m unittest discover -s tests   (or: pytest)"""

import json
import logging
import os
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mutelab import acoustics as ac  # noqa: E402
from mutelab.history import HistoryStore  # noqa: E402
from mutelab.hub import LocalHub  # noqa: E402
from mutelab.labtests import PROGRAMS, LabRunner, find_alignment, render_wav  # noqa: E402
from mutelab.notify import Notifier, split_topic_url  # noqa: E402
from mutelab.server import DashboardServer  # noqa: E402
from mutelab.settings import LabEnv, LabSettings  # noqa: E402
from mutelab.simulator import SimulatedMeter  # noqa: E402

LOG = logging.getLogger("mutelab-tests")
LOG.addHandler(logging.NullHandler())
LOG.propagate = False


class FakeTransport:
    def __init__(self):
        self.payloads = []

    def __call__(self, payload):
        self.payloads.append(payload)

    def titles(self):
        return [p["title"] for p in self.payloads]


def make_notifier(settings=None):
    transport = FakeTransport()
    n = Notifier("https://ntfy.example/topic", transport=transport, logger=LOG,
                 is_enabled=settings.notify_enabled if settings else None)
    return n, transport


class AcousticsTest(unittest.TestCase):
    def test_leq_is_energetic(self):
        self.assertAlmostEqual(ac.leq([60, 60]), 60.0, places=6)
        self.assertAlmostEqual(ac.leq([60, 70]), 67.4, places=1)
        self.assertIsNone(ac.leq([]))

    def test_lden_constant_level(self):
        # 12 h at L, 4 h at L+5, 8 h at L+10 -> L + 6.4 dB
        self.assertAlmostEqual(ac.lden(60, 60, 60), 66.4, places=1)
        self.assertIsNone(ac.lden(60, None, 60))

    def test_exceedance_levels(self):
        ex = ac.exceedance_levels(range(1, 101))
        self.assertAlmostEqual(ex["l10"], 90.1, places=1)
        self.assertAlmostEqual(ex["l50"], 50.5, places=1)
        self.assertAlmostEqual(ex["l90"], 10.9, places=1)

    def test_time_range_wraps_midnight(self):
        self.assertTrue(ac.in_time_range(datetime(2026, 1, 1, 23, 30), "22:00-07:00"))
        self.assertTrue(ac.in_time_range(datetime(2026, 1, 1, 6, 59), "22:00-07:00"))
        self.assertFalse(ac.in_time_range(datetime(2026, 1, 1, 12, 0), "22:00-07:00"))
        self.assertFalse(ac.in_time_range(datetime(2026, 1, 1, 12, 0), "00:00-00:00"))


class SettingsTest(unittest.TestCase):
    def test_validation_and_persistence(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "lab.json")
        s = LabSettings(path)
        with self.assertRaises(ValueError):
            s.update({"event_trigger_db": 300})
        with self.assertRaises(ValueError):
            s.update({"quiet_hours": "25:00-07:00"})
        with self.assertRaises(ValueError):
            s.update({"notify": ["episodes", "spam"]})
        with self.assertRaises(ValueError):
            s.update({"nope": 1})
        s.update({"event_trigger_db": "72.34", "notify": "episodes,daily", "daily_summary_time": "7:05"})
        again = LabSettings(path)
        self.assertEqual(again.get("event_trigger_db"), 72.3)
        self.assertEqual(again.get("notify"), ["episodes", "daily"])
        self.assertEqual(again.get("daily_summary_time"), "07:05")
        self.assertTrue(again.notify_enabled("daily"))
        self.assertFalse(again.notify_enabled("events"))

    def test_env_overrides_defaults_but_not_saved_values(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "lab.json")
        os.environ["MUTE_LAB_EPISODE_DB"] = "61"
        try:
            self.assertEqual(LabSettings(path).get("episode_db"), 61.0)
            LabSettings(path).update({"episode_db": 58})
            self.assertEqual(LabSettings(path).get("episode_db"), 58.0)
        finally:
            del os.environ["MUTE_LAB_EPISODE_DB"]


class HistoryTest(unittest.TestCase):
    def test_series_and_day_summary(self):
        store = HistoryStore(":memory:")
        day = date(2026, 3, 10)
        start = datetime(day.year, day.month, day.day).timestamp()
        rows = []
        for i in range(1440):
            level = 50.0
            rows.append({"ts": int(start + i * 60), "secs": 60, "energy": ac.db_to_energy(level), "laeq": level,
                         "lmin": 45.0, "lmax": 60.0 + (i == 600) * 30, "l10": 52.0, "l50": 50.0, "l90": 47.0})
        store.add_minutes(rows)
        summary = store.day_summary(day)
        self.assertAlmostEqual(summary["lday"], 50.0, places=1)
        self.assertAlmostEqual(summary["lnight"], 50.0, places=1)
        self.assertAlmostEqual(summary["lden"], 56.4, places=1)
        self.assertEqual(summary["lmax"], 90.0)
        series = store.series(start, start + 86400, 3600)
        self.assertEqual(len(series), 24)
        self.assertEqual(series[0]["laeq"], 50.0)
        self.assertEqual(series[0]["coverage"], 1.0)

    def test_events_roundtrip(self):
        store = HistoryStore(":memory:")
        eid = store.add_event("event", 100.0, 103.0, 91.2, 85.0, curve=[[100, 91.2]], meta={"trigger": 80})
        item = store.event(eid)
        self.assertEqual(item["curve"], [[100, 91.2]])
        self.assertEqual(item["duration_s"], 3.0)
        self.assertEqual(store.count_events(0, "event"), 1)


class HubTest(unittest.TestCase):
    def setUp(self):
        self.settings = LabSettings(None)
        self.settings.update({"quiet_hours": "00:00-00:00", "event_trigger_db": 80, "episode_db": 65,
                              "episode_end_minutes": 2, "notify": ["episodes", "events", "health", "lab"],
                              "event_notify_cooldown_s": 0})
        self.store = HistoryStore(":memory:")
        self.notifier, self.transport = make_notifier(self.settings)
        self.now = 1_800_000_000.0  # fixed, minute-aligned start
        self.hub = LocalHub(self.settings, LabEnv(), self.store, notifier=self.notifier, logger=LOG,
                            device_name="Test Box", clock=lambda: self.now)

    def feed(self, seconds, level_fn, rate=4):
        for i in range(int(seconds * rate)):
            t = self.now + i / rate
            self.hub.on_sample(t, level_fn(t))
            if i % rate == 0:
                self.hub.tick(t)
        self.now += seconds
        self.hub.tick(self.now)

    def test_event_episode_minutes_and_notifications(self):
        self.feed(120, lambda t: 40.0)
        burst_start = self.now + 5
        self.feed(30, lambda t: 92.0 if burst_start <= t < burst_start + 2 else 40.0)
        self.feed(300, lambda t: 70.0)  # sustained noise -> episode
        self.feed(240, lambda t: 40.0)  # quiet -> episode ends after 2 min
        self.notifier.flush(2)

        events = self.store.events(kind="event", limit=10)
        self.assertGreaterEqual(len(events), 1)
        burst = events[-1]
        self.assertEqual(burst["peak"], 92.0)
        self.assertLessEqual(burst["duration_s"], 3.0)
        episodes = self.store.events(kind="episode")
        self.assertEqual(len(episodes), 1)
        self.assertAlmostEqual(episodes[0]["duration_s"], 300, delta=75)
        self.assertGreater(self.store.minute_count(), 10)

        titles = self.transport.titles()
        self.assertTrue(any("92 dB" in t for t in titles), titles)
        self.assertTrue(any("Noise started" in t for t in titles), titles)
        self.assertTrue(any("Quiet again" in t for t in titles), titles)

        snap = self.hub.snapshot(self.now)
        self.assertEqual(snap["stats"]["laeq_1m"], 40.0)
        self.assertFalse(snap["episode"]["active"])
        self.assertTrue(snap["health"]["online"])
        self.assertEqual(snap["today"]["episodes"], 1)

    def test_meter_offline_notification(self):
        self.feed(30, lambda t: 45.0)
        self.now += 20
        self.hub.tick(self.now)
        self.notifier.flush(2)
        self.assertFalse(self.hub.health["online"])
        self.assertIn("📴 Meter offline", self.transport.titles())

    def test_calibration_offset_applies(self):
        self.assertEqual(self.hub.apply_calibration(50.0), 50.0)
        self.settings.update({"calibration_offset_db": 1.7})
        self.hub.settings_changed()
        self.assertEqual(self.hub.apply_calibration(50.0), 51.7)

    def test_lab_signals_stay_out_of_history_and_alerts(self):
        self.feed(60, lambda t: 40.0)
        run = self.hub.lab.start("linearity")
        run.t0 = self.now
        self.feed(run.capture_end - self.now, lambda t: 95.0)
        self.hub.lab.cancel(run.id)
        self.feed(70, lambda t: 40.0)
        self.notifier.flush(2)
        self.assertEqual(self.store.events(), [])
        noise_alerts = [t for t in self.transport.titles() if "dB at" in t or "Noise" in t]
        self.assertEqual(noise_alerts, [])
        self.assertLess(max(r["lmax"] for r in self.store.minutes(0, self.now + 60)), 50)

    def test_notification_kinds_are_respected(self):
        self.settings.update({"notify": ["daily"]})
        self.feed(30, lambda t: 95.0 if t < self.now + 2 else 40.0)
        self.notifier.flush(2)
        self.assertEqual(self.transport.payloads, [])


def run_program(program_id, params=None, tau=None, error_db=0.0, playback=True):
    lab = LabRunner(None, LabSettings(None))
    run = lab.start(program_id, params or {})
    meter = SimulatedMeter(seed=3, lively=0.0, playback=lab.playback_level if playback else None)
    if tau:
        meter.TAU_FAST = tau
    t = run.t0 - 1
    while t < run.capture_end:
        lab.on_sample(t, round(meter.read(t) + error_db, 1))
        t += 0.1
    lab.analyze_now(run)
    return lab, run


class LabTest(unittest.TestCase):
    def test_linearity_passes_on_ideal_meter(self):
        _, run = run_program("linearity")
        r = run.result
        self.assertEqual(r["verdict"], "pass", r["summary"])
        self.assertEqual(len(r["steps"]), 6)
        self.assertLess(r["metrics"]["max_error_db"], 1.0)

    def test_linearity_ignores_outside_noise(self):
        prog = PROGRAMS["linearity"]
        last = [x for x in prog.segments if x.role == "step"][-1]
        lab = LabRunner(None, LabSettings(None))
        run = lab.start("linearity")
        meter = SimulatedMeter(seed=3, lively=0.0, playback=lab.playback_level)
        t = run.t0 - 1
        while t < run.capture_end:
            v = meter.read(t)
            rel = t - run.t0 - 0.35
            if last.start + 0.5 <= rel < last.end - 0.3:  # a scooter passes during the last step
                v = round(ac.energy_to_db(ac.db_to_energy(v) + ac.db_to_energy(55 + 15 * (rel - last.start))), 1)
            lab.on_sample(t, v)
            t += 0.1
        lab.analyze_now(run)
        r = run.result
        self.assertEqual(r["verdict"], "warn", r["summary"])
        self.assertTrue(r["steps"][-1]["disturbed"])
        self.assertLess(r["metrics"]["max_error_db"], 1.0)
        self.assertIn("disturbed", r["summary"])

    def test_signal_not_detected_without_playback(self):
        _, run = run_program("linearity", playback=False)
        self.assertEqual(run.result["verdict"], "fail")

    def test_response_identifies_fast_and_slow(self):
        _, fast = run_program("response")
        self.assertEqual(fast.result["metrics"].get("time_weighting"), "Fast", fast.result["summary"])
        _, slow = run_program("response", tau=1.0)
        self.assertEqual(slow.result["metrics"].get("time_weighting"), "Slow", slow.result["summary"])

    def test_calibration_with_reference(self):
        lab, run = run_program("calibration", error_db=-2.0)
        self.assertTrue(run.result["needs_reference"])
        measured = run.result["metrics"]["measured_db"]
        updated = lab.set_reference(run.id, measured + 2.0)
        self.assertEqual(updated["apply"]["calibration_offset_db"], 2.0)

    def test_acoustic_calibrator(self):
        _, run = run_program("calibrator", {"reference_db": 94.0})
        # the demo calibrator reads 1.3 dB low
        self.assertAlmostEqual(run.result["apply"]["calibration_offset_db"], 1.3, delta=0.3)

    def test_selftest_and_ambient(self):
        _, st = run_program("selftest")
        self.assertIn(st.result["verdict"], ("pass", "warn"))
        self.assertGreater(st.result["metrics"]["rate_hz"], 5)
        _, amb = run_program("ambient", {"listen_s": 30})
        self.assertIn("event_trigger_db", amb.result["apply"])
        self.assertGreater(amb.result["apply"]["episode_db"], amb.result["apply"]["episode_night_db"] - 0.01)

    def test_alignment_recovers_latency(self):
        lab = LabRunner()
        run = lab.start("linearity")
        meter = SimulatedMeter(seed=5, lively=0.0, playback=lambda t: lab.playback_level(t, delay_s=2.3))
        samples, t = [], run.t0
        while t < run.capture_end:
            samples.append((t, meter.read(t)))
            t += 0.1
        lag, corr = find_alignment(PROGRAMS["linearity"], samples, run.t0)
        self.assertAlmostEqual(lag, 2.3, delta=0.25)
        self.assertGreater(corr, 0.8)

    def test_one_run_at_a_time(self):
        lab = LabRunner()
        lab.start("selftest")
        with self.assertRaises(RuntimeError):
            lab.start("linearity")

    def test_wav_render(self):
        data = render_wav(PROGRAMS["response"])
        self.assertEqual(data[:4], b"RIFF")
        with self.assertRaises(ValueError):
            render_wav(PROGRAMS["selftest"])


class NotifierTest(unittest.TestCase):
    def test_topic_url(self):
        self.assertEqual(split_topic_url("https://ntfy.sh/mute-abc"), ("https://ntfy.sh", "mute-abc"))
        self.assertEqual(split_topic_url("http://box:8080/ntfy/t1/"), ("http://box:8080/ntfy", "t1"))
        with self.assertRaises(ValueError):
            split_topic_url("https://ntfy.sh/")

    def test_cooldown_and_payload(self):
        n, transport = make_notifier()
        self.assertTrue(n.send("events", "a", "b", cooldown_key="k", cooldown_s=60, tags=["x"]))
        self.assertFalse(n.send("events", "a", "b", cooldown_key="k", cooldown_s=60))
        n.flush(2)
        self.assertEqual(len(transport.payloads), 1)
        self.assertEqual(transport.payloads[0]["topic"], "topic")
        self.assertEqual(transport.payloads[0]["tags"], ["x"])

    def test_unconfigured_is_silent(self):
        n = Notifier("")
        self.assertFalse(n.configured)
        self.assertFalse(n.send("events", "a", "b"))


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.env = LabEnv(port=0, bind="127.0.0.1", token="s3cret", data_dir=cls.dir)
        cls.settings = LabSettings(os.path.join(cls.dir, "lab.json"))
        cls.store = HistoryStore(os.path.join(cls.dir, "h.db"))
        cls.hub = LocalHub(cls.settings, cls.env, cls.store, logger=LOG, device_name="Srv Box", mode="demo")
        cls.server = DashboardServer(cls.hub, cls.settings, cls.env)
        cls.server.start()
        cls.hub.start()
        now = time.time()
        for i in range(40):
            cls.hub.on_sample(now - 4 + i * 0.1, 50.0 + (i % 5))
        cls.base = f"http://127.0.0.1:{cls.server.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.hub.stop()

    def req(self, path, method="GET", body=None, token=True):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            r.add_header("Content-Type", "application/json")
        if token:
            r.add_header("X-Mute-Token", "s3cret")
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers

    def test_index_is_public_api_needs_token(self):
        code, body, _ = self.req("/", token=False)
        self.assertEqual(code, 200)
        self.assertIn(b"Mute Lab", body)
        code, _, _ = self.req("/api/status", token=False)
        self.assertEqual(code, 401)

    def test_status_and_live(self):
        time.sleep(0.5)
        code, body, _ = self.req("/api/status")
        self.assertEqual(code, 200)
        snap = json.loads(body)
        self.assertEqual(snap["info"]["device_name"], "Srv Box")
        self.assertIsNotNone(snap["db"])
        code, body, _ = self.req("/api/live?seconds=60")
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["seconds"])

    def test_settings_validation(self):
        code, body, _ = self.req("/api/settings", "POST", {"event_trigger_db": 999})
        self.assertEqual(code, 400)
        code, body, _ = self.req("/api/settings", "POST", {"event_trigger_db": 77})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["settings"]["event_trigger_db"], 77.0)

    def test_lab_endpoints(self):
        code, body, _ = self.req("/api/lab/programs")
        self.assertEqual(code, 200)
        self.assertIn("linearity", [p["id"] for p in json.loads(body)["programs"]])
        code, body, _ = self.req("/api/lab/runs", "POST", {"test": "selftest"})
        self.assertEqual(code, 201)
        run_id = json.loads(body)["id"]
        code, _, _ = self.req("/api/lab/runs", "POST", {"test": "linearity"})
        self.assertEqual(code, 409)
        code, body, _ = self.req(f"/api/lab/runs/{run_id}")
        self.assertEqual(json.loads(body)["status"], "capturing")
        code, _, _ = self.req(f"/api/lab/runs/{run_id}/cancel", "POST")
        self.assertEqual(code, 200)
        code, body, headers = self.req("/api/lab/wav/linearity.wav")
        self.assertEqual(code, 200)
        self.assertEqual(body[:4], b"RIFF")
        code, _, _ = self.req("/api/lab/runs", "POST", {"test": "nope"})
        self.assertEqual(code, 404)

    def test_exports_and_misc(self):
        code, body, headers = self.req("/api/export.csv?days=1")
        self.assertEqual(code, 200)
        self.assertTrue(body.startswith(b"local_time"))
        for path in ("/api/days?n=3", "/api/history?hours=24", "/api/events", "/api/lab/runs", "/api/ha.yaml"):
            code, _, _ = self.req(path)
            self.assertEqual(code, 200, path)
        code, _, _ = self.req("/api/notify/test", "POST")
        self.assertEqual(code, 400)  # ntfy not configured
        code, _, _ = self.req("/api/does-not-exist")
        self.assertEqual(code, 404)

    def test_sse_stream_sends_snapshot(self):
        r = urllib.request.Request(self.base + "/api/stream?token=s3cret")
        with urllib.request.urlopen(r, timeout=5) as resp:
            self.assertEqual(resp.headers["Content-Type"], "text/event-stream")
            line = resp.readline()
        self.assertTrue(line.startswith(b"data: "))
        self.assertIn("stats", json.loads(line[6:]))


class IntegrationTest(unittest.TestCase):
    def test_maybe_start_lab_is_opt_in(self):
        from mutelab.integration import maybe_start_lab

        for key in ("MUTE_LAB_ENABLED", "MUTE_LAB_PORT", "MUTE_LAB_DATA_DIR", "MUTE_LAB_BIND"):
            os.environ.pop(key, None)
        self.assertIsNone(maybe_start_lab({"device_name": "X"}, None, LOG, "0"))
        d = tempfile.mkdtemp()
        os.environ.update({"MUTE_LAB_ENABLED": "true", "MUTE_LAB_PORT": "0", "MUTE_LAB_DATA_DIR": d,
                           "MUTE_LAB_BIND": "127.0.0.1"})
        try:
            station = maybe_start_lab({"device_name": "Cloud Box"}, None, LOG, "0.13.0")
            self.assertIsNotNone(station)
            station.on_sample(time.time(), station.apply_calibration(55.0))
            self.assertTrue(os.path.exists(os.path.join(d, "mute_history.db")))
            with urllib.request.urlopen(f"http://127.0.0.1:{station.server.port}/api/status", timeout=5) as r:
                self.assertEqual(json.loads(r.read())["info"]["mode"], "cloud")
            station.stop()
        finally:
            for key in ("MUTE_LAB_ENABLED", "MUTE_LAB_PORT", "MUTE_LAB_DATA_DIR", "MUTE_LAB_BIND"):
                os.environ.pop(key, None)


class MqttDiscoveryTest(unittest.TestCase):
    def test_lab_entities_published(self):
        from mqtt_client import MuteqMqttClient

        published = []

        class FakeClient:
            def publish(self, topic, payload, qos=0, retain=False):
                published.append((topic, payload, retain))

        c = MuteqMqttClient("dev123", "My Box", "", "x", "broker", 1883, "", "", False, LOG)
        c.client = FakeClient()
        c.connected = True
        c.enable_lab_entities()
        topics = [t for t, _, _ in published]
        self.assertIn("homeassistant/binary_sensor/dev123_noise_episode/config", topics)
        self.assertIn("homeassistant/sensor/dev123_laeq_1m/config", topics)
        for topic, payload, retain in published:
            self.assertTrue(retain)
            cfg = json.loads(payload)
            self.assertIn("unique_id", cfg)
            self.assertNotEqual(cfg["device"]["sw_version"], "0.0.26")
        published.clear()
        c.publish_lab_episode(True)
        c.publish_lab_stats({"laeq_1m": 50.0})
        self.assertEqual(published[0], ("muteq/dev123/lab/episode", "ON", True))
        self.assertEqual(published[1][0], "muteq/dev123/lab/stats")


if __name__ == "__main__":
    unittest.main()
