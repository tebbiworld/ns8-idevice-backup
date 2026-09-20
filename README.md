# ns8-idevice-backup

[NethServer 8](https://github.com/NethServer/ns8-core) module to back up
**iPhones and iPads over WiFi**, with no USB on the server. It uses
[pymobiledevice3](https://github.com/doronz88/pymobiledevice3) to connect to a
device by its IP address over the classic lockdown TCP port (62078) with a
**pairing file you create on your own computer** and upload here.

> Status: this module is deployed for testing the device connection. The
> end-to-end WiFi-backup path has not been verified against a physical device
> yet — that is exactly what the dev deployment is for. Treat it as unproven
> until a real iPhone backup has succeeded.

- No USB on the server: the pairing is done once on your computer (USB) and the
  pairing file is uploaded
- Connects by explicit device IP (no mDNS/Bonjour), so it works across subnet
  boundaries (server LAN and WiFi on different subnets)
- Device-side encrypted backups, kept per device with a configurable retention
- Runs rootless; the engine only makes outbound TCP connections to the device

## How the connection works (verified from source, not yet device-tested)

- pymobiledevice3 opens a TCP lockdown session to `<device_ip>:62078` using the
  uploaded pairing record, then runs the classic `com.apple.mobilebackup2`
  service — the same one Finder WiFi backup uses. No usbmux, netmuxd or Bonjour
  is involved.
- The server must be able to reach the iPhone on **TCP 62078**. With the iPhone
  on a separate WiFi subnet, the router must route/allow that from the server
  subnet (172.17.0.0/24) to the WiFi subnet.

## Pairing a device (once, on your own computer, iPhone on USB)

Two steps are required. The second is the one people miss:

1. **Get the pairing file.** Connect the iPhone by USB, open iTunes or the
   Apple Devices app and confirm *Trust this computer*. The computer then
   holds a lockdown pairing record for the device:

   - Windows: `C:\ProgramData\Apple\Lockdown\<UDID>.plist`
   - macOS: `/var/db/lockdown/<UDID>.plist` (root only)

   Upload that file **without renaming it**: the record has no UDID inside,
   the module takes the UDID from the file name. Alternatively use a
   `<UDID>.mobiledevicepairing` file made by `jitterbugpair`
   ([Jitterbug](https://github.com/osy/Jitterbug), archived; the binaries are
   under *Assets* of release v1.3.1).
2. **Enable WiFi connections** once over USB. Either tick *Sync with this
   iPhone over Wi-Fi* in iTunes / Apple Devices and apply, or run

       pymobiledevice3 lockdown wifi-connections on

   (on Windows `pip install pymobiledevice3` needs the Microsoft C++ Build
   Tools, so the checkbox is the easier way). The iPhone must have a
   **passcode** set. A pairing record alone is not enough; without this step
   the WiFi backup cannot connect. The key is `EnableWifiConnections` in the
   `com.apple.mobile.wireless_lockdown` domain; jitterbugpair sets only
   `EnableWifiDebugging`.

Then, in the module: upload the pairing file, enter the iPhone's WiFi IP
address, optionally set a backup encryption password, and press
*Back up now*.

## Encryption password

If you set a backup encryption password, the module turns on device-side backup
encryption for that device on the first backup. iOS then encrypts the backup on
the device; the password is **stored on the iPhone** and is required to restore.
The module also keeps the password so scheduled backups and restores can use
it: in `state/device-secrets.json` (mode 0600, part of the module backup), apart
from the device registry, and never returned by an action or shown in a page.
Store it safely — if it is lost, the encrypted backup cannot be restored.

## Long backups, lost connections, progress

A first full backup over WiFi can take more than an hour. While it runs, the
portal and cluster-admin show the percentage reported by the device.

- If the connection drops, the module waits 60 s and tries again, up to three
  attempts, in the same snapshot. Keep the iPhone on the charger and on the
  WiFi; without power iOS puts the WiFi sync to sleep.
- A full backup that still fails is **kept as unfinished** instead of deleted.
  It cannot be restored and does not count towards the retention; the next
  backup of the device continues in it and it disappears once a backup
  finishes. How much the device reuses is up to iOS: in **incremental** mode an
  interrupted update continues from the last finished state, a very first
  backup may be transferred again. For devices on a weak link, incremental mode
  is the better choice once one backup has finished.
- Only one backup per device runs at a time, whoever started it.

## Backup storage and the NS8 backup

- Device backups are stored in the `idevice-data` volume under
  `/data/backups/<UDID>/<timestamp>`.
- The NS8 backup of this module includes the **pairing records** and the device
  **registry** (small, and painful to lose), but **not** the device backups
  themselves by default, because they can be large. Add
  `volumes/idevice-data` to `imageroot/etc/state-include.conf` if you want the
  NS8 backup to carry the device backups too.

## Self-service portal (users manage their own devices)

Besides the cluster-admin view, the module can run a **self-service web app** so
end users manage the backups of their **own** devices without the admin entering
every device by hand.

- Configure a **self-service host** (FQDN) and optionally a **path** (e.g.
  `/idevice`, served like `https://host/idevice/`, similar to cluster-admin),
  with Let's Encrypt.
- Users log in with **AD/LDAP** (a direct bind with the admin-entered service
  account; use `ldaps://…:636`). Give the LDAP URL, base DN, bind DN + password,
  the login attribute (default `sAMAccountName`) and, optionally, a group DN.
- Each device has an **owner**; a user only sees and acts on their own devices.
  The admin sees all devices and assigns owners in cluster-admin. A device paired
  through the portal is owned by the logged-in user.
- Users can add a device (pairing upload), set the WiFi IP, pick full or
  incremental, **back up** and **restore** (restore can be limited to admins with
  the *Allow users to restore* switch). The portal is bilingual (DE/EN) and shows
  a short guide for creating the pairing file.
- **Audit log:** every login and device action is recorded per user in the
  container log and in `state/audit.log` (which the NS8 backup keeps).

The portal needs one TCP port and the `traefik@node:routeadm` authorization,
granted when the module is installed.

## Restore to a device

Restoring writes a stored snapshot back **onto** the device and reboots it, so it
overwrites the current contents of that iPhone/iPad. Trigger it from the leader
node with the snapshot name from *list-backups*:

    api-cli run module/idevice-backup1/restore-backup --data '{"udid":"<UDID>","snapshot":"2026-09-18_07-16-00"}'

The device must be reachable on WiFi (pairing valid, WiFi lockdown on) exactly as
for a backup, and you confirm the restore prompt on the device. An encrypted
backup is restored with its password automatically (the one stored for the
device); pass `encryption_password` to override it. Apple restores an encrypted
backup only to a device set up for it, and some data is device-bound.

The same operation is available inside the engine container:

    podman exec [-e IDEVICE_RESTORE_PASSWORD=...] idevice-backup \
        idevice-tool restore --udid <UDID> --ip <device-ip> \
        --dir /data/backups/<UDID>/<snapshot>

## Limitations and things to watch

- **Not yet verified end-to-end** against a real device (see status note).
- **WiFi lockdown must be enabled** on the device (see pairing step 2) — the
  most common reason a first backup fails to connect.
- **Routing/firewall:** the server must reach `<device_ip>:62078` across the
  subnet boundary.
- **DHCP:** if the iPhone's IP changes, update it on the device row. A static
  DHCP reservation for each iPhone is recommended.
- **Pairing expiry:** if the pairing stops working (e.g. after a long time or
  an iOS major update), re-pair on your computer and upload a new file.
- **Login / multi-user:** every cluster admin who can open this app sees all
  devices. Per-user device ownership via the NS8 user domain (LDAP) is not
  implemented yet.

## Development

    IMAGETAG=1.0.0 bash ./build-images.sh

`device/Containerfile` builds an image with pymobiledevice3 (lzfse compiled in
a builder stage); `device/idevice-tool` is the backup helper the module runs
inside the container.

Tests without an iPhone: `python3 device/tests/test_backup_logic.py` (continue,
retry, retention, lock, status; fake device) and `python3 tests/unit/test_devreg.py`
(password migration) run from a checkout; `device/tests/test_portal.py` needs
flask and runs in the image (`podman exec idevice-backup python3
/opt/idevice-tests/test_portal.py`). The Robot suite in `tests/` runs all of
them on a real node.

## License

Module: GPL-3.0-or-later. pymobiledevice3 and libimobiledevice are their
authors' projects under their own licenses; this module installs pymobiledevice3
unmodified from PyPI.
