"""
Mute Lab command line.

  python -m mutelab demo                 # try everything with a simulated meter (no hardware)
  python -m mutelab run                  # standalone local station with the real USB meter, no cloud
  python -m mutelab wav all --out ./wav  # export the lab test signals as WAV files
  python -m mutelab report --days 14     # daily Lday/Levening/Lnight/Lden table from the local history
"""

import argparse
import logging
import os
import signal
import sys
import threading
import time

from . import __version__
from .acoustics import WHO_ROAD_LDEN_DB
from .history import HistoryStore
from .labtests import PROGRAMS, render_wav
from .settings import LabEnv


def _logger() -> logging.Logger:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    return logging.getLogger("mutelab")


def _mqtt_from_env(device_id: str, device_name: str, logger):
    if os.environ.get("LOCAL_MQTT_ENABLED", "false").lower() not in ("1", "true", "yes", "on"):
        return None
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
    from mqtt_client import MuteqMqttClient

    client = MuteqMqttClient(
        device_id=device_id,
        device_name=device_name,
        address="",
        env_profile="local",
        server=os.environ.get("LOCAL_MQTT_SERVER", ""),
        port=int(os.environ.get("LOCAL_MQTT_PORT", "1883")),
        username=os.environ.get("LOCAL_MQTT_USER", ""),
        password=os.environ.get("LOCAL_MQTT_PASS", ""),
        tls=os.environ.get("LOCAL_MQTT_TLS", "false").lower() in ("1", "true", "yes", "on"),
        logger=logger,
    )
    client.connect()
    return client


def _run_station(args, simulate: bool):
    from .integration import LabStation, local_device_id
    from .simulator import SimulatedMeter, seed_history

    logger = _logger()
    env = LabEnv.from_environ(port=args.port, data_dir=args.data_dir, bind=args.bind)
    os.makedirs(env.data_dir, exist_ok=True)
    device_name = args.name
    if simulate and args.seed_history:
        store = HistoryStore(os.path.join(env.data_dir, "mute_history.db"))
        if store.minute_count() == 0:
            logger.info(f"[DEMO] Generating {args.seed_history} days of synthetic history (one-off)...")
            t0 = time.time()
            n = seed_history(store, days=args.seed_history)
            logger.info(f"[DEMO] {n} minutes generated in {time.time() - t0:.1f} s")
        store.close()

    device = None
    if not simulate:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if root not in sys.path:
            sys.path.insert(0, root)
        from usb_reader import find_usb_device, read_spl_value

        device = find_usb_device(None, None, logger)  # exits with a clear message if no meter
        logger.info(f"[METER] {device}")

    mqtt = _mqtt_from_env(local_device_id(env.data_dir), device_name, logger)
    station = LabStation(env, logger, mqtt=mqtt, device_name=device_name,
                         mode="demo" if simulate else "standalone", version=__version__)
    station.start()

    if simulate:
        meter = SimulatedMeter(lively=args.lively, playback=station.hub.lab.playback_level)
        read = lambda: meter.read()  # noqa: E731
        interval = 1.0 / args.rate
    else:
        read = lambda: read_spl_value(device, logger)  # noqa: E731
        interval = 0.01

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    url = f"http://localhost:{station.server.port}/" if station.server else "(dashboard disabled)"
    logger.info(f"[LAB] Open the dashboard: {url}")
    last_ok = time.time()
    while not stop.is_set():
        value = read()
        now = time.time()
        if value is not None:
            last_ok = now
            station.on_sample(now, station.apply_calibration(value))
        elif now - last_ok > 30 and not simulate:
            logger.error("[METER] no readings for 30 s, exiting so the supervisor can restart us")
            station.report_fatal("no readings from the sound meter for 30 s")
            break
        stop.wait(interval)
    station.stop()
    if mqtt:
        mqtt.disconnect()


def cmd_wav(args):
    os.makedirs(args.out, exist_ok=True)
    ids = [p.id for p in PROGRAMS.values() if p.mode == "play"] if args.program == "all" else [args.program]
    for pid in ids:
        program = PROGRAMS.get(pid)
        if not program or program.mode != "play":
            sys.exit(f"unknown or silent program: {pid}")
        path = os.path.join(args.out, f"mute-{pid}.wav")
        with open(path, "wb") as fh:
            fh.write(render_wav(program, lead_in_s=2.0))
        print(f"{path}  ({program.duration + 2:.0f} s) — {program.name}")
    print("\nStart the matching test in the dashboard with 'I'll play the WAV myself', then press play.")


def cmd_report(args):
    path = os.path.join(args.data_dir, "mute_history.db")
    if not os.path.exists(path):
        sys.exit(f"no history at {path}")
    store = HistoryStore(path)
    days = store.daily(args.days)
    fmt = lambda v: "   —" if v is None else f"{v:5.1f}"  # noqa: E731
    print(f"{'date':<11} {'LAeq24':>6} {'Lday':>6} {'Leve':>6} {'Lnight':>6} {'Lden':>6} {'Lmax':>6} {'events':>6}")
    for d in reversed(days):
        mark = " ⚠" if d["lden"] is not None and d["lden"] > WHO_ROAD_LDEN_DB else ""
        print(f"{d['date']:<11} {fmt(d['laeq_24h']):>6} {fmt(d['lday']):>6} {fmt(d['levening']):>6} "
              f"{fmt(d['lnight']):>6} {fmt(d['lden']):>6} {fmt(d['lmax']):>6} {d['events']:>6}{mark}")
    print(f"\n⚠ = Lden above the WHO road-traffic guideline ({WHO_ROAD_LDEN_DB:.0f} dB)")
    store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m mutelab", description="Mute Lab — local companion for mute")
    parser.add_argument("--version", action="version", version=f"mutelab {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def station_args(p, default_dir):
        p.add_argument("--port", type=int, default=int(os.environ.get("MUTE_LAB_PORT", "8080")))
        p.add_argument("--bind", default=os.environ.get("MUTE_LAB_BIND", "0.0.0.0"))
        p.add_argument("--data-dir", default=os.environ.get("MUTE_LAB_DATA_DIR", default_dir))
        p.add_argument("--name", default=os.environ.get("MUTE_DEVICE_NAME", "Mute Box"))

    p_demo = sub.add_parser("demo", help="simulated meter + 14 days of synthetic history")
    station_args(p_demo, "./mutelab-demo")
    p_demo.add_argument("--seed-history", type=int, default=14, metavar="DAYS")
    p_demo.add_argument("--rate", type=float, default=8.0, help="simulated readings per second")
    p_demo.add_argument("--lively", type=float, default=3.0, help="how eventful the simulated street is")

    p_run = sub.add_parser("run", help="standalone station with the real USB meter (no cloud)")
    station_args(p_run, "/config")

    p_wav = sub.add_parser("wav", help="export lab test signals as WAV")
    p_wav.add_argument("program", choices=["all"] + [p.id for p in PROGRAMS.values() if p.mode == "play"])
    p_wav.add_argument("--out", default=".")

    p_rep = sub.add_parser("report", help="daily noise report from the local history")
    p_rep.add_argument("--days", type=int, default=14)
    p_rep.add_argument("--data-dir", default=os.environ.get("MUTE_LAB_DATA_DIR", "/config"))

    args = parser.parse_args(argv)
    if args.cmd == "demo":
        _run_station(args, simulate=True)
    elif args.cmd == "run":
        args.seed_history = 0
        _run_station(args, simulate=False)
    elif args.cmd == "wav":
        cmd_wav(args)
    elif args.cmd == "report":
        cmd_report(args)


if __name__ == "__main__":
    main()
