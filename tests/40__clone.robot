*** Settings ***
Library     SSHLibrary
Resource    api.resource

*** Test Cases ***
Clone the module
    [Documentation]    "Clone and Move" is a platform behaviour: the core copies
    ...                volumes and state/, the module re-creates its route through
    ...                clone-module/50configure_module.
    ${out} =    Run task    cluster/clone-module    {"module":"${module_id}","replace":false,"node":1}
    Set Global Variable    ${clone_id}    ${out['module_id']}
    Should Not Be Equal    ${clone_id}    ${module_id}

The clone has its route, settings and secrets
    ${orig} =    Run task    module/traefik1/get-route    {"instance":"${module_id}"}
    ${route} =    Run task    module/traefik1/get-route    {"instance":"${clone_id}"}
    Should Be Equal    ${route['host']}    ${orig['host']}
    Should Not Be Equal    ${route['url']}    ${orig['url']}
    # only values that are set count: configure-module may add empty entries
    Secrets are kept out of the module environment    ${clone_id}
    ${a} =    Run on node    runagent -m ${module_id} bash -c 'grep -v "=$" "$AGENT_STATE_DIR/passwords.env" | sort | sha256sum'
    ${b} =    Run on node    runagent -m ${clone_id} bash -c 'grep -v "=$" "$AGENT_STATE_DIR/passwords.env" | sort | sha256sum'
    Should Be Equal    ${a}    ${b}

Remove the clone
    Run on node    remove-module --no-preserve ${clone_id}
