*** Settings ***
Library     SSHLibrary
Resource    api.resource

*** Test Cases ***
Back up the module
    ${repo}    ${path} =    Back up the module to the cluster repository    ${module_id}
    Set Global Variable    ${BACKUP_REPO}    ${repo}
    Set Global Variable    ${BACKUP_PATH}    ${path}

Restore into a new instance
    ${rid} =    Restore the module from the cluster repository    ${BACKUP_REPO}    ${BACKUP_PATH}
    Set Global Variable    ${restored_id}    ${rid}
    Should Not Be Equal    ${restored_id}    ${module_id}

The restored instance has settings and secrets
    ${cfg} =    Run task    module/${restored_id}/get-configuration    {}
    Should Be Equal As Integers    ${cfg['retention']}    4
    Should Be True    ${cfg['ldap_bind_password_set']}
    Secrets are kept out of the module environment    ${restored_id}
    ${a} =    Run on node    runagent -m ${module_id} bash -c 'sort "$AGENT_STATE_DIR/passwords.env" | sha256sum'
    ${b} =    Run on node    runagent -m ${restored_id} bash -c 'sort "$AGENT_STATE_DIR/passwords.env" | sha256sum'
    Should Be Equal    ${a}    ${b}

The restored instance has the devices and their passwords
    ${a} =    Password hash of the test device    ${module_id}
    ${b} =    Password hash of the test device    ${restored_id}
    Should Be Equal    ${a}    ${b}
    ${modes} =    Run on node    runagent -m ${restored_id} bash -c 'stat -c \%a "$AGENT_STATE_DIR/devices.json" "$AGENT_STATE_DIR/device-secrets.json" | sort -u'
    Should Be Equal As Strings    ${modes.strip()}    600
    ${cfg} =    Run task    module/${restored_id}/get-configuration    {}
    Should Be True    ${cfg['devices'][0]['encryption_password_set']}
