"""
Meter lab: test programs, test-signal synthesis and analysis.

Every "play" program is a list of timed segments. The dashboard plays them through the
phone speaker (Web Audio) while the box records the meter, or you can download the
same program as a WAV and play it on any speaker. The analysis aligns the recording
to the program automatically, so clock offsets and audio latency do not matter.
"""

import io
import math
import random
import statistics
import threading
import time
import uuid
import wave
from array import array
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .acoustics import exceedance_levels, leq, median, percentile, round_or_none

GRID_HZ = 10.0


@dataclass
class Segment:
    start: float
    duration: float
    kind: str  # "tone" | "pink" | "silence"
    gain_db: float = 0.0
    freq: float = 1000.0
    label: str = ""
    role: str = ""  # analysis hint: "step", "steady", "burst", "background", "reference"

    @property
    def end(self) -> float:
        return self.start + self.duration

    @property
    def audible(self) -> bool:
        return self.kind in ("tone", "pink")

    def to_dict(self) -> dict:
        return {
            "start": round(self.start, 3),
            "duration": round(self.duration, 3),
            "kind": self.kind,
            "gain_db": self.gain_db,
            "freq": self.freq,
            "label": self.label,
            "role": self.role,
        }


@dataclass
class Program:
    id: str
    name: str
    icon: str
    description: str
    mode: str  # "play" (needs a speaker) or "listen" (meter only)
    segments: List[Segment] = field(default_factory=list)
    listen_s: float = 0.0
    how_to: str = ""

    @property
    def duration(self) -> float:
        if self.segments:
            return max(s.end for s in self.segments)
        return self.listen_s

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "icon": self.icon,
            "description": self.description,
            "how_to": self.how_to,
            "mode": self.mode,
            "duration": round(self.duration, 2),
            "segments": [s.to_dict() for s in self.segments],
        }


def _linearity_program() -> Program:
    segs = [Segment(0.0, 3.0, "silence", label="Background", role="background")]
    t = 3.0
    for i in range(6):
        gain = -6.0 * i if i else 0.0
        segs.append(Segment(t, 3.0, "tone", gain_db=gain, label=f"Step {i + 1}/6 · {gain:+.0f} dB", role="step"))
        t += 3.0
        segs.append(Segment(t, 1.5, "silence", label="gap", role="gap"))
        t += 1.5
    segs.append(Segment(t, 3.0, "silence", label="Background", role="background"))
    return Program(
        id="linearity",
        name="Linearity staircase",
        icon="📶",
        description="A 1 kHz tone drops by exactly 6 dB six times. Each step should read 6 dB lower "
                    "until the tone sinks into the background. Shows the usable range and linearity error.",
        how_to="Phone speaker 5–10 cm from the meter, volume high (top step ≥ 35 dB above background).",
        mode="play",
        segments=segs,
    )


def _response_program() -> Program:
    segs = [
        Segment(0.0, 3.0, "silence", label="Background", role="background"),
        Segment(3.0, 4.0, "tone", label="Steady reference", role="steady"),
        Segment(7.0, 2.5, "silence", label="gap", role="gap"),
    ]
    t = 9.5
    for i in range(3):
        segs.append(Segment(t, 0.2, "tone", label=f"200 ms burst {i + 1}/3", role="burst"))
        t += 0.2
        segs.append(Segment(t, 2.3, "silence", label="gap", role="gap"))
        t += 2.3
    return Program(
        id="response",
        name="Burst response (Fast / Slow)",
        icon="⚡",
        description="IEC 61672 tone-burst check: a 200 ms burst reads −1.0 dB vs steady on a Fast meter "
                    "and −7.4 dB on a Slow one. Tells you which time weighting your meter really uses.",
        how_to="Same placement as the staircase. Needs a meter that updates at least 4× per second.",
        mode="play",
        segments=segs,
    )


def _calibration_program() -> Program:
    return Program(
        id="calibration",
        name="Reference calibration",
        icon="🎯",
        description="Plays 12 s of steady pink noise. Read your reference meter (or use a 94 dB acoustic "
                    "calibrator instead) and enter its value — the box computes and saves the offset.",
        how_to="Put the reference meter right next to the Mute Box microphone, both facing the speaker.",
        mode="play",
        segments=[
            Segment(0.0, 2.0, "silence", label="Background", role="background"),
            Segment(2.0, 12.0, "pink", gain_db=0.0, label="Pink noise — read your reference meter now", role="reference"),
            Segment(14.0, 1.0, "silence", label="gap", role="gap"),
        ],
    )


