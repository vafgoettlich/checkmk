#!/usr/bin/env python3
# Copyright (C) 2023 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

from collections.abc import Sequence
from enum import Enum

import xmltodict
from pydantic import BaseModel, BeforeValidator
from typing_extensions import Annotated

from .robotmk_parse_xml import Rebot


class AttemptOutcome(Enum):
    AllTestsPassed = "AllTestsPassed"
    TestFailures = "TestFailures"
    RobotFrameworkFailure = "RobotFrameworkFailure"
    EnvironmentFailure = "EnvironmentFailure"
    TimedOut = "TimedOut"


class AttemptOutcomeOtherError(BaseModel, frozen=True):
    OtherError: str


def _parse_xml(xml_value: str) -> Rebot:
    return Rebot.model_validate(xmltodict.parse(xml_value))


class RebotResult(BaseModel, frozen=True):
    xml: Annotated[Rebot, BeforeValidator(_parse_xml)]
    html_base64: str


class RebotOutcomeResult(BaseModel, frozen=True):
    Ok: RebotResult


class RebotOutcomeError(BaseModel, frozen=True):
    Error: str


class AttemptsOutcome(BaseModel, frozen=True):
    attempts: Sequence[AttemptOutcome | AttemptOutcomeOtherError]
    rebot: RebotOutcomeResult | RebotOutcomeError | None


class ExecutionReport(BaseModel, frozen=True):
    Executed: AttemptsOutcome


class ExecutionReportAlreadyRunning(Enum):
    AlreadyRunning = "AlreadyRunning"


class SuiteExecutionReport(BaseModel, frozen=True):
    suite_name: str
    outcome: ExecutionReport | ExecutionReportAlreadyRunning


def parse(string_table: Sequence[Sequence[str]]) -> Sequence[SuiteExecutionReport]:
    return [SuiteExecutionReport.model_validate_json(line[0]) for line in string_table]
