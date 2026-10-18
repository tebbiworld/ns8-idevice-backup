*** Settings ***
Library     SSHLibrary
Resource    api.resource

*** Variables ***
# No iPhone in CI: the suite checks the module plumbing (engine container,
# self-service portal, directory settings, secrets, backup and restore).
${CONFIG}    {"retention":4,"include_backups":false,"autobackup_enabled":true,"autobackup_interval_hours":12,"selfservice_host":"idevice.ci.test","selfservice_path":"","selfservice_restore":true,"http2https":true,"lets_encrypt":false,"ldap_enabled":true,"ldap_url":"ldaps://ldap.ci.test:636","ldap_base_dn":"dc=ci,dc=test","ldap_bind_dn":"cn=reader,dc=ci,dc=test","ldap_bind_password":"Bind#Pass 1","ldap_user_attribute":"uid","ldap_group":"","ldap_domain":"ci.test"}

*** Test Cases ***
Install the module
    IF    '${SCENARIO}' == 'update'
        ${output}  ${rc} =    Execute Command    add-module ${UPDATE_FROM} 1    return_rc=True
    ELSE
        ${output}  ${rc} =    Execute Command    add-module ${IMAGE_URL} 1    return_rc=True
    END
    Should Be Equal As Integers    ${rc}  0
    &{output} =    Evaluate    ${output}
    Set Global Variable    ${module_id}    ${output.module_id}

Configure the module
    Run task    module/${module_id}/configure-module    ${CONFIG}    decode_json=${FALSE}

The portal answers behind Traefik
    Wait Until Keyword Succeeds    30 times    10 seconds    Portal login page is served

Register a device with the released version
    [Documentation]    Its backup password lands in devices.json (0644) there;
    ...                the update has to move it, see the devices suite.
    Skip If    '${SCENARIO}' != 'update'    scenario is ${SCENARIO}
    Register the test device    ${module_id}

Remember the certificate check before the update
    Skip If    '${SCENARIO}' != 'update'    scenario is ${SCENARIO}
    ${cfg} =    Run task    module/${module_id}/get-configuration    {}
    # releases before 1.5.0 had no such setting and never checked the certificate
    ${before} =    Evaluate    $cfg.get('ldap_tls_verify', False)
    Set Global Variable    ${TLS_VERIFY_BEFORE}    ${before}

Update to the image under test
    Skip If    '${SCENARIO}' != 'update'    scenario is ${SCENARIO}
    Run on node    api-cli run update-module --data '{"force":true,"module_url":"${IMAGE_URL}","instances":["${module_id}"]}'
    Wait Until Keyword Succeeds    30 times    10 seconds    Portal login page is served

Configuration reads back
    ${cfg} =    Run task    module/${module_id}/get-configuration    {}
    Should Be Equal As Integers    ${cfg['retention']}    4
    Should Be True    ${cfg['ldap_bind_password_set']}
    Should Be True    ${cfg['web_running']}

The portal receives the bind password
    ${n} =    Run on node    runagent -m ${module_id} bash -c 'grep -c "^LDAP_BIND_PASSWORD=Bind#Pass 1$" "$AGENT_STATE_DIR/ldap.env"'
    Should Be Equal As Integers    ${n.strip()}    1

Secrets are stored in passwords.env only
    Secrets are kept out of the module environment    ${module_id}
    ${mode} =    Run on node    runagent -m ${module_id} bash -c 'stat -c \%a "$AGENT_STATE_DIR/ldap.env" "$AGENT_STATE_DIR/web.env" | sort -u'
    Should Be Equal As Strings    ${mode.strip()}    600

An earlier configuration is kept as an LDAP server of its own
    ${cfg} =    Run task    module/${module_id}/get-configuration    {}
    Should Be Equal    ${cfg['ldap_source']}    custom
    Should Be Equal    ${cfg['ldap_url']}    ldaps://ldap.ci.test:636
    Should Be Equal    ${cfg['ldap_user_attribute']}    uid
    Should Be True    'ldap_bind_password' not in $cfg
    # the update keeps the certificate check of the released version
    IF    '${SCENARIO}' == 'update'
        Should Be Equal    ${cfg['ldap_tls_verify']}    ${TLS_VERIFY_BEFORE}
    END

