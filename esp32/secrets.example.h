// secrets.example.h
//
// Template for per-developer credentials.
//
// First-time setup:
//   1. Copy this file to "secrets.h" in the same folder.
//   2. Edit secrets.h and fill in your Wi-Fi and server values.
//
// secrets.h is gitignored, so your credentials never leave your machine.

#ifndef NACP1_SECRETS_H
#define NACP1_SECRETS_H

// Wi-Fi network the ESP32 should join.
// Must be the same network your laptop (running the Flask server) is on.
#define WIFI_SSID      "YOUR_WIFI_SSID"
#define WIFI_PASSWORD  "YOUR_WIFI_PASSWORD"

// URL of the Flask server's /data endpoint.
// Use your laptop's LAN IP -- NOT 127.0.0.1 (the ESP32 cannot reach loopback).
// On Windows, find it with:
//   Get-NetIPAddress -AddressFamily IPv4 | ? InterfaceAlias -eq 'Wi-Fi'
#define SERVER_URL     "http://192.168.1.100:5000/data"

#endif // NACP1_SECRETS_H
