#
# Copyright (C) 2026 tebbi
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""Directory login shared by the tebbiworld NS8 modules (kept identical in
ns8-clamav web/ldapauth.py and ns8-idevice-backup device/ldapauth.py).

Two sources, selected by LDAP_SOURCE and written to the container
environment by the module's bin/discover-ldap:

  domain  a user domain of the NS8 cluster, through the node's ldapproxy
          (clear text on the node loopback, the proxy talks TLS to the
          directory); the module stores no credentials of its own
  custom  an LDAP server entered in the module settings

Variables (prefix LDAP_):
  SOURCE         domain | custom
  HOST, PORT     server
  TLS            none | ssl (ldaps://) | starttls
  TLS_VERIFY     true | false: check the server certificate (system CAs or CA_FILE)
  CA_FILE        optional CA bundle for TLS_VERIFY
  SCHEMA         ad | rfc2307
  BASE_DN, BIND_DN, BIND_PASSWORD
  USER_ATTRIBUTE login attribute (default sAMAccountName for ad, uid for rfc2307)
  GROUP          optional: only members may log in; a group name, or a DN
                 (custom servers); nested AD groups count
"""
import os
import ssl
import sys

import ldap3
from ldap3.utils.conv import escape_filter_chars

TIMEOUT = 10


class Settings:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        get = lambda k, d="": (env.get("LDAP_" + k) or d).strip()
        self.source = get("SOURCE", "domain").lower()
        self.host = get("HOST")
        self.tls = get("TLS", "none").lower()
        self.port = int(get("PORT") or (636 if self.tls == "ssl" else 389))
        self.tls_verify = get("TLS_VERIFY", "true").lower() not in ("0", "false", "no", "off")
        self.ca_file = get("CA_FILE")
        self.schema = get("SCHEMA", "ad").lower()
        self.base_dn = get("BASE_DN")
        self.bind_dn = get("BIND_DN")
        self.bind_password = env.get("LDAP_BIND_PASSWORD", "")
        default_attr = "sAMAccountName" if self.schema == "ad" else "uid"
        self.user_attribute = get("USER_ATTRIBUTE", default_attr)
        self.group = get("GROUP")

    @property
    def configured(self):
        return bool(self.host and self.base_dn)

    def describe(self):
        """One line for the log, without secrets."""
        if not self.configured:
            return "directory login not configured"
        where = "user domain (ldapproxy)" if self.source == "domain" else f"LDAP server {self.host}:{self.port}"
        tls = "" if self.source == "domain" else f", TLS {self.tls}" + ("" if self.tls == "none" or self.tls_verify else " without certificate check")
        return f"{where}{tls}, schema {self.schema}" + (f", group {self.group}" if self.group else "")

    def server(self):
        tls = None
        if self.tls in ("ssl", "starttls"):
            if self.tls_verify:
                tls = ldap3.Tls(validate=ssl.CERT_REQUIRED, version=ssl.PROTOCOL_TLS_CLIENT,
                                ca_certs_file=self.ca_file or None)
            else:
                tls = ldap3.Tls(validate=ssl.CERT_NONE)
        return ldap3.Server(self.host, port=self.port, use_ssl=self.tls == "ssl", tls=tls,
                            connect_timeout=TIMEOUT)

    def connect(self, user, password):
        conn = ldap3.Connection(self.server(), user, password, receive_timeout=TIMEOUT)
        if self.tls == "starttls":
            conn.open()
            conn.start_tls()
        if not conn.bind():
            conn.unbind()
            raise ldap3.core.exceptions.LDAPBindError(conn.result.get("description", "bind failed"))
        return conn


def _log(msg):
    print(msg, file=sys.stderr)


def _user_filter(s, login):
    name = escape_filter_chars(login)
    if s.schema == "ad":
        # enabled accounts only
        return f"(&(objectClass=user)({s.user_attribute}={name})(!(userAccountControl:1.2.840.113556.1.4.803:=2)))"
    return f"(&(objectClass=posixAccount)({s.user_attribute}={name}))"


def _is_member(s, conn, user_dn, uid):
    group = s.group
    if "=" in group:
        group_dn = group
    else:
        gname = escape_filter_chars(group)
        gfilter = f"(&(objectClass=group)(sAMAccountName={gname}))" if s.schema == "ad" else f"(&(objectClass=posixGroup)(cn={gname}))"
        conn.search(s.base_dn, gfilter, attributes=[])
        if len(conn.entries) != 1:
            _log(f"login refused: group {group!r} not found")
            return False
        group_dn = conn.entries[0].entry_dn
    if s.schema == "ad":
        # LDAP_MATCHING_RULE_IN_CHAIN: members of nested groups count too
        conn.search(user_dn, f"(memberOf:1.2.840.113556.1.4.1941:={escape_filter_chars(group_dn)})",
                    search_scope=ldap3.BASE, attributes=[])
    else:
        conn.search(group_dn, f"(|(memberUid={escape_filter_chars(uid)})(member={escape_filter_chars(user_dn)}))",
                    search_scope=ldap3.BASE, attributes=[])
    return len(conn.entries) > 0


def authenticate(s, login, password):
    """Return {"uid", "display", "mail", "dn"} for a valid login, else None."""
    # an empty password would be an anonymous (always successful) bind
    if not s.configured or not login or not password:
        return None
    try:
        with s.connect(s.bind_dn, s.bind_password) as conn:
            conn.search(s.base_dn, _user_filter(s, login), attributes=[s.user_attribute, "displayName", "mail"])
            if len(conn.entries) != 1:
                return None
            entry = conn.entries[0]
            user_dn = entry.entry_dn
            attrs = entry.entry_attributes_as_dict
            uid = str((attrs.get(s.user_attribute) or [login])[0])
            display = str((attrs.get("displayName") or [uid])[0])
            mail = str((attrs.get("mail") or [""])[0])
            if s.group and not _is_member(s, conn, user_dn, uid):
                _log(f"login refused: {uid} is not a member of {s.group}")
                return None
    except ldap3.core.exceptions.LDAPBindError:
        _log("directory login unavailable: the service account bind failed")
        return None
    except ldap3.core.exceptions.LDAPException as exc:
        _log(f"LDAP error: {type(exc).__name__}: {exc}")
        return None
    # the password check itself: bind as the user
    try:
        s.connect(user_dn, password).unbind()
    except ldap3.core.exceptions.LDAPException:
        return None
    return {"uid": uid, "display": display, "mail": mail, "dn": user_dn}