def _calibrator_program() -> Program:
    return Program(
        id="calibrator",
        name="Acoustic calibrator (94 / 114 dB)",
        icon="🔧",
        description="No sound played: fit a 94 dB or 114 dB acoustic calibrator on the microphone, start, "
                    "and the box measures 10 s of it to compute the exact offset.",
        how_to="Switch the calibrator on and let it settle before you press Run.",
        mode="listen",
        listen_s=10.0,
    )


def _selftest_program() -> Program:
    return Program(
        id="selftest",
        name="Meter self-test",
        icon="🩺",
        description="30 s of listening: update rate, timing jitter, resolution, noise floor, stuck or "
                    "clipped readings. Run it in a quiet room.",
        how_to="Keep the room quiet during the test.",
        mode="listen",
        listen_s=30.0,
    )


def _ambient_program() -> Program:
    return Program(
        id="ambient",
        name="Ambient survey & auto-tune",
        icon="🌙",
        description="Listens to your normal environment (2 min) and combines it with your local history "
                    "to recommend event and episode thresholds for day and night.",
        how_to="Don't play anything, just let the box listen to a typical moment.",
        mode="listen",
        listen_s=120.0,
    )


PROGRAMS: Dict[str, Program] = {
    p.id: p
    for p in (
        _selftest_program(),
        _linearity_program(),
        _response_program(),
        _calibration_program(),
        _calibrator_program(),
        _ambient_program(),
    )
}


# ---------------------------------------------------------------------------------------
# Synthesis (WAV) — the dashboard does the same thing live with the Web Audio API.
# ---------------------------------------------------------------------------------------
def render_wav(program: Program, sample_rate: int = 22050, base_dbfs: float = -3.0, lead_in_s: float = 0.0) -> bytes:
    if program.mode != "play":
        raise ValueError(f"program {program.id} does not play any sound")
    total = int((program.duration + lead_in_s) * sample_rate)
    buf = array("h", bytes(2 * total))
    rng = random.Random(1361)
    fade = int(0.01 * sample_rate)
    for seg in program.segments:
        if not seg.audible:
            continue
        amp = 32767 * 10 ** ((base_dbfs + seg.gain_db) / 20.0)
        i0 = int((seg.start + lead_in_s) * sample_rate)
        n = int(seg.duration * sample_rate)
        b0 = b1 = b2 = b3 = b4 = b5 = b6 = 0.0
        w = 2 * math.pi * seg.freq / sample_rate
        for k in range(n):
            if seg.kind == "tone":
                x = math.sin(w * k)
            else:  # Paul Kellett's pink noise filter, normalised to roughly ±1
                white = rng.uniform(-1.0, 1.0)
                b0 = 0.99886 * b0 + white * 0.0555179
                b1 = 0.99332 * b1 + white * 0.0750759
                b2 = 0.96900 * b2 + white * 0.1538520
                b3 = 0.86650 * b3 + white * 0.3104856
                b4 = 0.55000 * b4 + white * 0.5329522
                b5 = -0.7616 * b5 - white * 0.0168980
                x = (b0 + b1 + b2 + b3 + b4 + b5 + b6 + white * 0.5362) * 0.11
                b6 = white * 0.115926
            env = 1.0
            if k < fade:
                env = k / fade
            elif k > n - fade:
                env = max(0.0, (n - k) / fade)
            idx = i0 + k
            if idx < total:
                buf[idx] = max(-32767, min(32767, int(amp * env * x)))
    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(buf.tobytes())
    return out.getvalue()


# ---------------------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------------------
def to_grid(samples: List[Tuple[float, float]], t0: float, duration: float, hz: float = GRID_HZ) -> List[Optional[float]]:
    """Resample (t, dB) samples onto a fixed grid: max per bin, then hold the last value."""
    n = int(math.ceil(duration * hz))
    grid: List[Optional[float]] = [None] * n
    for t, v in samples:
        i = int((t - t0) * hz)
        if 0 <= i < n and (grid[i] is None or v > grid[i]):
            grid[i] = v
    last = None
    for i in range(n):
        if grid[i] is None:
            grid[i] = last
        else:
            last = grid[i]
    first = next((v for v in grid if v is not None), None)
    return [first if v is None else v for v in grid]


