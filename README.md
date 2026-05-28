# Network-Aware Adaptive IoT Communication System

An academic mini-project that demonstrates **network awareness**, **adaptive
communication**, **real-time monitoring**, and **closed-loop feedback control**
on an ESP32 IoT node.

The ESP32 continuously measures live network metrics (RTT, RSSI, packet loss,
throughput) and sends them as JSON to a Python Flask server. The server's
**Adaptive Decision Engine** classifies the network condition and replies with
the optimal transmission parameters (interval, packet size). The ESP32
immediately applies them, and a Chart.js dashboard visualizes every metric and
decision in real time.

---

## 1. Architecture

```
+----------------------------+         JSON / HTTP          +-----------------------------+
|  IoT Device Layer (ESP32)  |  ─── POST /data ──────────▶ |        Server Module        |
|                            |                              |       (Flask, Python)       |
|  - WiFi connect / retry    | ◀── adaptive params (JSON) ─ |  - app.py        (routes)   |
|  - RTT / RSSI / loss / thr |                              |  - decision.py   (engine)   |
|  - Adaptive TX behavior    |                              |  - metrics.py    (storage)  |
+-------------┬--------------+                              +--------------┬--------------+
              │                                                            │
              │ (network metrics)                                          │ (SQLite + CSV)
              ▼                                                            ▼
+----------------------------+                              +-----------------------------+
| Network Monitoring Module  |                              |    Data Storage Layer       |
| (embedded in ESP32 fw)     |                              |  metrics.db  /  metrics.csv |
+----------------------------+                              +--------------┬--------------+
                                                                           │
                                                                           ▼
                                                            +-----------------------------+
                                                            |  Dashboard / Visualization  |
                                                            |   (HTML + CSS + Chart.js)   |
                                                            |   live graphs, KPIs, logs   |
                                                            +-----------------------------+
```

**Feedback loop:** every POST from the ESP32 produces a JSON response carrying
the next `interval`, `packetSize`, and classified `state`. The device applies
them on its very next transmission — this is the adaptive control loop required
by the SRS.

---

## 2. Project structure

```
NACP1/
├── esp32/
│   └── main.ino              ESP32 firmware (Arduino IDE)
├── server/
│   ├── app.py                Flask routes
│   ├── decision.py           Adaptive Decision Engine
│   ├── metrics.py            SQLite + CSV storage
│   ├── requirements.txt      Python deps
│   └── data/                 (auto-created) metrics.db, metrics.csv
├── dashboard/
│   ├── index.html            Dashboard markup
│   ├── style.css             Dark, modern theme
│   └── script.js             Chart.js + polling client
└── README.md                 This file
```

---

## 3. Required libraries & dependencies

### ESP32 (Arduino IDE)

1. **Board package:** ESP32 by Espressif Systems (Boards Manager).
2. **Library Manager:**
   - `ArduinoJson` by Benoit Blanchon (v6 or v7).
   - `WiFi` and `HTTPClient` are bundled with the ESP32 core — no install needed.

### Server (Python 3.9+)

```
Flask>=3.0.0
flask-cors>=4.0.0
```

Install with `pip install -r server/requirements.txt`.

### Dashboard

Pure HTML / CSS / JS. Chart.js is loaded from a CDN — no build step.

---

## 4. Setup instructions

### 4.1 Run the server (PC)

