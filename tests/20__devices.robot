*** Settings ***
Library     SSHLibrary
Resource    api.resource

*** Variables ***
# No iPhone in CI. A made-up pairing record registers a device, 127.0.0.1 makes
# the connection fail at once; the logic behind continue/retry/progress is
# tested with a fake device inside the engine image.
${UDID}       00008130-00000000CITEST01
${DEV_PW}     Dev#Pass 9
${DEV_PW_SHA}    unset

*** Test Cases ***
Logic tests pass inside the shipped image
    [Documentation]    Continue an unfinished snapshot, retry a lost connection,
    ...                retention, lock, status file, portal progress endpoint.
    ${out} =    Run on node    runagent -m ${module_id} podman exec idevice-backup python3 /opt/idevice-tests/test_backup_logic.py 2>&1 | tail -n 3
    Should Contain    ${out}    OK
    ${out} =    Run on node    runagent -m ${module_id} podman exec idevice-backup python3 /opt/idevice-tests/test_portal.py 2>&1 | tail -n 3
    Should Contain    ${out}    OK

Register a device with a backup password
    Skip If    '${SCENARIO}' == 'update'    registered before the update, see the install suite
    Register the test device    ${module_id}

The device password is a secret
    [Documentation]    Up to 1.3.0 it was a field of devices.json (0644). In the
    ...                update scenario the device was registered with the old
    ...                version, so this also proves the migration.
    ${cfg} =    Run task    module/${module_id}/get-configuration    {}
    ${dev} =    Evaluate    [d for d in $cfg['devices'] if d['udid'] == '${UDID}'][0]
    Should Be True    ${dev['encryption_password_set']}
    Should Not Contain    ${{json.dumps($cfg)}}    ${DEV_PW}
    ${modes} =    Run on node    runagent -m ${module_id} bash -c 'stat -c \%a "$AGENT_STATE_DIR/devices.json" "$AGENT_STATE_DIR/device-secrets.json" | sort -u'
    Should Be Equal As Strings    ${modes.strip()}    600
    ${leaks} =    Run on node    runagent -m ${module_id} bash -c 'grep -c "encryption_password" "$AGENT_STATE_DIR/devices.json" || true'
    Should Be Equal As Integers    ${leaks.strip()}    0
    ${leaks} =    Run on node    runagent -m ${module_id} bash -c 'grep -cF "${DEV_PW}" "$AGENT_STATE_DIR/devices.json" "$AGENT_STATE_DIR/environment" | grep -vc ":0$" || true'
    Should Be Equal As Integers    ${leaks.strip()}    0
    ${sha} =    Password hash of the test device    ${module_id}
    ${expected} =    Evaluate    hashlib.sha256('${DEV_PW}'.encode()).hexdigest()    modules=hashlib
    Should Be Equal    ${sha}    ${expected}    the stored password changed
    Set Global Variable    ${DEV_PW_SHA}    ${sha}

Migrating again changes nothing
    Run on node    runagent -m ${module_id} migrate-device-secrets
    ${sha} =    Password hash of the test device    ${module_id}
    Should Be Equal    ${sha}    ${DEV_PW_SHA}

A legacy registry is migrated when the services start
    [Documentation]    e.g. the state of an old backup restored over the instance
    Run on node    runagent -m ${module_id} python3 -c 'import json; p="devices.json"; d=json.load(open(p)); d["${UDID}"]["encryption_password"]="Legacy#Pass 1"; json.dump(d,open(p,"w")); import os; os.chmod(p,0o644)'
    Run on node    runagent -m ${module_id} bash -c 'rm -f "$AGENT_STATE_DIR/device-secrets.json"'
    Run on node    runagent -m ${module_id} systemctl --user restart idevice-backup.service
    ${leaks} =    Run on node    runagent -m ${module_id} bash -c 'grep -c "encryption_password" "$AGENT_STATE_DIR/devices.json" || true'
    Should Be Equal As Integers    ${leaks.strip()}    0
    ${sha} =    Password hash of the test device    ${module_id}
    ${expected} =    Evaluate    hashlib.sha256('Legacy#Pass 1'.encode()).hexdigest()    modules=hashlib
    Should Be Equal    ${sha}    ${expected}
    # back to the password the later suites compare with
    Run task    module/${module_id}/update-device    {"udid":"${UDID}","encryption_password":"${DEV_PW}"}    decode_json=${FALSE}

