/*
  main.ino
  Network-Aware Adaptive IoT Communication System
  ESP32 Node Firmware

  Responsibilities (per SRS / Design doc):
    1. Connect / auto-reconnect to WiFi (non-blocking)
    2. Measure network metrics:
         - RTT (HTTP request-response timing)
         - RSSI (WiFi.RSSI())
         - Packet loss (sliding window of ACKs)
         - Throughput (bytes / RTT, Kbps)
    3. Send JSON telemetry to Flask server at periodic interval
    4. Receive adaptive parameters (interval, packetSize, state) in response
    5. Apply parameters dynamically on the next transmission
    6. Print structured logs to Serial @115200 for viva demonstration

  Required libraries (Arduino Library Manager):
    - WiFi              (bundled with ESP32 core)
    - HTTPClient        (bundled with ESP32 core)
    - ArduinoJson       (Benoit Blanchon)  -- v6 or v7 both work
*/

#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>

// ================== USER CONFIGURATION ==================
// Wi-Fi credentials and server URL are kept in "secrets.h" (gitignored).
// First-time setup: copy "secrets.example.h" to "secrets.h" and fill in your values.
#include "secrets.h"

const char* DEVICE_ID     = "esp32-node-01";

// ================== ADAPTIVE PARAMETER BOUNDS ==================
// These bounds must mirror the server-side decision engine.
const uint32_t MIN_INTERVAL_MS = 1000;     // fastest TX cadence
const uint32_t MAX_INTERVAL_MS = 30000;    // slowest TX cadence
const uint16_t MIN_PAYLOAD     = 32;       // smallest packet payload
const uint16_t MAX_PAYLOAD     = 1024;     // largest packet payload

// ================== INITIAL STATE ==================
// Start with a "Moderate" profile; the server will adjust within seconds.
uint32_t txInterval  = 5000;
uint16_t payloadSize = 128;
String   networkState = "Initializing";

// ================== METRICS STATE ==================
uint32_t packetsSent   = 0;
uint32_t packetsAcked  = 0;

// Packet-loss is computed over a true sliding window of the most recent N
// transmissions using a ring buffer. This updates the loss percentage on
// *every* transmission instead of every N transmissions, so chaos / congestion
// shows up on the dashboard within a couple of seconds.
const uint8_t LOSS_WINDOW = 10;
bool     ackRing[LOSS_WINDOW] = {false};
uint8_t  ackRingIdx           = 0;
uint8_t  ackRingFilled        = 0;

float    lastRttMs        = 0.0f;
int      lastRssiDbm      = 0;
float    lastLossPct      = 0.0f;
float    lastThroughputKb = 0.0f;

// ================== TIMING ==================
unsigned long lastTxMs      = 0;
unsigned long lastWifiRetry = 0;
const unsigned long WIFI_RETRY_INTERVAL = 5000;
const unsigned long HTTP_TIMEOUT_MS     = 8000;

// ================== FORWARD DECLARATIONS ==================
void   connectWiFi();
void   ensureWiFi();
bool   transmitAndAdapt();
String buildPayload();
void   applyServerDecision(const String& body);
String makePadding(uint16_t size);
void   logTransmission(int httpCode, bool ok);

// ================== SETUP ==================
void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println();
  Serial.println(F("============================================="));
  Serial.println(F(" Network-Aware Adaptive IoT Node (ESP32)"));
  Serial.println(F("============================================="));
  Serial.printf("Device ID    : %s\n", DEVICE_ID);
  Serial.printf("Server URL   : %s\n", SERVER_URL);
  Serial.printf("Initial int  : %u ms\n", txInterval);
  Serial.printf("Initial size : %u bytes\n", payloadSize);
  Serial.println();

  connectWiFi();
}

// ================== MAIN LOOP (non-blocking) ==================
void loop() {
  ensureWiFi();

  const unsigned long now = millis();
  if (WiFi.status() == WL_CONNECTED && (now - lastTxMs) >= txInterval) {
    lastTxMs = now;
    transmitAndAdapt();
  }
  // Other cooperative tasks could be added here (sensors, OTA, etc.)
}

// ================== WIFI: INITIAL CONNECT ==================
void connectWiFi() {
  Serial.printf("[WiFi] Connecting to SSID \"%s\"...\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.persistent(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  const unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && (millis() - start) < 20000) {
    delay(300);
    Serial.print('.');
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[WiFi] Connected. IP=%s  RSSI=%d dBm\n",
                  WiFi.localIP().toString().c_str(),
                  WiFi.RSSI());
  } else {
    Serial.println(F("[WiFi] Initial connection failed; will retry in loop()."));
  }
}

// ================== WIFI: NON-BLOCKING WATCHDOG ==================
void ensureWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;

  const unsigned long now = millis();
  if (now - lastWifiRetry < WIFI_RETRY_INTERVAL) return;
  lastWifiRetry = now;

  Serial.println(F("[WiFi] Link down; issuing reconnect..."));
  WiFi.disconnect();
  WiFi.reconnect();
}

