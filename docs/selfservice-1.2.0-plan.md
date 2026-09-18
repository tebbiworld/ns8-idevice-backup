# ns8-idevice-backup 1.2.0 — self-service app architecture plan

Goal: let end users manage the backups of **their own** iPhones/iPads through a
web app authenticated against the NS8 user domain (AD/LDAP), so the admin does
not have to enter every device in the company by hand. The admin keeps the full
view in cluster-admin.

Not in scope: injecting into the core `/users-admin/{domain}/` portal — that page
belongs to the account provider in ns8-core and a community module cannot extend
it. The self-service app therefore lives on its **own FQDN**.

---

## 1. Two surfaces, one module

| Surface | Who | Auth | Scope |
| --- | --- | --- | --- |
| **cluster-admin** (today's Settings/Status views) | node admins | NS8 RBAC | all devices, global options, owner assignment |
| **self-service web app** (new, own FQDN) | domain users | LDAP against the user domain | only the logged-in user's own devices |

Both share the same module state (device registry) and the same engine
(pymobiledevice3 / `idevice-tool`) and volumes. The self-service surface never
uses the NS8 agent action layer (that is admin/cluster scoped); it is a small web
service inside the module that enforces per-user ownership itself.

## 2. Components

- **engine** (exists): the pymobiledevice3 container that runs backup/restore.
- **web** (new): a small web service (FastAPI/Flask; Python so it can reuse pmd3
  and the existing `idevice-tool`). Serves the login + self-service UI and a JSON
  API. Recommended: **same image as the engine** (it already carries pmd3 and the
  tool), started as a second process/unit, so there is one image to build and the
  web can call `idevice-tool` locally over the shared volumes. Alternative: a
  separate web container sharing the `idevice-lockdown`, `idevice-data` and a
  state volume — cleaner isolation, more plumbing. **Decision: same image.**
- **Traefik route** (new): the web service gets an FQDN and a module-managed
  Traefik route with Let's Encrypt (the module has no route today). TLS is
  terminated by Traefik; the web service is plain HTTP inside, behind the proxy.

## 3. Authentication and the user domain

- Bind against the NS8 user domain via `agent.ldapproxy.Ldapproxy`:
  `get_domain(domain)` yields the LDAP host/port, the service `bind_dn` +
  password, and the user search base/filter — the same mechanism ARSnova, Open
  WebUI and SageMath use.
- Login flow: service-bind → search the user → re-bind as the user to verify the
  password → issue a session cookie.
- **AD requires LDAPS on 636** (plaintext 389 is rejected on `ad.ebbinghaus.world`);
  the module must use `ldaps://…:636` and trust the domain CA.
- **Configuration:** the instance is bound to one user domain (a setting, like the
  other LDAP modules). Multi-domain is a later option, not v1.2.0.
- **Sessions:** signed cookie, short TTL, HTTPS-only, CSRF token on state-changing
  requests. Session store kept in a small state file/volume so a restart does not
  drop everyone.

### 3a. Reaching the ldapproxy under Podman 5 (pasta) — the login gotcha

Since Podman 5 the default rootless network is **pasta**, which mirrors the
node's own LAN IP into the container, so a module cannot reach a node service by
the node IP (connection refused inside the container). The ldapproxy is a
node-local service, so the web container needs one of the established recipes
(community thread 28684, posts #4/#5; and how the other tebbiworld LDAP modules
already do it):

- **`host.containers.internal` (169.254.1.2)** as the LDAP host — used by
  ns8-arsnova (`bin/ldap-container-host`) and ns8-sagemath for same-node LDAP.
- or **`--network pasta:--map-gw,-a,10.0.2.0,-g,10.0.2.2`** in the web unit, which
  keeps the well-known `10.0.2.2` gateway pointing at the host loopback (ldapproxy
  binds to 127.0.0.1) — mrmarkuz/thorsten confirmed this for Dokuwiki/WebTop.
- or **`--network=host`** — ns8-openwebui uses this to sidestep the pasta hairpin.

Decision: reuse the arsnova/sagemath pattern — resolve the ldapproxy endpoint
from `agent.ldapproxy.Ldapproxy`, and if it is a node-local address, target it via
`host.containers.internal` (a small helper writes the `--add-host` mapping into the
web unit at configure time). Fall back to `--map-gw` if a node binds the proxy to
127.0.0.1 only. **LDAPS on 636** is mandatory for AD (see the encryption note).


## 4. Ownership model (registry change)

`devices.json` gains per device:

- `owner`: the LDAP user id (e.g. `uid` or `user@domain`) that owns the device.
- `domain`: the user domain the owner belongs to.

Rules:

- A device paired **through the self-service app** gets `owner = the logged-in
  user`, `domain = their domain`.
- A device added **in cluster-admin** has no owner until the admin assigns one.
- The self-service app lists/acts only on devices where `owner == current user`.
- cluster-admin sees **all** devices, shows an **Owner** column, and can assign,
  reassign or clear the owner.
- A cross-device restore in self-service is allowed **only between two devices the
  same user owns** (a user can never write onto someone else's device).

Backwards compatible: existing devices simply have no `owner` and stay
admin-only until assigned.

## 5. What each role may do

Self-service user (own devices only):

- log in / log out
- add a device: upload the pairing file, set name, WiFi IP, optional encryption
  password (owner is set to them)
- run a backup; choose full/incremental (their device)
- restore onto their own device, or cross-restore between their own devices
  (destructive; strong confirmation)
- update IP, rename, delete their device

Self-service user may **not**: see other users' devices, change global options
(retention, NS8-backup toggle), assign owners.

Admin (cluster-admin) — as in 1.1.0, plus: Owner column, assign/reassign/clear
owner, and an optional per-instance switch "allow users to restore themselves"
(off = self-service can back up but restore stays admin-only, because restore is
destructive).

## 6. Backend / API

The web service exposes a minimal JSON API, all guarded by the session and an
ownership check on every device id:

- `POST /login`, `POST /logout`, `GET /me`
- `GET /devices` → the caller's devices
- `POST /devices` (upload pairing) → owner = caller
- `PATCH /devices/{udid}` (ip, name, backup_mode, encryption_password)
- `DELETE /devices/{udid}`
- `POST /devices/{udid}/backup`
- `GET /devices/{udid}/backups`
- `POST /devices/{udid}/restore` (target = own device; guardrails)

Internally these reuse the exact logic already written for the agent actions
(`idevice-tool` calls, retention, state-include is untouched here) but add the
`owner == caller` gate. Factor the shared logic (pairing parse, backup, restore,
list, prune) into a small module both the agent actions and the web API import,
so there is one implementation.

## 7. Security

- Ownership enforced **server-side** on every request; never trust a udid from the
  client without the owner check.
- HTTPS only (Traefik), secure/httponly cookies, CSRF on writes, rate-limit login.
- Restore is destructive: explicit confirmation, and the optional admin switch to
  keep restore admin-only.
- Pairing file validated as today (lockdown record / UDID from filename).
- The encryption password is per device; a user sets and holds their own. The
  module still stores it (node-admin readable) — document that clearly.
- Audit: log who did what (login, pair, backup, restore) to the module log.

## 8. Constraints (unavoidable, document them)

- **USB pairing stays.** Apple requires the one-time pairing + `wifi-connections
  on` over USB on the user's own computer. The app only receives the finished
  pairing file; it cannot remove that step.
- **Encrypted backups need the password** to restore; losing it loses the backup.
- **Reachability:** the server must reach each device on TCP 62078 over WiFi.

## 9. Milestones

1. **Ownership in the registry** + admin Owner column / assignment (ships in
   cluster-admin first; no web app yet).
2. **Web skeleton:** same-image web process, FQDN + Traefik route, LDAP login,
   sessions.
3. **Self-service device management:** list own, add (pairing upload), run backup,
   full/incremental.
4. **Self-service restore:** own device + cross-restore among own devices, with
   guardrails and the admin switch.
5. **Polish:** i18n (EN/DE), audit log, README/wiki, deploy test against the dev
   AD domain.

## 10. Decisions (locked 2026-09-18, approved by the maintainer)

- **One domain per instance** (multi-domain later).
- **Restore in self-service allowed by default, with an admin switch** to make it admin-only.
- **Same image** for engine + web.
- **Owner identity: store both `uid` and `user@domain`**, match on uid within the bound domain.
- **Session store: a small file in a state volume.**
