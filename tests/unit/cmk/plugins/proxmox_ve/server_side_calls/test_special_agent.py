#!/usr/bin/env python3
# Copyright (C) 2023 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

from collections.abc import Mapping, Sequence

import pytest

from cmk.plugins.proxmox_ve.server_side_calls.special_agent import (
    special_agent_proxmox_ve as config,
)
from cmk.server_side_calls.v1 import (
    HostConfig,
    HTTPProxy,
    IPv4Config,
    PlainTextSecret,
    StoredSecret,
)


@pytest.mark.parametrize(
    ["params", "expected_result"],
    [
        pytest.param(
            {
                "username": "user",
                "password": ("password", "passwd"),
                "port": "443",
                "no-cert-check": True,
                "timeout": "30",
                "log-cutoff-weeks": "4",
            },
            [
                "-u",
                "user",
                "-p",
                PlainTextSecret(value="passwd"),
                "--port",
                "443",
                "--no-cert-check",
                "--timeout",
                "30",
                "--log-cutoff-weeks",
                "4",
                "testhost",
            ],
            id="explicit_password",
        ),
        pytest.param(
            {
                "username": "user",
                "password": ("store", "passwd"),
                "timeout": "40",
            },
            [
                "-u",
                "user",
                "-p",
                StoredSecret(value="passwd"),
                "--timeout",
                "40",
                "testhost",
            ],
            id="password_from_store",
        ),
    ],
)
def test_agent_proxmox_ve_arguments(
    params: Mapping[str, object], expected_result: Sequence[str]
) -> None:
    # Assemble
    host_config = HostConfig(
        name="testhost",
        ipv4_config=IPv4Config(address="hurz"),
    )
    http_proxies = {"my_proxy": HTTPProxy(id="my_proxy", name="My Proxy", url="proxy.com")}
    # Act
    commands = list(config(params, host_config, http_proxies))
    # Assert
    assert len(commands) == 1
    command = commands[0].command_arguments
    assert command == expected_result