// ================== PAYLOAD CONSTRUCTION ==================
// Pads the JSON with 'x' characters so the encoded payload approaches the
// adaptive packetSize requested by the server.
String makePadding(uint16_t size) {
  String s;
  s.reserve(size);
  for (uint16_t i = 0; i < size; ++i) s += 'x';
  return s;
}

String buildPayload() {
  StaticJsonDocument<2048> doc;
  doc["deviceId"]           = DEVICE_ID;
  doc["seq"]                = packetsSent + 1;
  doc["rssi"]               = WiFi.RSSI();
  doc["lastRttMs"]          = lastRttMs;
  doc["lossPct"]            = lastLossPct;
  doc["throughputKbps"]     = lastThroughputKb;
  doc["currentInterval"]    = txInterval;
  doc["currentPayloadSize"] = payloadSize;

  // Approx. overhead of the JSON keys above is ~96 bytes; pad the rest.
  const uint16_t overhead = 96;
  uint16_t padLen = (payloadSize > overhead) ? (payloadSize - overhead) : 0;
  doc["pad"] = makePadding(padLen);

  String out;
  serializeJson(doc, out);
  return out;
}

// ================== TRANSMIT + MEASURE + ADAPT ==================
bool transmitAndAdapt() {
  if (WiFi.status() != WL_CONNECTED) return false;

  HTTPClient http;
  http.setTimeout(HTTP_TIMEOUT_MS);
  http.setConnectTimeout(HTTP_TIMEOUT_MS);
  if (!http.begin(SERVER_URL)) {
    Serial.println(F("[HTTP] begin() failed"));
    return false;
  }
  http.addHeader("Content-Type", "application/json");

  const String payload = buildPayload();
  const uint32_t bytes = payload.length();

  ++packetsSent;

  // ---- RTT measurement (request -> response) ----
  const unsigned long t0 = millis();
  const int httpCode     = http.POST(payload);
  const unsigned long t1 = millis();
  const bool ok          = (httpCode >= 200 && httpCode < 300);

  if (ok) {
    ++packetsAcked;

    lastRttMs = static_cast<float>(t1 - t0);
    // Throughput in Kbps:  bytes * 8 bits / (ms / 1000) / 1000 = bytes*8/ms
    if (lastRttMs > 0.0f) {
      lastThroughputKb = (bytes * 8.0f) / lastRttMs;
    }

    const String body = http.getString();
    applyServerDecision(body);
  } else {
    Serial.printf("[HTTP] POST failed: code=%d msg=%s\n",
                  httpCode, http.errorToString(httpCode).c_str());
  }

  // ---- Packet-loss: ring-buffer sliding window over the last N transmissions ----
  ackRing[ackRingIdx] = ok;
  ackRingIdx = (ackRingIdx + 1) % LOSS_WINDOW;
  if (ackRingFilled < LOSS_WINDOW) ++ackRingFilled;

  uint8_t losses = 0;
  for (uint8_t i = 0; i < ackRingFilled; ++i) {
    if (!ackRing[i]) ++losses;
  }
  lastLossPct = 100.0f * static_cast<float>(losses) / static_cast<float>(ackRingFilled);

  lastRssiDbm = WiFi.RSSI();
  logTransmission(httpCode, ok);
  http.end();
  return ok;
}

// ================== APPLY ADAPTIVE PARAMETERS FROM SERVER ==================
void applyServerDecision(const String& body) {
  StaticJsonDocument<512> resp;
  DeserializationError err = deserializeJson(resp, body);
  if (err) {
    Serial.printf("[JSON] parse error: %s\n", err.c_str());
    return;
  }

  if (resp["interval"].is<uint32_t>()) {
    uint32_t v = resp["interval"].as<uint32_t>();
    if (v >= MIN_INTERVAL_MS && v <= MAX_INTERVAL_MS) {
      txInterval = v;
    }
  }
  if (resp["packetSize"].is<uint16_t>()) {
    uint16_t v = resp["packetSize"].as<uint16_t>();
    if (v >= MIN_PAYLOAD && v <= MAX_PAYLOAD) {
      payloadSize = v;
    }
  }
  if (resp["state"].is<const char*>()) {
    networkState = String(resp["state"].as<const char*>());
  }
}

// ================== STRUCTURED LOGGING ==================
void logTransmission(int httpCode, bool ok) {
  Serial.printf(
    "[TX %lu] code=%-3d  rtt=%6.1f ms  rssi=%4d dBm  loss=%5.1f %%  th=%6.2f Kbps  "
    "int=%5lu ms  size=%4u B  state=%s  ack=%lu/%lu\n",
    static_cast<unsigned long>(packetsSent),
    httpCode,
    lastRttMs,
    lastRssiDbm,
    lastLossPct,
    lastThroughputKb,
    static_cast<unsigned long>(txInterval),
    static_cast<unsigned int>(payloadSize),
    networkState.c_str(),
    static_cast<unsigned long>(packetsAcked),
    static_cast<unsigned long>(packetsSent)
  );
}
