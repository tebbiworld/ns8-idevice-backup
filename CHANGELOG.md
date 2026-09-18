# Changelog

## 1.2.0 — 2026-09-18

- **Self-service portal.** A second web surface (own FQDN, or a path such as
  `/idevice` on a host) where end users log in with their AD/LDAP account and
  manage the backups of **their own** devices only: add a device (pairing
  upload), set the WiFi IP, choose full/incremental, back up, restore, remove.
  Devices are owned per user; the admin still sees and assigns everything in
  cluster-admin. Bilingual (DE/EN, auto from the browser, with a switch) and a
  built-in "how to get the pairing file" guide.
- **Per-device owner/domain** in the registry and an Owner column in
  cluster-admin.
- **LDAP login** is a direct bind with admin-entered credentials (ldaps
  recommended; the container reaches the directory through
  host.containers.internal under pasta when it is node-local).
- **Restore self-service** is on by default and can be limited to admins.
- **Audit log**: logins, add/backup/restore/delete and settings changes are
  written per user to the container log and to `state/audit.log` (kept in the
  NS8 backup).
- The module now publishes one TCP port for the portal and requests the
  `traefik@node:routeadm` authorization (granted at install).

## 1.1.0 — 2026-09-18

- **Restore from the UI.** Each device has a *Restore* button that lists its
  snapshots and restores one back onto the same device or, for a lost/broken
  phone, onto another paired device (cross-device). Destructive: it overwrites
  and reboots the target; the dialog warns and requires an explicit choice.
- **Incremental or full backups per device.** A per-device *Mode* selector:
  *Full* keeps timestamped snapshots with retention (default), *Incremental*
  keeps one backup that iOS updates in place at every run.
- **Include device backups in the NS8 backup (toggle).** Off by default; on, the
  ``idevice-data`` volume is added to the module's NS8 backup (regenerates
  ``state-include.conf``). The UI notes it can roughly double the backup size.
- restore-backup gained ``target_udid`` and the engine tool a ``--source`` /
  ``--incremental`` option.

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
