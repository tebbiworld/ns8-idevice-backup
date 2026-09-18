# Changelog

## 1.0.0 — 2026-09-18

- First release. WiFi backup of iPhones/iPads over the network with
  pymobiledevice3 (connect by IP over classic lockdown TCP 62078, no USB / no
  netmuxd / no mDNS, cross-subnet). Upload a lockdown pairing record (the
  iTunes / Apple Devices `<UDID>.plist`, or a jitterbugpair file) with device
  name, IP and optional backup encryption password; per-device backup with
  retention; **restore** a snapshot back onto the device; device list with
  pairing/backup status; settings UI (EN/DE).
- A held `com.apple.mobile.heartbeat` session keeps the WiFi connection alive
  during backup/restore (required on iOS 26, verified against a physical device).
- Rootless, outbound-only: no Traefik route, no published node ports. Requires
  `pymobiledevice3 lockdown wifi-connections on` once over USB on the device.
