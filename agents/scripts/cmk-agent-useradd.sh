#!/bin/sh
# Copyright (C) 2019 tribe29 GmbH - License: Checkmk Enterprise License
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

HOMEDIR="/var/lib/cmk-agent"

usage() {
    cat >&2 <<HERE
Usage: ${0} new|upgrade
Create the system user 'cmk-agent' for the Checkmk agent package.
HERE
    exit 1
}

_allow_legacy_pull() {
    cat >"${HOMEDIR}/allow-legacy-pull" <<HERE
This file has been placed as a marker for cmk-agent-ct
to allow unencrypted legacy agent pull mode.
It will be removed automatically on first successful agent registration.
You can remove it manually to disallow legacy mode, but note that
for regular operation you need to register the agent anyway.
HERE
}

main() {
    case "$1" in
        new | upgrade) ;;
        *) usage ;;
    esac

    # add cmk-agent system user
    echo "Creating/updating cmk-agent user account..."
    comment="Checkmk agent system user"
    usershell="/bin/false"

    if id "cmk-agent" >/dev/null 2>&1; then
        # check that the existing user is as expected
        existing="$(getent passwd "cmk-agent")"
        existing="${existing#cmk-agent:*:*:*:}"
        expected="${comment}:${HOMEDIR}:${usershell}"
        if [ "${existing}" != "${expected}" ]; then
            echo "cmk-agent user found:  expected '${expected}'" >&2
            echo "                      but found '${existing}'" >&2
            echo "Refusing to install with unexpected user properties." >&2
            exit 1
        fi
        unset existing expected
    else
        useradd \
            --comment "${comment}" \
            --system \
            --home-dir "${HOMEDIR}" \
            --no-create-home \
            --shell "${usershell}" \
            "cmk-agent" || exit 1
        user_is_new="yes"
    fi

    # Create home directory manually instead of doing this on user creation,
    # because it might already exist with wrong ownership
    mkdir -p ${HOMEDIR}
    chown -R cmk-agent:cmk-agent ${HOMEDIR}

    [ "${user_is_new}" ] && [ "$1" = "upgrade" ] && _allow_legacy_pull

    unset homedir comment usershell

}

main "$@"