A running backup is reported with its percentage
    [Documentation]    The tool keeps <udid>/.status.json; both UIs read it, whoever started the backup.
    Wait Until Keyword Succeeds    12 times    5 seconds    Engine container is running
    Write backup status    running    37
    ${dev} =    Device from the configuration
    Should Be True    ${dev['running']}
    Should Be Equal As Integers    ${dev['progress']}    37
    Should Be Equal    ${dev['run_state']}    running

A dead backup process is not reported as running
    Run on node    runagent -m ${module_id} podman exec idevice-backup sh -c 'echo "{\\"state\\":\\"running\\",\\"percent\\":37,\\"updated\\":1}" > /data/backups/${UDID}/.status.json'
    ${dev} =    Device from the configuration
    Should Not Be True    ${dev['running']}
    Should Be Equal As Integers    ${dev['progress']}    0

The portal progress endpoint needs a login
    ${code} =    Run on node    curl -sk -o /dev/null -w '\%{http_code}' -H 'Host: idevice.ci.test' https://127.0.0.1/progress.json
    Should Be Equal As Strings    ${code.strip()}    401

An unreachable device fails with a clear message and leaves no snapshot
    Run task    module/${module_id}/run-backup    {"udid":"${UDID}"}    decode_json=${FALSE}    rc_expected=1
    ${cfg} =    Run task    module/${module_id}/get-configuration    {}
    ${dev} =    Evaluate    [d for d in $cfg['devices'] if d['udid'] == '${UDID}'][0]
    Should Be Equal    ${dev['last_status']}    failed
    Should Contain    ${dev['last_error']}    could not reach the device
    Should Be Equal As Integers    ${dev['backup_count']}    0
    Should Be Equal As Integers    ${dev['incomplete_count']}    0
    Should Not Be True    ${dev['running']}

An unfinished snapshot does not count and cannot be restored
    Run on node    runagent -m ${module_id} podman exec idevice-backup sh -c 'mkdir -p /data/backups/${UDID}/2026-01-01_00-00-00/${UDID} && echo "{}" > /data/backups/${UDID}/2026-01-01_00-00-00/.incomplete'
    ${dev} =    Device from the configuration
    Should Be Equal As Integers    ${dev['backup_count']}    0
    Should Be Equal As Integers    ${dev['incomplete_count']}    1
    ${list} =    Run task    module/${module_id}/list-backups    {"udid":"${UDID}"}
    Should Not Be True    ${list['backups'][0]['complete']}
    Run task    module/${module_id}/restore-backup    {"udid":"${UDID}","snapshot":"2026-01-01_00-00-00"}    decode_json=${FALSE}    rc_expected=2
    Run on node    runagent -m ${module_id} podman exec idevice-backup rm -rf /data/backups/${UDID}/2026-01-01_00-00-00

*** Keywords ***
Engine container is running
    ${st} =    Run on node    runagent -m ${module_id} podman inspect idevice-backup --format '{{.State.Status}}'
    Should Be Equal As Strings    ${st.strip()}    running

Write backup status
    [Arguments]    ${state}    ${percent}
    Run on node    runagent -m ${module_id} podman exec idevice-backup sh -c 'mkdir -p /data/backups/${UDID} && echo "{\\"state\\":\\"${state}\\",\\"percent\\":${percent},\\"attempt\\":1,\\"attempts\\":3,\\"updated\\":$(date +\%s)}" > /data/backups/${UDID}/.status.json'

Device from the configuration
    ${cfg} =    Run task    module/${module_id}/get-configuration    {}
    ${dev} =    Evaluate    [d for d in $cfg['devices'] if d['udid'] == '${UDID}'][0]
    RETURN    ${dev}