def expected_envelope(program: Program, n: int, hz: float = GRID_HZ) -> List[float]:
    env = [0.0] * n
    for seg in program.segments:
        if not seg.audible:
            continue
        a, b = int(seg.start * hz), int(seg.end * hz)
        for i in range(max(0, a), min(n, b)):
            env[i] = 1.0 + seg.gain_db / 40.0  # louder segments weigh more, all stay positive
    return env


def _pearson(x: List[float], y: List[float]) -> float:
    n = len(x)
    if n < 3:
        return 0.0
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx <= 0 or syy <= 0:
        return 0.0
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    return sxy / math.sqrt(sxx * syy)


def find_alignment(program: Program, samples: List[Tuple[float, float]], t0: float,
                   max_lag_s: float = 6.0, hz: float = GRID_HZ) -> Tuple[float, float]:
    """Return (lag_seconds, correlation) that best aligns the program onto the recording."""
    span = program.duration + max_lag_s + 1.0
    grid = to_grid(samples, t0, span, hz)
    if not grid or grid[0] is None:
        return 0.0, 0.0
    n_prog = int(program.duration * hz)
    env = expected_envelope(program, n_prog, hz)
    best = (0.0, -1.0)
    for lag_i in range(0, int(max_lag_s * hz) + 1):
        window = grid[lag_i:lag_i + n_prog]
        if len(window) < n_prog:
            break
        r = _pearson(env, window)
        if r > best[1]:
            best = (lag_i / hz, r)
    return best


def _segment_values(samples, t0: float, lag: float, seg: Segment, trim_in: float = 0.6, trim_out: float = 0.2):
    a = t0 + lag + seg.start + min(trim_in, seg.duration * 0.4)
    b = t0 + lag + seg.end - min(trim_out, seg.duration * 0.2)
    return [v for t, v in samples if a <= t <= b]


def sample_rate_stats(samples: List[Tuple[float, float]]) -> Dict[str, Optional[float]]:
    if len(samples) < 3:
        return {"rate_hz": 0.0, "jitter_ms": None, "max_gap_s": None}
    times = [t for t, _ in samples]
    span = times[-1] - times[0]
    deltas = [b - a for a, b in zip(times, times[1:])]
    return {
        "rate_hz": round((len(times) - 1) / span, 2) if span > 0 else 0.0,
        "jitter_ms": round(statistics.pstdev(deltas) * 1000.0, 1),
        "max_gap_s": round(max(deltas), 2),
    }


def _resolution(values: List[float]) -> Optional[float]:
    diffs = sorted({round(abs(b - a), 2) for a, b in zip(values, values[1:]) if abs(b - a) > 0.001})
    return diffs[0] if diffs else None


def _verdict(*levels: str) -> str:
    order = {"pass": 0, "warn": 1, "fail": 2}
    return max(levels, key=lambda v: order[v]) if levels else "pass"


# ---------------------------------------------------------------------------------------
# Analyses — each returns {"verdict", "summary", "metrics", "steps"?, "recommendations", ...}
# ---------------------------------------------------------------------------------------
def analyze_selftest(samples, t0, program, ctx) -> dict:
    values = [v for _, v in samples]
    timing = sample_rate_stats(samples)
    if len(values) < 5:
        return {
            "verdict": "fail",
            "summary": "The meter delivered almost no readings during the test.",
            "metrics": timing,
            "recommendations": ["Check the USB cable / Docker device mapping, then re-run the self-test."],
        }
    stats = exceedance_levels(values)
    spread = statistics.pstdev(values)
    resolution = _resolution(values)
    flat = max(values) - min(values) < 0.05
    at_floor = sum(1 for v in values if v <= 30.05) / len(values)
    verdicts, notes, recs = [], [], []
    rate = timing["rate_hz"] or 0.0
    if rate < 0.5:
        verdicts.append("fail")
        notes.append(f"very slow updates ({rate} Hz)")
        recs.append("The meter updates less than twice a second: short events (door slams) will be missed.")
    elif rate < 2.0:
        verdicts.append("warn")
        notes.append(f"slow updates ({rate} Hz)")
        recs.append("Update rate is low; event peaks may be under-estimated.")
    else:
        notes.append(f"{rate} readings/s")
    if timing["max_gap_s"] and timing["max_gap_s"] > 2.0:
        verdicts.append("warn")
        notes.append(f"gap of {timing['max_gap_s']} s")
        recs.append("Readings stalled for more than 2 s — check USB power (a powered hub helps on a Pi).")
    if flat:
        verdicts.append("fail")
        notes.append("readings never change")
        recs.append("A frozen value usually means a stuck meter: unplug/replug it or power-cycle the box.")
    if at_floor > 0.5:
        verdicts.append("warn")
        notes.append("mostly at the 30 dB floor")
        recs.append("The room is quieter than the meter's range; the noise floor is the meter, not the room.")
    floor = stats["l90"]
    if floor is not None and floor > 50:
        verdicts.append("warn")
        notes.append(f"background {floor:.1f} dB")
        recs.append("Background is high for a self-test: run it again somewhere quieter.")
    verdict = _verdict(*verdicts) if verdicts else "pass"
    return {
        "verdict": verdict,
        "summary": "Self-test: " + ", ".join(notes) + ".",
        "metrics": {
            **timing,
            "readings": len(values),
            "noise_floor_db": round_or_none(floor),
            "laeq_db": round_or_none(leq(values)),
            "l50_db": round_or_none(stats["l50"]),
            "spread_db": round(spread, 2),
            "resolution_db": resolution,
            "min_db": min(values),
            "max_db": max(values),
        },
        "recommendations": recs,
    }


