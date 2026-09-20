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

*** Keywords ***
Portal login page is served
    ${out} =    Run on node    curl -fsSkL -H 'Host: idevice.ci.test' https://127.0.0.1/
    Should Contain    ${out}    iOS Device Backup