```powershell
cd "c:\Users\RISHI D\Desktop\Projects\NACP1\server"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

You should see:

```
[INFO] Adaptive IoT server starting on 0.0.0.0:5000
[INFO] Dashboard available at http://localhost:5000/
```

Open <http://localhost:5000/> in a browser to view the dashboard.

### 4.2 Find your PC's LAN IP

```powershell
ipconfig
```

Look for the IPv4 address of your active adapter (e.g. `192.168.1.42`). The
ESP32 must be able to reach this address — your PC and ESP32 must be on the
same WiFi network and the Windows Firewall must allow inbound TCP port 5000
(allow Python when prompted).

### 4.3 Flash the ESP32 (Arduino IDE)

1. Open `esp32/main.ino` in the Arduino IDE.
2. **Tools → Board** → select your ESP32 dev board.
3. **Tools → Port** → select the COM port of the ESP32.
4. Install **ArduinoJson** via Library Manager (Tools → Manage Libraries).
5. Edit the top of `main.ino`:

   ```cpp
   const char* WIFI_SSID     = "YOUR_SSID";
   const char* WIFI_PASSWORD = "YOUR_PASSWORD";
   const char* SERVER_URL    = "http://192.168.1.42:5000/data";   // your PC IP
   const char* DEVICE_ID     = "esp32-node-01";
   ```

6. **Upload**.
7. **Tools → Serial Monitor** at **115200** baud.

### 4.4 Watch the system run

- The Serial Monitor prints structured logs per transmission.
- The browser dashboard updates every ~2 seconds.
- The SQLite database and CSV mirror grow under `server/data/`.

---

## 4a. Quick restart (next session)

Open the project in VS Code &mdash; the terminal will be at the project root.

**Check LAN IP** (re-flash if changed):

```powershell
Get-NetIPAddress -AddressFamily IPv4 |
  Where-Object { $_.PrefixOrigin -eq 'Dhcp' -and $_.InterfaceAlias -eq 'Wi-Fi' } |
  Select-Object IPAddress, InterfaceAlias
```

**Start fresh** &mdash; kills old server, wipes logs, hides server, opens dashboard:

```powershell
Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue |
  Select-Object -ExpandProperty OwningProcess -Unique |
  ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
Remove-Item -Force .\server\data\metrics.db  -ErrorAction SilentlyContinue
Remove-Item -Force .\server\data\metrics.csv -ErrorAction SilentlyContinue
Start-Process -WindowStyle Hidden python -ArgumentList "$PWD\server\app.py"
Start-Sleep -Seconds 1
Start-Process "http://localhost:5000/"
```

**Start with live server logs in terminal** (use a second terminal for anything else):

```powershell
python .\server\app.py
```

**Stop server**:

```powershell
Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue |
  Select-Object -ExpandProperty OwningProcess -Unique |
  ForEach-Object { Stop-Process -Id $_ -Force }
