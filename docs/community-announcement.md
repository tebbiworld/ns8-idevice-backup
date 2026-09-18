<!--
First community post for the NS8 iOS Device Backup module, written in the style
of https://community.nethserver.org/t/ns8-forgejo-testing/28554 (first post).
Paste into a new topic on community.nethserver.org, category "App", tag "ns8".
Fill in the wiki link once the page is published.
-->

# NS8 iOS Device Backup (testing)

Hi all,

I've built an NS8 module that backs up **iPhones and iPads over WiFi**, with no USB cable on the server. It uses [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) and talks to the device by its IP over the classic lockdown port (62078) — no usbmux, no Bonjour/mDNS — so it works across subnet boundaries.

It's in my community repository. To try it, add the repo once:

```
api-cli run add-repository --data '{"name":"tebbiworld","url":"https://raw.githubusercontent.com/tebbiworld/ns8-repo/main/ns8/updates/","status":true,"testing":false}'
```

then install **iOS Device Backup** from the Software Center. (Or straight from the image: `add-module ghcr.io/tebbiworld/idevice-backup:latest 1`.)

What it does:

* back up a device over WiFi by IP — **full** snapshots (with a retention count) or one **incremental** folder per device
* device-side **encrypted** backups; **restore** a snapshot onto the same device or onto a replacement iPhone
* an optional **self-service portal**: end users log in with their **AD/LDAP** account and manage the backups of *their own* devices only, while the admin keeps the full view in cluster-admin. Bilingual EN/DE.
* optionally include the device backups in the NS8 (restic) backup — off by default, they can be many GB

A few things to know:

* pairing is done **once over USB** on your own computer (in iTunes / the Apple Devices app: tap *Trust*, tick *Sync over Wi-Fi*); you then upload the resulting lockdown pairing file. Apple requires that step — the module can't remove it.
* give the iPhone a **fixed IP** and turn **Private Wi-Fi Address off** for that network, so it stays reachable
* an encrypted backup **cannot be restored without its password** — keep it somewhere safe off the server

I mainly built this to keep a couple of family iPhones backed up to the server without plugging them in every time. Feedback very welcome — especially on restore to a replacement device, and on the self-service portal against different AD/LDAP setups.

Docs: NethServer wiki (tebbiworld repository) · Source: [github.com/tebbiworld/ns8-idevice-backup](https://github.com/tebbiworld/ns8-idevice-backup)

Thanks!

*Category: App · Tags: ns8*
