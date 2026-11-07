"""
Mute Lab — local companion for the mute client.

Everything in here runs on the Mute Box itself and keeps data on your network:

- live dashboard for phone / tablet / Home Assistant (stdlib HTTP + Server-Sent Events)
- local history (SQLite): per-minute LAeq / L10 / L50 / L90, events, EU Lden reports
- ntfy push notifications (noise episode start/end, meter health, daily report)
- meter lab: self-test, linearity staircase, burst response, reference calibration,
  ambient survey with auto-tuned thresholds
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