```

**Re-add firewall rule** (only if it ever disappears &mdash; admin PowerShell):

```powershell
New-NetFirewallRule -DisplayName "NACP1 Flask ESP32 (TCP 5000)" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 5000 -Profile Any
```

---

## 5. How the adaptive mechanism works

### 5.1 Metric collection on the ESP32

| Metric        | How it is measured                                                    |
| ------------- | --------------------------------------------------------------------- |
| RTT (ms)      | `millis()` delta around `http.POST()` (request → response).           |
| RSSI (dBm)    | `WiFi.RSSI()` — instantaneous WiFi signal strength.                   |
| Packet loss   | Sliding-window count of failed POSTs over `WINDOW_SIZE` (default 10). |
| Throughput    | `payloadBytes * 8 / rttMs` → kilobits per second.                     |

### 5.2 Composite scoring (server)

Each reading is normalized to `[0, 1]` against two thresholds (good edge,
poor edge) and combined into a single health score:

```
score = 0.45 · loss_score + 0.35 · rtt_score + 0.20 · rssi_score
```

Packet loss is weighted highest because it most directly affects reliability.

### 5.3 Classification with hysteresis

```
score ≥ 0.70  → Good
0.40 ≤ score < 0.70 → Moderate
score < 0.40  → Poor
```

The engine **does not flip state on a single noisy sample**. A new state
becomes active only after `MIN_SAMPLES_FOR_SWITCH = 3` consecutive readings
agree. While confidence is low, the engine **retains the previously-issued
parameters** — directly satisfying the "maintain previous parameters if
confidence is low" requirement.

### 5.4 Parameter profiles

| State    | Interval (ms) | Packet size (bytes) | Data-rate hint |
| -------- | ------------- | ------------------- | -------------- |
| Good     | 2 000         | 512                 | high           |
| Moderate | 5 000         | 256                 | normal         |
| Poor     | 10 000        | 64                  | low            |

This matches the design doc's adaptation rules:

- **Reduce packet size during congestion / high loss** ✔
- **Increase packet size in stable conditions** ✔
- **Increase interval during poor conditions** ✔
- **Decrease interval during stable conditions** ✔

### 5.5 Closed-loop feedback

Every `POST /data` response carries:

```json
{
  "state":        "Moderate",
  "score":        0.612,
  "interval":     5000,
  "packetSize":   256,
  "dataRateHint": "normal",
  "confidence":   0.6
}
```

The ESP32 calls `applyServerDecision()` which validates each field against the
firmware's bounds and applies them — that is the feedback control loop.

### 5.6 Future ML integration

`decision.py` exposes a single method, `decide(reading) → dict`. To swap in an
ML classifier later, only that method has to change — `app.py`, `metrics.py`,
the dashboard, and the ESP32 firmware remain untouched.

---

## 6. Networking concepts used

- **Application layer:** HTTP/1.1 with JSON payloads (RESTful style).
- **Transport layer:** TCP — the request/ACK pattern is what gives us our RTT
  and packet-loss measurements at the application level.
- **Link layer / WiFi PHY:** RSSI from `WiFi.RSSI()` (802.11 b/g/n).
- **Congestion awareness:** rising RTT + rising loss is interpreted as
  congestion → the engine **reduces** packet size and **increases** the
  inter-packet interval (AIMD-flavoured, but on application timing instead of
  TCP windows).
- **Reliability:** the loss-tracking sliding window approximates application-level
  packet delivery ratio, which lets the engine react before TCP's own
  retransmissions saturate the link.
- **Throughput estimation:** Kbps derived from payload bytes / RTT — a
  classical application-layer goodput estimate.

---

## 7. End-to-end workflow (matches design flowchart)

1. ESP32 powers on, `Serial` boots at 115 200 baud.
2. Connects to WiFi (auto-reconnect watchdog runs every 5 s).
3. Sends an initial telemetry packet with default parameters (5 000 ms / 128 B).
4. Server classifies the link condition.
5. Server replies with `{ state, interval, packetSize, … }`.
6. ESP32 applies the new parameters.
7. After every `N = 10` transmissions, the firmware recomputes packet loss
   over the sliding window.
8. Server engine sees a significant change → triggers reclassification.
9. New parameters are pushed → ESP32 applies them.
10. Dashboard polls `/metrics` every 2 s and re-renders all six charts plus the
    log feed.
11. SQLite + CSV grow with every record for offline analysis.
12. Loop continues indefinitely; WiFi drops self-heal without a reboot.

---

## 8. Sample outputs

### Serial Monitor (ESP32)

```
=============================================
 Network-Aware Adaptive IoT Node (ESP32)
=============================================
Device ID    : esp32-node-01
Server URL   : http://192.168.1.42:5000/data
Initial int  : 5000 ms
Initial size : 128 bytes

