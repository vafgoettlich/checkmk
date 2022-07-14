#!/usr/bin/env python3
# Copyright (C) 2019 tribe29 GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

#########################################################################################
#                                                                                       #
#                                 !!   W A T C H   O U T   !!                           #
#                                                                                       #
#   The logwatch plugin is notorious for being an exception to just about every rule    #
#   or best practice that applies to check plugin development.                          #
#   It is highly discouraged to use this a an example!                                  #
#                                                                                       #
#########################################################################################

from typing import Dict, Literal, Optional

from .agent_based_api.v1 import register
from .agent_based_api.v1.type_defs import StringTable
from .utils.logwatch import ItemData, Section


def _extract_error_message(line: str) -> Optional[str]:
    """Check line for error message
    Return None if no error message is found, error_message otherwise
    """
    if not line.startswith("CANNOT READ CONFIG FILE: "):
        return None
    return "Error in agent configuration: %s" % line[25:]


def _extract_item_attribute(
    line: str,
) -> tuple[str | None, Literal["cannotopen", "missing", "ok"]]:
    """Check line for next item subsection
    Return None if no item subsection is found, (item, attribute) otherwise

    >>> _extract_item_attribute("W no item here")
    (None, 'ok')
    >>> _extract_item_attribute("[[[vanished_file:missing]]]")
    ('vanished_file', 'missing')
    >>> _extract_item_attribute("[[[this is nice]]]")
    ('this is nice', 'ok')
    >>> _extract_item_attribute("[[[filename:with:colons]]]")
    ('filename:with:colons', 'ok')
    """
    if line[:3] != "[[[" or line[-3:] != "]]]":
        return None, "ok"

    header = line[3:-3]
    logfile_name, *attribute = header.rsplit(":", 1)
    match attribute:
        case ("missing" | "cannotopen" as value,):
            return logfile_name, value
        case _:
            return header, "ok"


def parse_logwatch(string_table: StringTable) -> Section:
    """
    >>> import pprint
    >>> section = parse_logwatch([
    ...     ['[[[mylog]]]'],
    ...     ['C', 'whoha!', 'Someone', 'mooped!'],
    ...     ['[[[missinglog:missing]]]'],
    ...     ['[[[unreadablelog:cannotopen]]]'],
    ...     ['[[[empty.log]]]'],
    ...     ['[[[my_other_log]]]'],
    ...     ['W', 'watch', 'your', 'step!'],
    ... ])
    >>> pprint.pprint(section.errors)
    []
    >>> pprint.pprint(section.logfiles)
    {'empty.log': {'attr': 'ok', 'lines': []},
     'missinglog': {'attr': 'missing', 'lines': []},
     'my_other_log': {'attr': 'ok', 'lines': ['W watch your step!']},
     'mylog': {'attr': 'ok', 'lines': ['C whoha! Someone mooped!']},
     'unreadablelog': {'attr': 'cannotopen', 'lines': []}}
    """

    errors = []
    logfiles: Dict[str, ItemData] = {}

    item_data: Optional[ItemData] = None

    for raw_line in string_table:
        line = " ".join(raw_line)

        error_msg = _extract_error_message(line)
        if error_msg is not None:
            errors.append(error_msg)
            continue

        item, attribute = _extract_item_attribute(line)
        if item is not None:
            item_data = logfiles.setdefault(item, {"attr": attribute, "lines": []})
            continue

        if item_data is not None:
            item_data["lines"].append(line)

    return Section(errors=errors, logfiles=logfiles)


register.agent_section(
    name="logwatch",
    parse_function=parse_logwatch,
)
