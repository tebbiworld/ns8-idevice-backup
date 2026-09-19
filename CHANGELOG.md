# Changelog

## 1.3.0 — 2026-09-19

Alignment with the NethServer module conventions (NethServer/agents skills).

### Changed

- **Secrets moved out of the module environment.** The session secret of the self-service portal and the LDAP bind password are now kept in `state/passwords.env` (mode 0600) instead of `state/environment`, which NS8 mirrors to Redis in plain text. Existing installations are migrated on update; the values do not change. The generated `ldap.env` and `web.env` are private (0600).
- **Restore brings the whole configuration back.** `restore-module` only re-applied the retention; it now restores every setting: automatic backups, the self-service portal with its route and the directory login. The backup includes the secrets file.

### Added

- Robot Framework tests (install, update from the previous release, backup and restore) run on real NS8 nodes through `stephdl/ns8-ci-actions`.

### Platform integration

- **Clone and move.** New `clone-module` step (a link to the restore step): a cloned or moved instance gets its route and settings back instead of coming up unconfigured.
- `org.nethserver.volumes`: the bulk-data volume(s) `idevice-data` can be placed on an additional disk when the module is installed.
- Instances installed before 1.2.0 get a properly allocated TCP port for the self-service portal on update (`node:portsadm`). Before, the portal used a port the node could hand out again to another module.
- Release notes are linked from the software centre (`relnotes_url`).

## 1.2.3 — 2026-09-18

- **Automatic backups.** A per-instance schedule backs up devices without anyone
  clicking. Enable it in Settings and set an interval in hours; an hourly timer
  backs up each device whose last backup is older than the interval and retries
  devices that were asleep on the next run.
- **Mobile-friendly self-service portal.** On phones the device table now stacks
  into one card per device instead of overflowing sideways.

## 1.2.2 — 2026-09-18

- **Encryption-password fix.** Backing up an already-encrypted device no longer
  fails with "Invalid password (MBErrorDomain/207)". The engine now queries the
  device's real encryption state and sets a backup password only when the device
  is not yet encrypted, instead of trying to re-apply it. The stored encryption
  flag self-heals from the device on every backup.
- **Admin UI for the self-service portal.** Cluster-admin gains a *Self-service
  portal* section (portal host name or path, Let's Encrypt, the self-restore
  switch) and a *Directory login (AD/LDAP)* form (URL, base DN, bind DN and
  password, login attribute, owner domain, optional group). These settings
  previously had to be entered from the backend only.
- Saving the global options no longer clears the portal/LDAP configuration:
  configure-module now preserves any field that is not submitted.

## 1.2.1 — 2026-09-18

- Self-service how-to now tells users to give the iPhone a fixed IP and turn
  Private Wi-Fi Address off, so its address stays reachable.
- Self-service layout polish (column widths for wide screens).
- The update hook now restarts the self-service web app too, so an update
  reaches it and not only the engine.

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
