"""
metrics.py
Persistent storage layer for the Network-Aware Adaptive IoT Communication
System.

Two backing stores are written simultaneously:

    * SQLite (metrics.db) -- queryable, used by the dashboard via /metrics
    * CSV    (metrics.csv) -- append-only export, easy to open in Excel /
                              pandas for reports and viva analysis.

Schema
------
    id                 INTEGER PRIMARY KEY
    timestamp          REAL    -- Unix epoch (seconds, float)
    deviceId           TEXT
    seq                INTEGER -- ESP32 sequence number
    rssi               INTEGER -- dBm
    rttMs              REAL    -- round-trip time in milliseconds
    lossPct            REAL    -- packet-loss percent over last window
    throughputKbps     REAL    -- payload bits / RTT
    currentInterval    INTEGER -- interval the ESP32 was using when it sent
    currentPayloadSize INTEGER -- payload size the ESP32 was using
    state              TEXT    -- decision engine output: Good/Moderate/Poor
    interval           INTEGER -- next interval decided by the engine
    packetSize         INTEGER -- next packet size decided by the engine
"""

from __future__ import annotations

import csv
import os
import sqlite3
import threading
from typing import Dict, List, Optional


class MetricsStore:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS readings (
      id                 INTEGER PRIMARY KEY AUTOINCREMENT,
      timestamp          REAL    NOT NULL,
      deviceId           TEXT    NOT NULL,
      seq                INTEGER,
      rssi               INTEGER,
      rttMs              REAL,
      lossPct            REAL,
      throughputKbps     REAL,
      currentInterval    INTEGER,
      currentPayloadSize INTEGER,
      state              TEXT,
      interval           INTEGER,
      packetSize         INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_readings_ts     ON readings(timestamp);
    CREATE INDEX IF NOT EXISTS idx_readings_device ON readings(deviceId, timestamp);
    """

    CSV_COLUMNS = [
        "timestamp", "deviceId", "seq", "rssi", "rttMs", "lossPct",
        "throughputKbps", "currentInterval", "currentPayloadSize",
        "state", "interval", "packetSize",
    ]

    def __init__(self, db_path: str, csv_path: Optional[str] = None) -> None:
        self.db_path  = db_path
        self.csv_path = csv_path
        self._lock    = threading.Lock()
        self._init_db()
        if csv_path and not os.path.exists(csv_path):
            with open(csv_path, "w", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerow(self.CSV_COLUMNS)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._conn() as c:
            c.executescript(self.SCHEMA)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def append(self, reading: Dict) -> None:
        """Insert a reading into SQLite and append a row to the CSV mirror."""
        with self._lock:
            with self._conn() as c:
                c.execute(
                    """
                    INSERT INTO readings (
                        timestamp, deviceId, seq, rssi, rttMs, lossPct,
                        throughputKbps, currentInterval, currentPayloadSize,
                        state, interval, packetSize
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        reading.get("timestamp"),
                        reading.get("deviceId"),
                        reading.get("seq", 0),
                        reading.get("rssi", 0),
                        reading.get("rttMs", 0.0),
                        reading.get("lossPct", 0.0),
                        reading.get("throughputKbps", 0.0),
                        reading.get("currentInterval", 0),
                        reading.get("currentPayloadSize", 0),
                        reading.get("state"),
                        reading.get("interval"),
                        reading.get("packetSize"),
                    ),
                )

            if self.csv_path:
                with open(self.csv_path, "a", newline="", encoding="utf-8") as fh:
                    csv.writer(fh).writerow(
                        [reading.get(col, "") for col in self.CSV_COLUMNS]
                    )

    def recent(self, n: int = 200) -> List[Dict]:
        """
        Return the most recent `n` readings in chronological order
        (oldest -> newest), as a list of plain dicts.
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM readings ORDER BY id DESC LIMIT ?",
                (n,),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def count(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM readings").fetchone()[0]

    def clear(self) -> None:
        """Wipe history (useful between demo runs)."""
        with self._lock:
            with self._conn() as c:
                c.execute("DELETE FROM readings")
            if self.csv_path and os.path.exists(self.csv_path):
                with open(self.csv_path, "w", newline="", encoding="utf-8") as fh:
                    csv.writer(fh).writerow(self.CSV_COLUMNS)