The portal listens on the node loopback only
    ${port} =    Run on node    runagent -m ${module_id} bash -c 'echo $WEB_PORT'
    ${bound} =    Run on node    ss -tlnH 'sport = :${port.strip()}' | awk '{print $4}' | sort -u
    Should Be Equal As Strings    ${bound.strip()}    127.0.0.1:${port.strip()}
    ${old} =    Run on node    ss -tlnH 'sport = :8080' | wc -l
    Should Be Equal As Integers    ${old.strip()}    0

The favicon is served without login
    ${out} =    Run on node    curl -sSk -o /dev/null -w '\%{http_code} \%{content_type}' -H 'Host: idevice.ci.test' https://127.0.0.1/favicon.ico
    Should Start With    ${out.strip()}    200 image/png

Groups of an unknown user domain are an empty list
    ${out} =    Run task    module/${module_id}/list-ldap-groups    {"domain":"no-such-domain.ci.test"}
    Should Be Equal As Integers    ${{ len($out['groups']) }}    0

Failed logins are throttled
    # five wrong passwords for one account, then even the form is refused
    # before the directory is asked (the audit log records "throttled")
    FOR    ${i}    IN RANGE    6
        Portal login with a wrong password
    END
    ${n} =    Run on node    runagent -m ${module_id} bash -c 'grep -c "result=throttled" "$AGENT_STATE_DIR/audit.log" || true'
    Should Be True    ${n.strip()} >= 1

The overdue mail settings read back
    Run task    module/${module_id}/configure-module    {"retention":4,"include_backups":false,"autobackup_enabled":true,"autobackup_interval_hours":12,"notify_mail_to":"backup-admin@ci.test","notify_owners":true,"notify_overdue_days":3}    decode_json=${FALSE}
    ${cfg} =    Run task    module/${module_id}/get-configuration    {}
    Should Be Equal    ${cfg['notify_mail_to']}    backup-admin@ci.test
    Should Be True    ${cfg['notify_owners']}
    Should Be Equal As Integers    ${cfg['notify_overdue_days']}    3
    Should Not Be True    ${cfg['autobackup_require_power']}
    Run task    module/${module_id}/configure-module    {"retention":4,"include_backups":false,"autobackup_enabled":true,"autobackup_interval_hours":12,"autobackup_require_power":true}    decode_json=${FALSE}
    ${cfg} =    Run task    module/${module_id}/get-configuration    {}
    Should Be True    ${cfg['autobackup_require_power']}
    # saving the global options keeps the portal login
    Should Be True    ${cfg['web_running']}

An overdue device without a smarthost is reported, not fatal
    ${out} =    Run on node    runagent -m ${module_id} python3 -c 'import backupnotify, time; print(backupnotify.check({"x": {"name": "ci", "ip": "192.0.2.1", "owner": "ci", "last_backup": time.time() - 30 * 86400}}))' 2>&1
    Should Contain    ${out}    not sent

*** Keywords ***
Portal login page is served
    ${out} =    Run on node    curl -fsSkL -H 'Host: idevice.ci.test' https://127.0.0.1/
    Should Contain    ${out}    iOS Device Backup

Portal login with a wrong password
    Run on node    j=$(mktemp); csrf=$(curl -sSk -c $j -b $j -H 'Host: idevice.ci.test' https://127.0.0.1/login | grep -o 'name="csrf" value="[^"]*"' | cut -d'"' -f4); curl -sSk -o /dev/null -c $j -b $j -H 'Host: idevice.ci.test' --data-urlencode "csrf=$csrf" --data-urlencode username=ci-throttle --data-urlencode password=wrong https://127.0.0.1/login; rm -f $j
