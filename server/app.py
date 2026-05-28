"""
app.py
Flask server for the Network-Aware Adaptive IoT Communication System.

Endpoints
---------
POST /data       Receive ESP32 telemetry, run adaptive decision engine,
                 return new transmission parameters as JSON.
GET  /metrics    Return the most recent N readings as JSON for the dashboard.
GET  /latest     Return the most recent single reading.
GET  /health     Liveness probe + record count.
GET  /chaos      Inspect current network-condition injection state.
POST /chaos      Configure injection (delay_ms + drop_pct) for viva demos.
GET  /           Serve the dashboard (../dashboard/index.html).

Architecture
------------
This module is intentionally thin. All adaptation logic lives in decision.py
and all persistence lives in metrics.py, so the design stays modular and
testable. Adding ML or a different storage backend later requires changing
exactly one file.
"""

from __future__ import annotations

import logging
import os
import random
import time

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

from decision import AdaptiveDecisionEngine
from metrics import MetricsStore

# ----------------------------------------------------------------------------
# Paths
# ----------------------------------------------------------------------------
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_DIR = os.path.normpath(os.path.join(BASE_DIR, "..", "dashboard"))
DATA_DIR      = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

# ----------------------------------------------------------------------------
# Application setup
# ----------------------------------------------------------------------------
app = Flask(__name__, static_folder=DASHBOARD_DIR, static_url_path="")
CORS(app)  # allow the dashboard JS to fetch /metrics if served from elsewhere

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("server")

store  = MetricsStore(
    db_path=os.path.join(DATA_DIR, "metrics.db"),
    csv_path=os.path.join(DATA_DIR, "metrics.csv"),
)
engine = AdaptiveDecisionEngine()

# ----------------------------------------------------------------------------
# Chaos injection (viva demo feature)
# ----------------------------------------------------------------------------
# `delay_ms` is added with time.sleep() before responding to /data, which
# inflates the RTT the ESP32 measures. `drop_pct` causes /data to randomly
# return HTTP 503 (no ACK), which the ESP32 counts as a lost packet in its
# sliding window. Together they let us demonstrate the adaptive engine's
# Good -> Moderate -> Poor transition on demand.
_chaos = {"delay_ms": 0, "drop_pct": 0.0}


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------
@app.route("/")
def index():
    """Serve the dashboard index page."""
    return send_from_directory(DASHBOARD_DIR, "index.html")


@app.route("/data", methods=["POST"])
def receive_data():
    """
    Ingest telemetry from an ESP32 node, run the adaptive decision engine, and
    return the new transmission parameters the device should apply on its next
    cycle.

    Expected JSON body (from the ESP32):
        {
          "deviceId":           "esp32-node-01",
          "seq":                42,
          "rssi":               -63,
          "lastRttMs":          120.0,
          "lossPct":            2.5,
          "throughputKbps":     54.3,
          "currentInterval":    5000,
          "currentPayloadSize": 128
        }
    """
    payload = request.get_json(silent=True) or {}
    if "deviceId" not in payload or "rssi" not in payload:
        return jsonify({"error": "missing required fields (deviceId, rssi)"}), 400

    # ---- Chaos injection (only active when configured via /chaos) ----
    if _chaos["delay_ms"] > 0:
        time.sleep(_chaos["delay_ms"] / 1000.0)
    if _chaos["drop_pct"] > 0 and (random.random() * 100.0) < _chaos["drop_pct"]:
        # Simulate a packet drop: respond with no ACK so the ESP32 counts a loss.
        return jsonify({"error": "simulated drop"}), 503

    reading = {
        "timestamp":          time.time(),
        "deviceId":           str(payload.get("deviceId")),
        "seq":                _safe_int(payload.get("seq")),
        "rssi":               _safe_int(payload.get("rssi")),
        "rttMs":              _safe_float(payload.get("lastRttMs")),
        "lossPct":            _safe_float(payload.get("lossPct")),
        "throughputKbps":     _safe_float(payload.get("throughputKbps")),
        "currentInterval":    _safe_int(payload.get("currentInterval")),
        "currentPayloadSize": _safe_int(payload.get("currentPayloadSize")),
    }

    decision = engine.decide(reading)

    # Attach the engine's output to the row so the dashboard sees the most
    # recent adaptive parameters alongside the raw metrics.
    reading.update({
        "state":      decision["state"],
        "interval":   decision["interval"],
        "packetSize": decision["packetSize"],
    })

    store.append(reading)

    log.info(
        "data device=%s seq=%d rssi=%d rtt=%.1fms loss=%.1f%% th=%.2fKbps "
        "-> state=%s interval=%d size=%d (conf=%.2f score=%.3f)",
        reading["deviceId"], reading["seq"], reading["rssi"],
        reading["rttMs"], reading["lossPct"], reading["throughputKbps"],
        decision["state"], decision["interval"], decision["packetSize"],
        decision["confidence"], decision["score"],
    )

    return jsonify(decision)


@app.route("/metrics")
def metrics():
    """Return up to ?limit=N most recent readings (default 200)."""
    n = _safe_int(request.args.get("limit"), 200)
    n = max(1, min(n, 2000))
    return jsonify(store.recent(n))


@app.route("/latest")
def latest():
    rows = store.recent(1)
    return jsonify(rows[-1] if rows else {})


@app.route("/health")
def health():
    return jsonify({"ok": True, "records": store.count()})


@app.route("/chaos", methods=["GET", "POST"])
def chaos():
    """
    Inspect or update the chaos-injection state.

    POST body (JSON):
        { "delay_ms": <int>, "drop_pct": <float 0..100> }

    Either field is optional; omitted fields are left unchanged. Send both
    as 0 to disable injection.
    """
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        if "delay_ms" in data:
            _chaos["delay_ms"] = max(0, _safe_int(data.get("delay_ms"), 0))
        if "drop_pct" in data:
            pct = _safe_float(data.get("drop_pct"), 0.0)
            _chaos["drop_pct"] = max(0.0, min(100.0, pct))
        log.info("CHAOS update: delay_ms=%d drop_pct=%.1f",
                 _chaos["delay_ms"], _chaos["drop_pct"])
    return jsonify(_chaos)


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    port = _safe_int(os.environ.get("PORT"), 5000)
    log.info("Adaptive IoT server starting on 0.0.0.0:%d", port)
    log.info("Dashboard available at http://localhost:%d/", port)
    app.run(host="0.0.0.0", port=port, threaded=True)