def analyze_linearity(samples, t0, program, ctx) -> dict:
    lag, corr = find_alignment(program, samples, t0, ctx.get("max_lag_s", 6.0))
    timing = sample_rate_stats(samples)
    if corr < 0.5:
        return _not_detected(corr, timing)
    # Background before and after the staircase; the quieter one wins (a car passing during
    # one of them must not hide the low steps).
    backgrounds = [median(_segment_values(samples, t0, lag, seg)) for seg in program.segments
                   if seg.role == "background"]
    backgrounds = [b for b in backgrounds if b is not None]
    background = min(backgrounds) if backgrounds else min(v for _, v in samples)
    measured = []
    for seg in [s for s in program.segments if s.role == "step"]:
        vals = sorted(_segment_values(samples, t0, lag, seg))
        if not vals:
            continue
        # A pure tone gives a rock-steady reading; a wide spread means something else was heard.
        spread = (percentile(vals, 90) or 0) - (percentile(vals, 10) or 0)
        measured.append((seg, median(vals), spread))
    if len(measured) < 2:
        return _not_detected(corr, timing)

    steps, floor_reached, prev_level = [], False, None
    for seg, level, spread in measured:
        above_bg = level - background
        if above_bg < 10.0:
            floor_reached = True
        disturbed = spread > 2.0 or (prev_level is not None and level > prev_level + 1.0)
        steps.append({
            "label": seg.label,
            "gain_db": seg.gain_db,
            "measured_db": round(level, 1),
            "spread_db": round(spread, 1),
            "above_background_db": round(above_bg, 1),
            "disturbed": disturbed,
            "usable": not floor_reached and not disturbed,
        })
        prev_level = level
    # Best fit with slope 1: anchor = median(measured - gain) over the clean steps.
    clean = [s for s in steps if s["usable"]] or [s for s in steps if not s["disturbed"]] or steps
    anchor = median([s["measured_db"] - s["gain_db"] for s in clean])
    for s in steps:
        s["expected_db"] = round(anchor + s["gain_db"], 1)
        s["error_db"] = round(s["measured_db"] - s["expected_db"], 1)
    usable_steps = [s for s in steps if s["usable"]]
    worst = max(usable_steps, key=lambda s: abs(s["error_db"]), default=None)
    max_err = abs(worst["error_db"]) if worst else 0.0
    span = (usable_steps[0]["measured_db"] - usable_steps[-1]["measured_db"]) if usable_steps else 0.0
    disturbed = [s for s in steps if s["disturbed"]]
    recs = []
    if len(usable_steps) < 3:
        verdict = "warn"
        summary = (f"Only {len(usable_steps)} clean step(s) above the {background:.1f} dB background — "
                   "turn the volume up or pick a quieter moment, then re-run.")
    elif max_err <= 1.0:
        verdict = "pass"
        summary = f"Linear within ±{max_err:.1f} dB over {span:.0f} dB ({len(usable_steps)} clean steps)."
    elif max_err <= 2.0:
        verdict = "warn"
        summary = f"Linearity error up to {max_err:.1f} dB over {span:.0f} dB — fine for trends, not for evidence."
    else:
        verdict = "fail"
        where = worst["label"].split("·")[-1].strip()
        summary = f"Linearity error up to {max_err:.1f} dB (worst at {where}) over {span:.0f} dB."
    if disturbed:
        if verdict == "pass":
            verdict = "warn"
        names = ", ".join(s["label"].split("·")[-1].strip() for s in disturbed)
        summary += f" {len(disturbed)} step(s) disturbed by outside noise ({names}) and ignored."
        recs.append("Something else was audible during the test (traffic, voices). Re-run in a quieter moment "
                    "for a complete result.")
    if usable_steps and usable_steps[0]["error_db"] < -1.0:
        recs.append("The loudest step reads low: the speaker is probably distorting — lower the volume a little.")
    if worst and worst is not usable_steps[0] and worst["error_db"] > 1.0:
        recs.append("Low steps read high: the background adds to them; more volume or a quieter room helps.")
    if floor_reached:
        floor_step = next(s for s in steps if s["above_background_db"] < 10.0)
        recs.append(f"Readings merge into the background from {floor_step['label'].split('·')[-1].strip()} — "
                    f"that is the bottom of the usable range in this room.")
    return {
        "verdict": verdict,
        "summary": summary,
        "metrics": {
            "background_db": round(background, 1),
            "max_error_db": round(max_err, 2),
            "usable_span_db": round(span, 1),
            "alignment_lag_s": lag,
            "alignment_score": round(corr, 3),
            **timing,
        },
        "steps": steps,
        "recommendations": recs,
    }