[WiFi] Connecting to SSID "MyHomeWifi"...
.....
[WiFi] Connected. IP=192.168.1.57  RSSI=-58 dBm
[TX 1] code=200  rtt=  92.0 ms  rssi= -58 dBm  loss=  0.0 %  th=  3.13 Kbps  int= 5000 ms  size= 128 B  state=Good      ack=1/1
[TX 2] code=200  rtt= 110.0 ms  rssi= -60 dBm  loss=  0.0 %  th=  2.62 Kbps  int= 2000 ms  size= 512 B  state=Good      ack=2/2
[TX 3] code=200  rtt= 240.0 ms  rssi= -78 dBm  loss=  0.0 %  th=  4.27 Kbps  int= 2000 ms  size= 512 B  state=Good      ack=3/3
[TX 4] code=-1   rtt= 240.0 ms  rssi= -82 dBm  loss=  0.0 %  th=  4.27 Kbps  int= 2000 ms  size= 512 B  state=Good      ack=3/4
[HTTP] POST failed: code=-1 msg=connection refused
...
[TX 12] code=200 rtt= 420.0 ms  rssi= -86 dBm  loss= 30.0 %  th=  1.22 Kbps  int=10000 ms  size=  64 B  state=Poor      ack=8/12
```

### Server log

```
2026-05-16 22:14:01 [INFO] data device=esp32-node-01 seq=1 rssi=-58 rtt=92.0ms loss=0.0% th=3.13Kbps -> state=Good interval=2000 size=512 (conf=0.20 score=0.964)
2026-05-16 22:14:03 [INFO] data device=esp32-node-01 seq=2 rssi=-60 rtt=110.0ms loss=0.0% th=2.62Kbps -> state=Good interval=2000 size=512 (conf=0.40 score=0.918)
...
```

### CSV (`server/data/metrics.csv`)

```
timestamp,deviceId,seq,rssi,rttMs,lossPct,throughputKbps,currentInterval,currentPayloadSize,state,interval,packetSize
1715890441.21,esp32-node-01,1,-58,92.0,0.0,3.13,5000,128,Good,2000,512
1715890443.42,esp32-node-01,2,-60,110.0,0.0,2.62,2000,512,Good,2000,512
...
```

---

## 9. Viva-oriented explanation

**Q: What problem does this project solve?**
> Fixed-interval IoT devices waste bandwidth when the link is healthy and drop
> packets when it isn't. We make the device *network-aware* — it observes RTT,
> RSSI, and loss, and adapts its TX interval and packet size in a closed loop
> with a server-side decision engine.

**Q: Why JSON over HTTP instead of MQTT or raw TCP?**
> HTTP request/response gives us a free ACK and a free RTT measurement on every
> packet — perfect for this academic mini-project. JSON keeps the payload
> human-readable for the viva and lets us pad the body to a target packet size
> trivially. MQTT would obscure RTT and make the demo harder to explain.

**Q: How is packet loss measured?**
> A sliding window of N=10 transmissions. After every window the firmware
> computes `100 × (sent − acked) / sent`. A failed HTTP code or a timeout
> counts as a lost packet.

**Q: What prevents the system from flapping between states?**
> Hysteresis. The engine requires **three** consecutive readings to agree on
> a new state before switching. While confidence is below 1.0 the previously
> issued parameters are retained — explicit "maintain previous parameters if
> confidence is low" behavior.

**Q: Where does congestion-aware behavior come in?**
> When RTT and loss both rise, the composite score drops into the *Poor* band
> and the engine simultaneously **(a)** lengthens the inter-packet interval to
> reduce offered load and **(b)** shrinks the packet to minimize on-air time
> and retransmission cost. That is the textbook adaptive response to
> congestion.

**Q: How is throughput computed?**
> `payloadBytes × 8 / rttMs` gives kilobits per second of application-level
> goodput on the most recent transmission.

**Q: Why SQLite + CSV?**
> SQLite gives the dashboard fast `/metrics` queries; CSV is for exporting
> data into Excel / pandas for the project report.

**Q: How would you extend it?**
> Three obvious extensions: (1) plug an ML classifier into `decision.py`
> without changing any other file; (2) support multiple ESP32 nodes — the
> server already accepts `deviceId` and indexes by it; (3) add MQTT alongside
> HTTP for lower-overhead telemetry while keeping HTTP for control responses.

---

## 10. Inducing congestion for the viva

The system has two complementary ways to demonstrate the Good &rarr; Moderate
&rarr; Poor adaptation. Use whichever the examiner asks for.

### 10.1 Server-side chaos injection (deterministic / repeatable)

Three buttons in the dashboard's **Demo controls** panel — **Healthy**,
**Congested**, **Severe** — POST to a `/chaos` endpoint on the Flask server,
which then:

- adds `time.sleep(delay_ms)` before responding to `/data` &rarr; inflates the
  RTT the ESP32 measures, and
- randomly returns HTTP `503` for `drop_pct` % of requests &rarr; the ESP32
  counts each as a lost packet in its sliding window.

| Button     | delay_ms | drop_pct | Expected end state |
| ---------- | -------- | -------- | ------------------ |
| Healthy    | 0        | 0 %      | Good               |
| Congested  | 350      | 20 %     | Moderate / Poor    |
| Severe     | 700      | 40 %     | Poor               |

You can also drive it from the command line:

```powershell
# Healthy
Invoke-RestMethod -Method POST -Uri http://localhost:5000/chaos `
  -ContentType 'application/json' -Body '{"delay_ms":0,"drop_pct":0}'

