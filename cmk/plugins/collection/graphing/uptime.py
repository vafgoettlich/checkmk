#!/usr/bin/env python3
# Copyright (C) 2023 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

from cmk.graphing.v1 import metrics, perfometers, Title

UNIT_TIME = metrics.Unit(metrics.TimeNotation())

metric_uptime = metrics.Metric(
    name="uptime",
    title=Title("Uptime"),
    unit=UNIT_TIME,
    color=metrics.Color.DARK_YELLOW,
)

perfometer_uptime = perfometers.Perfometer(
    name="uptime",
    focus_range=perfometers.FocusRange(
        perfometers.Closed(0),
        perfometers.Open(30 * 24 * 3600),
    ),
    segments=["uptime"],
)