IEC_BURST_DELTA = {"Fast": -1.0, "Slow": -7.4}


def analyze_response(samples, t0, program, ctx) -> dict:
    lag, corr = find_alignment(program, samples, t0, ctx.get("max_lag_s", 6.0))
    timing = sample_rate_stats(samples)
    if corr < 0.4:
        return _not_detected(corr, timing)
    steady_seg = next(s for s in program.segments if s.role == "steady")
    steady = median(_segment_values(samples, t0, lag, steady_seg, trim_in=1.5, trim_out=0.2))
    background = median([v for s in program.segments if s.role == "background"
                         for v in _segment_values(samples, t0, lag, s)])
    if steady is None:
        return _not_detected(corr, timing)
    # Rise time: first reading within 1 dB of the steady level after onset.
    onset = t0 + lag + steady_seg.start
    rise = next((t - onset for t, v in samples if onset <= t <= onset + steady_seg.duration and v >= steady - 1.0), None)
    bursts = []
    for seg in [s for s in program.segments if s.role == "burst"]:
        a = t0 + lag + seg.start - 0.15
        b = t0 + lag + seg.start + 1.5
        vals = [v for t, v in samples if a <= t <= b]
        if vals:
            bursts.append({"label": seg.label, "peak_db": round(max(vals), 1), "delta_db": round(max(vals) - steady, 1)})
    rate = timing["rate_hz"] or 0.0
    metrics = {
        "steady_db": round(steady, 1),
        "background_db": round_or_none(background),
        "rise_time_s": round(rise, 2) if rise is not None else None,
        "alignment_lag_s": lag,
        "alignment_score": round(corr, 3),
        **timing,
    }
    if not bursts:
        return {"verdict": "fail", "summary": "No burst could be measured.", "metrics": metrics,
                "recommendations": ["Re-run with the volume higher."]}
    avg_delta = statistics.mean(b["delta_db"] for b in bursts)
    metrics["burst_delta_db"] = round(avg_delta, 1)
    if rate < 4.0:
        return {
            "verdict": "warn",
            "summary": f"Inconclusive: the meter only updates {rate} times/s, too slow to catch 200 ms bursts "
                       f"(measured {avg_delta:+.1f} dB vs steady).",
            "metrics": metrics,
            "steps": bursts,
            "recommendations": ["Short impulsive events will be under-read by this meter; rely on LAeq rather than peaks."],
        }
    weighting = min(IEC_BURST_DELTA, key=lambda k: abs(IEC_BURST_DELTA[k] - avg_delta))
    deviation = avg_delta - IEC_BURST_DELTA[weighting]
    metrics["time_weighting"] = weighting
    metrics["deviation_db"] = round(deviation, 1)
    if abs(deviation) <= 1.0:
        verdict = "pass"
        summary = f"Behaves like a {weighting} meter: 200 ms bursts read {avg_delta:+.1f} dB (IEC: {IEC_BURST_DELTA[weighting]:+.1f})."
    elif abs(deviation) <= 2.5:
        verdict = "warn"
        summary = f"Closest to {weighting} but {deviation:+.1f} dB off the IEC burst response ({avg_delta:+.1f} dB)."
    else:
        verdict = "fail"
        summary = f"Unusual burst response ({avg_delta:+.1f} dB): neither Fast nor Slow."
    recs = []
    if weighting == "Slow":
        recs.append("Slow weighting smooths impulsive noise: door slams will read lower than on a Fast meter.")
    return {"verdict": verdict, "summary": summary, "metrics": metrics, "steps": bursts, "recommendations": recs}