# Severe congestion
Invoke-RestMethod -Method POST -Uri http://localhost:5000/chaos `
  -ContentType 'application/json' -Body '{"delay_ms":700,"drop_pct":40}'
```

This is the **recommended way to demo** because it is deterministic and
reproducible: every press produces the same condition, every time.

**Viva justification:** "Server-side injection lets us isolate the adaptive
control logic from real-world noise. We can prove the engine reacts correctly
to a precisely defined stimulus &mdash; this is standard practice when
validating control systems."

### 10.2 Physical / wireless congestion (real, but not repeatable)

If an examiner asks "but can it handle *real* congestion?", switch to one or
more of these:

1. **Distance &amp; obstruction** &mdash; walk the ESP32 to the far end of the
   room, behind a wall, or into a metal box. RSSI will collapse from
   ~&minus;55 dBm to &minus;85 dBm and below; watch the RSSI chart fall and
   the engine drop to Poor as packet errors climb.
2. **Hotspot saturation** &mdash; on your phone, start a speed test
   (Ookla / fast.com), upload a large file to cloud storage, or stream 4K
   video. The hotspot's airtime gets eaten and the ESP32's POST RTT will
   spike from ~80 ms to several hundred ms.
3. **Laptop-side network load** &mdash; with the laptop on the same hotspot,
   start `iperf3`, a Steam download, or run
   `ping -t -l 65500 8.8.8.8` to saturate the local link.
4. **Brief outage demo** &mdash; turn the hotspot off for ~10 seconds. The
   ESP32 logs `[HTTP] POST failed`, `lossPct` climbs to 100 %, then the
   non-blocking WiFi watchdog reconnects and the engine recovers &mdash; this
   demonstrates the **resilience** requirement from the SRS.
5. **Faraday wrap** &mdash; loosely wrap aluminum foil over the ESP32's PCB
   antenna area. Drops RSSI by 20&ndash;30 dBm instantly. Quick visual demo.

### 10.3 Combined demo script (suggested viva flow)

1. Start with **Healthy** for ~30 s &mdash; show flat low RTT, near-zero loss,
   engine pinned to **Good**, interval = 2000 ms, packet = 512 B.
2. Click **Congested** &mdash; watch RTT chart double, loss chart climb,
   engine flip to **Moderate** after the 3-sample hysteresis window.
3. Click **Severe** &mdash; engine flips to **Poor**, interval expands to
   10 000 ms, packet shrinks to 64 B. Explain *why* (congestion-aware
   contraction).
4. Click **Healthy** &mdash; engine waits 3 consistent samples, then recovers
   to **Good**. This proves the feedback loop and the hysteresis logic.
5. If asked about realism, run the **physical** congestion demo (10.2) &mdash;
   walk away with the ESP32 and re-show the same Good &rarr; Poor transition
   driven by genuine RSSI degradation.

---

## 11. Troubleshooting

| Symptom                                | Likely cause / fix                                                |
| -------------------------------------- | ----------------------------------------------------------------- |
| ESP32: `POST failed: code=-1`          | Wrong server IP in `SERVER_URL`, or Windows Firewall blocking 5000 |
| ESP32 stuck at "Connecting to SSID"    | Wrong WiFi password, or 5 GHz-only SSID (ESP32 wants 2.4 GHz)     |
| Dashboard shows "Server unreachable"   | Flask server not running, or browser pointed at wrong port        |
| Charts are empty                       | No ESP32 has POSTed yet — check Serial Monitor logs               |
| Want to reset history                  | Stop the server, delete `server/data/metrics.db` and `.csv`, restart |
| Decision engine never leaves Moderate  | Tune `RTT_*`, `LOSS_*`, `RSSI_*` thresholds at the top of `decision.py` |