def analyze_calibration(samples, t0, program, ctx) -> dict:
    timing = sample_rate_stats(samples)
    if program.mode == "listen":
        values = [v for t, v in samples if t >= t0 + 1.0]
        background = None
        lag, corr = 0.0, 1.0
    else:
        lag, corr = find_alignment(program, samples, t0, ctx.get("max_lag_s", 6.0))
        if corr < 0.4:
            return _not_detected(corr, timing)
        ref_seg = next(s for s in program.segments if s.role == "reference")
        values = _segment_values(samples, t0, lag, ref_seg, trim_in=1.0, trim_out=0.5)
        background = median([v for s in program.segments if s.role == "background"
                             for v in _segment_values(samples, t0, lag, s, trim_in=0.3)])
    if len(values) < 3:
        return _not_detected(corr, timing)
    level = median(values)
    spread = statistics.pstdev(values)
    current_offset = float(ctx.get("calibration_offset_db", 0.0))
    metrics = {
        "measured_db": round(level, 1),
        "spread_db": round(spread, 2),
        "background_db": round_or_none(background),
        "current_offset_db": current_offset,
        "alignment_lag_s": lag,
        **timing,
    }
    recs = []
    verdict = "pass"
    if background is not None and level - background < 15:
        verdict = "warn"
        recs.append("Pink noise is less than 15 dB above background: turn the volume up for a reliable offset.")
    if spread > 2.0:
        verdict = "warn"
        recs.append("Readings were unsteady; keep everything still and re-run.")
    reference = ctx.get("reference_db")
    result = {"verdict": verdict, "metrics": metrics, "recommendations": recs, "needs_reference": reference is None}
    if reference is None:
        result["summary"] = f"Measured {level:.1f} dB. Enter what your reference showed to compute the offset."
    else:
        result.update(apply_reference(result, float(reference)))
    return result


def apply_reference(result: dict, reference_db: float) -> dict:
    """Combine a calibration measurement with the reference reading entered by the user."""
    metrics = dict(result.get("metrics") or {})
    measured = metrics["measured_db"]
    current = float(metrics.get("current_offset_db", 0.0))
    delta = reference_db - measured
    proposed = round(current + delta, 1)
    metrics.update({"reference_db": reference_db, "correction_db": round(delta, 1), "proposed_offset_db": proposed})
    recs = list(result.get("recommendations") or [])
    verdict = result.get("verdict", "pass")
    if abs(proposed) > 10:
        verdict = "warn"
        recs.append("An offset above 10 dB is suspicious — check that both meters use A-weighting and the same units.")
    summary = (f"Box read {measured:.1f} dB, reference {reference_db:.1f} dB → "
               f"{'+' if delta >= 0 else ''}{delta:.1f} dB correction (new offset {proposed:+.1f} dB).")
    return {
        "verdict": verdict,
        "summary": summary,
        "metrics": metrics,
        "recommendations": recs,
        "needs_reference": False,
        "apply": {"calibration_offset_db": proposed},
    }


def analyze_ambient(samples, t0, program, ctx) -> dict:
    values = [v for _, v in samples]
    timing = sample_rate_stats(samples)
    if len(values) < 10:
        return {"verdict": "fail", "summary": "Not enough readings to survey the environment.", "metrics": timing,
                "recommendations": ["Check the meter connection and re-run."]}
    now_stats = exceedance_levels(values)
    hist = ctx.get("history_levels") or {}
    day = hist.get("day") or now_stats
    night = hist.get("night")
    l10, l50, l90 = day["l10"], day["l50"], day["l90"]
    event_trigger = _clamp(round(max(l10 + 15.0, l90 + 25.0)), 55, 100)
    episode_day = _clamp(round(l50 + 10.0), 45, 90)
    if night:
        episode_night = _clamp(round(night["l50"] + 8.0), 40, episode_day)
    else:
        episode_night = _clamp(episode_day - 10, 40, episode_day)
    basis = "your last days of history + this survey" if hist.get("day") else "this survey only (history fills in after a day)"
    return {
        "verdict": "pass",
        "summary": f"Background L90 {now_stats['l90']:.1f} dB, typical L50 {now_stats['l50']:.1f} dB, busy L10 "
                   f"{now_stats['l10']:.1f} dB. Recommended thresholds based on {basis}.",
        "metrics": {
            "laeq_db": round_or_none(leq(values)),
            "l10_db": round_or_none(now_stats["l10"]),
            "l50_db": round_or_none(now_stats["l50"]),
            "l90_db": round_or_none(now_stats["l90"]),
            "history_day": hist.get("day"),
            "history_night": night,
            **timing,
        },
        "recommendations": [
            f"Event trigger {event_trigger} dB: catches peaks ~15 dB above your busy level.",
            f"Episode threshold {episode_day} dB by day / {episode_night} dB at night (1-min L50).",
        ],
        "apply": {
            "event_trigger_db": float(event_trigger),
            "episode_db": float(episode_day),
            "episode_night_db": float(episode_night),
        },
    }


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _not_detected(corr: float, timing: dict) -> dict:
    return {
        "verdict": "fail",
        "summary": "The test signal was not detected by the meter.",
        "metrics": {"alignment_score": round(corr, 3), **timing},
        "recommendations": [
            "Turn the phone volume up (and the silent switch off on iPhone).",
            "Hold the speaker 5–10 cm from the meter microphone.",
            "Keep the dashboard tab in the foreground while the test runs.",
        ],
    }


ANALYZERS: Dict[str, Callable] = {
    "selftest": analyze_selftest,
    "linearity": analyze_linearity,
    "response": analyze_response,
    "calibration": analyze_calibration,
    "calibrator": analyze_calibration,
    "ambient": analyze_ambient,
}


# ---------------------------------------------------------------------------------------
# Runner — records the meter while a program plays, then analyses in the background.
# ---------------------------------------------------------------------------------------
class LabRun:
    TAIL_S = 4.0

    def __init__(self, program: Program, params: dict):
        self.id = uuid.uuid4().hex[:10]
        self.program = program
        self.params = dict(params or {})
        self.created = time.time()
        self.t0 = self.created
        self.status = "capturing"
        self.samples: List[Tuple[float, float]] = []
        self.result: Optional[dict] = None
        self.error: Optional[str] = None
        self.db_id: Optional[int] = None
        self.listen_s = float(self.params.get("listen_s") or program.duration)

    @property
    def capture_end(self) -> float:
        tail = self.TAIL_S if self.program.mode == "play" else 0.0
        if self.params.get("manual"):
            tail += 20.0
        return self.t0 + self.listen_s + tail

    def to_dict(self, include_samples: bool = False) -> dict:
        now = time.time()
        data = {
            "id": self.id,
            "test": self.program.id,
            "name": self.program.name,
            "status": self.status,
            "t0": self.t0,
            "elapsed": round(now - self.t0, 2),
            "duration": round(self.capture_end - self.t0, 2),
            "result": self.result,
            "error": self.error,
            "db_id": self.db_id,
            "readings": len(self.samples),
        }
        if include_samples:
            data["samples"] = [[round(t - self.t0, 2), v] for t, v in self.samples]
        return data


class LabRunner:
    def __init__(self, store=None, settings=None, notifier=None, logger=None,
                 history_levels: Optional[Callable[[], dict]] = None):
        self.store = store
        self.settings = settings
        self.notifier = notifier
        self.logger = logger
        self.history_levels = history_levels
        self._lock = threading.Lock()
        self.current: Optional[LabRun] = None
        self.recent: Dict[str, LabRun] = {}

    def start(self, program_id: str, params: Optional[dict] = None) -> LabRun:
        program = PROGRAMS.get(program_id)
        if program is None:
            raise KeyError(f"unknown test: {program_id}")
        params = dict(params or {})
        if program.id == "ambient" and params.get("listen_s"):
            params["listen_s"] = max(30.0, min(1800.0, float(params["listen_s"])))
        with self._lock:
            if self.current and self.current.status == "capturing":
                raise RuntimeError("another test is already running")
            run = LabRun(program, params)
            self.current = run
            self.recent[run.id] = run
            if len(self.recent) > 20:
                oldest = min(self.recent.values(), key=lambda r: r.created)
                self.recent.pop(oldest.id, None)
        if self.logger:
            self.logger.info(f"[LAB] test '{program.id}' started (run {run.id})")
        return run

    def cancel(self, run_id: str) -> bool:
        with self._lock:
            run = self.recent.get(run_id)
            if not run or run.status != "capturing":
                return False
            run.status = "cancelled"
            if self.current is run:
                self.current = None
            return True

    def in_test_window(self, ts: float) -> bool:
        """
        True if a test signal may have been audible at `ts` (used to keep lab sounds out of the
        history and the alerts). The whole capture window counts, even for cancelled runs.
        """
        for run in list(self.recent.values()):
            if run.t0 - 1.0 <= ts <= run.capture_end:
                return True
        return False

    def get(self, run_id: str) -> Optional[LabRun]:
        return self.recent.get(run_id)

    def on_sample(self, ts: float, db: float):
        """Called by the hub for every (calibrated) reading."""
        run = self.current
        if run is None or run.status != "capturing":
            return
        if ts >= run.t0 - 1.0:
            run.samples.append((ts, db))

    def tick(self, now: Optional[float] = None):
        """Called periodically by the hub: closes captures that are due and analyses them."""
        run = self.current
        if run is None or run.status != "capturing":
            return
        now = now or time.time()
        if now >= run.capture_end:
            with self._lock:
                run.status = "analyzing"
                self.current = None
            threading.Thread(target=self._analyze, args=(run,), name="mutelab-analyze", daemon=True).start()

    def analyze_now(self, run: LabRun):
        """Synchronous analysis (used by tests)."""
        run.status = "analyzing"
        if self.current is run:
            self.current = None
        self._analyze(run)

    def _context(self, run: LabRun) -> dict:
        ctx = {
            "max_lag_s": 25.0 if run.params.get("manual") else 6.0,
            "reference_db": run.params.get("reference_db"),
            "calibration_offset_db": self.settings.get("calibration_offset_db") if self.settings else 0.0,
        }
        if run.program.id == "ambient" and self.history_levels:
            try:
                ctx["history_levels"] = self.history_levels()
            except Exception:
                ctx["history_levels"] = None
        return ctx

    def _analyze(self, run: LabRun):
        try:
            analyzer = ANALYZERS[run.program.id]
            result = analyzer(list(run.samples), run.t0, run.program, self._context(run))
            result["test"] = run.program.id
            result["name"] = run.program.name
            result["samples"] = [[round(t - run.t0, 2), v] for t, v in run.samples][-3000:]
            run.result = result
            run.status = "done"
            if self.store:
                run.db_id = self.store.add_lab_run(run.program.id, result["verdict"], result["summary"], result, ts=run.t0)
            if self.notifier and not result.get("needs_reference"):
                icon = {"pass": "✅", "warn": "⚠️", "fail": "❌"}.get(result["verdict"], "")
                self.notifier.send("lab", f"{icon} {run.program.name}", result["summary"], tags=["test_tube"])
            if self.logger:
                self.logger.info(f"[LAB] {run.program.id}: {result['verdict']} — {result['summary']}")
        except Exception as exc:
            run.status = "error"
            run.error = str(exc)
            if self.logger:
                self.logger.warning(f"[LAB] analysis of {run.program.id} failed: {exc}")

    def set_reference(self, run_id: str, reference_db: float) -> dict:
        run = self.recent.get(run_id)
        if not run or not run.result:
            raise KeyError("run not found or not analysed yet")
        if run.program.id not in ("calibration", "calibrator"):
            raise ValueError("reference values only apply to calibration runs")
        if not 30.0 <= reference_db <= 140.0:
            raise ValueError("reference must be between 30 and 140 dB")
        updated = dict(run.result)
        updated.update(apply_reference(run.result, reference_db))
        run.result = updated
        if self.store:
            run.db_id = self.store.add_lab_run(run.program.id, updated["verdict"], updated["summary"], updated, ts=run.t0)
        return updated

    def playback_level(self, t: float, top_db: float = 88.0, delay_s: float = 0.35) -> Optional[float]:
        """Simulator hook: SPL that the current program would produce at time t (None when silent)."""
        run = self.current
        if run is None or run.status != "capturing":
            return None
        rel = t - run.t0 - delay_s
        if run.program.id == "calibrator" and rel >= 0:
            return 94.0 - 1.3  # demo: a calibrator on a meter that reads 1.3 dB low
        if run.program.mode != "play":
            return None
        for seg in run.program.segments:
            if seg.audible and seg.start <= rel < seg.end:
                return top_db + seg.gain_db - (2.0 if seg.kind == "pink" else 0.0)
        return None
