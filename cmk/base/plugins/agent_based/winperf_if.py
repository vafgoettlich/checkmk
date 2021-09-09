#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2019 tribe29 GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

import dataclasses
from dataclasses import asdict
from typing import (
    Any,
    Collection,
    Dict,
    Iterator,
    List,
    Mapping,
    MutableMapping,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from .agent_based_api.v1 import Attributes, register, Result, State, TableRow
from .agent_based_api.v1.type_defs import CheckResult, DiscoveryResult, InventoryResult, StringTable
from .utils.interfaces import (
    CHECK_DEFAULT_PARAMETERS,
    check_multiple_interfaces,
    discover_interfaces,
    DISCOVERY_DEFAULT_PARAMETERS,
    Interface,
    InventoryParams,
    mac_address_from_hexstring,
    render_mac_address,
    saveint,
)
from .utils.interfaces import Section as SectionInterfaces

Line = Sequence[str]
Lines = Iterator[Line]
NICNames = Sequence[str]
ParsedSubSectionLine = Mapping[str, str]
RawSubSection = List[ParsedSubSectionLine]
SubSection = Dict[str, ParsedSubSectionLine]
AgentTimestamp = Optional[float]
AgentSection = Dict[str, Line]
Section = Tuple[AgentTimestamp, SectionInterfaces, SubSection]


@dataclasses.dataclass
class NICAttr:
    index: int
    counters: Dict[str, Union[str, int]]


NICAttrs = Dict[str, NICAttr]


def _canonize_name(name: str) -> str:
    return name.replace("_", " ").replace("  ", " ").rstrip()


def _line_to_mapping(
    headers: Line,
    line: Line,
) -> Mapping[str, str]:
    """
    >>> _line_to_mapping(["a", "b"], ["1", "2  ", "3"])
    {'a': '1', 'b': '2'}
    """
    return dict(
        zip(
            headers,
            (x.strip() for x in line),
        )
    )


def winperf_if_canonize_nic_name(name: str) -> str:
    return name.replace("_", " ").replace("  ", " ").rstrip()


def winperf_if_normalize_nic_name(
    name: str,
    nic_names: NICNames,
) -> str:
    # Intel[R] PRO 1000 MT-Desktopadapter__3   (perf counter)
    # Intel(R) PRO/1000 MT-Desktopadapter 3    (wmic name)
    # Intel(R) PRO/1000 MT-Desktopadapter #3   (wmic InterfaceDescription)
    mod_nic_name = name
    for from_token, to_token in [("/", " "), ("(", "["), (")", "]"), ("#", " ")]:
        for n in nic_names:
            if from_token in n:
                # we do not modify it if this character is in any of the counter names
                break
        else:
            mod_nic_name = mod_nic_name.replace(from_token, to_token).replace("  ", " ")
    return mod_nic_name


def _parse_winperf_if_sub_section(
    lines: Lines,
    terminating_key: str,
    headers: Line,
) -> RawSubSection:
    section = []
    for line in lines:
        if line[0] == terminating_key:
            break

        if terminating_key == "[teaming_end]":
            section.append(_parse_winperf_if_teaming_section_line(line, headers))
        elif terminating_key == "[dhcp_end]":
            section.append(_parse_winperf_if_dhcp_section_line(line, headers))
    return section


def _parse_winperf_if_dhcp_section_line(
    line: Line,
    headers: Line,
) -> ParsedSubSectionLine:
    # wmic is bugged on some windows versions such that we can't use proper csv output, only
    # visual tables. Those aren't properly split up by the check_mk parser.
    # Try to fix that mess

    # assumption 1: header fields contain no spaces
    num_fields = len(headers)

    # assumption 2: only the leftmost field contains spaces
    lm_field = " ".join(line[: (num_fields - 1) * -1])
    line = [lm_field] + list(line[(len(line) - num_fields + 1) :])
    return dict(zip(headers, [x.rstrip() for x in line]))


def _parse_winperf_if_teaming_section_line(
    line: Line,
    headers: Line,
) -> ParsedSubSectionLine:
    return dict(zip(headers, [x.rstrip() for x in line]))


def _parse_winperf_if_agent_section_timestamp_and_instance_names(
    line: Line,
    lines: Lines,
) -> Tuple[AgentTimestamp, NICNames]:
    # The lines containing timestamp and nic names are consecutive:
    # [u'1418225545.73', u'510']
    # [u'8', u'instances:', 'NAME', ...]
    agent_timestamp = None
    try:
        # There may be other lines with same length but different
        # format. Thus we have to check if the current one is the
        # right one containing the agent timestamp.
        # In second place there's another integer which is a strong
        # hint for the 'agent timestamp'-line.
        agent_timestamp = float(line[0])
        int(line[1])
    except ValueError:
        pass

    try:
        line = next(lines)
    except StopIteration:
        instances: NICNames = []
    else:
        instances = line[2:]
    return agent_timestamp, instances


def _parse_winperf_if_section(
    string_table: StringTable,
) -> Tuple[AgentTimestamp, NICNames, AgentSection, RawSubSection, RawSubSection, RawSubSection]:
    agent_timestamp = None
    raw_nic_names: NICNames = []
    agent_section: AgentSection = {}
    plugin_section: RawSubSection = []
    dhcp_section = []
    teaming_section = []

    plugin_section_header = None
    lines = iter(string_table)
    for line in lines:
        if line[0] == "[dhcp_start]":
            dhcp_section_headers = next(lines)
            dhcp_section.extend(
                _parse_winperf_if_sub_section(lines, "[dhcp_end]", dhcp_section_headers)
            )
            continue

        if line[0].startswith("[teaming_start]"):
            teaming_section_headers = next(lines)
            teaming_section.extend(
                _parse_winperf_if_sub_section(lines, "[teaming_end]", teaming_section_headers)
            )
            continue

        if {"Node", "MACAddress", "Name", "NetConnectionID", "NetConnectionStatus"}.issubset(line):
            plugin_section_header = line
            continue

        if len(line) in (2, 3) and not line[-1].endswith("count"):
            # Do not consider lines containing counters:
            # ['-122', '38840302775', 'bulk_count']
            # ['10', '10000000000', 'large_rawcount']
            (
                agent_timestamp,
                raw_nic_names,
            ) = _parse_winperf_if_agent_section_timestamp_and_instance_names(line, lines)
            plugin_section_header = None
            continue

        if plugin_section_header:
            plugin_section.append(dict(zip(plugin_section_header, [x.strip() for x in line])))

        else:  # agent section
            agent_section.setdefault(line[0], line[1:])

    return (
        agent_timestamp,
        raw_nic_names,
        agent_section,
        plugin_section,
        dhcp_section,
        teaming_section,
    )


def _prepare_winperf_if_dhcp_section(
    nic_names: NICNames,
    dhcp_section: RawSubSection,
) -> SubSection:
    dhcp_info: SubSection = {}
    for row in dhcp_section:
        nic_name = winperf_if_normalize_nic_name(row["Description"], nic_names)
        dhcp_info.setdefault(nic_name, row)
    return dhcp_info


def _prepare_winperf_if_teaming_section(teaming_section: RawSubSection) -> SubSection:
    return {
        guid: {k: v.strip() for k, v in dict_entry.items()}
        for dict_entry in teaming_section
        for guid in dict_entry.get("GUID", "").split(";")
    }


def _prepare_winperf_if_plugin_section(
    nic_names: NICNames,
    plugin_section: RawSubSection,
    teaming_info: SubSection,
) -> SubSection:
    plugin_info: SubSection = {}
    for row in plugin_section:
        # we need to ignore data on interfaces in the optional
        # wmic section which are marked as non-existing, since
        # it may happen that there are non-existing interfaces
        # with the same nic_name as an active one (at least on HP
        # hardware)
        if row.get("NetConnectionStatus") == "4":
            continue

        guid = row.get("GUID")
        if guid in teaming_info:
            guid_entry = teaming_info[guid]
            guid_to_name = dict(
                zip(guid_entry["GUID"].split(";"), guid_entry["MemberDescriptions"].split(";"))
            )
            nic_name = winperf_if_canonize_nic_name(guid_to_name[guid])

        elif "Name" in row:
            nic_name = winperf_if_canonize_nic_name(row["Name"])

        else:
            continue

        # Exact match
        if nic_name in nic_names:
            plugin_info.setdefault(nic_name, row)
            continue

        # In the perf counters the nics have strange suffixes, e.g.
        # Ethernetadapter der AMD-PCNET-Familie 2 - Paketplaner-Miniport, while
        # in wmic it's only named "Ethernetadapter der AMD-PCNET-Familie 2".
        mod_nic_name = winperf_if_normalize_nic_name(nic_name, nic_names)
        if mod_nic_name in nic_names:
            plugin_info.setdefault(mod_nic_name, row)
            continue

        for name in nic_names:
            if name.startswith(mod_nic_name + " "):
                l = len(mod_nic_name)
                if not (name[l:].strip()[0]).isdigit():
                    plugin_info.setdefault(name, row)
                    break
    return plugin_info


# Windows NetConnectionStatus Table to ifOperStatus Table
# 1 up
# 2 down
# 3 testing
# 4 unknown
# 5 dormant
# 6 notPresent
# 7 lowerLayerDown
_CONNECTION_STATES = {
    "0": ("2", "Disconnected"),
    "1": ("2", "Connecting"),
    "2": ("1", "Connected"),
    "3": ("2", "Disconnecting"),
    "4": ("2", "Hardware not present"),
    "5": ("2", "Hardware disabled"),
    "6": ("2", "Hardware malfunction"),
    "7": ("7", "Media disconnected"),
    "8": ("2", "Authenticating"),
    "9": ("2", "Authentication succeeded"),
    "10": ("2", "Authentication failed"),
    "11": ("2", "Invalid address"),
    "12": ("2", "Credentials required"),
}


def _get_if_table(
    nic_attrs: NICAttrs,
    plugin_info: SubSection,
    teaming_info: SubSection,
) -> SectionInterfaces:
    # Now convert the dicts into the format that is needed by if.include
    if_table = []
    for nic_name, nic_attr in nic_attrs.items():
        nic = nic_attr.counters
        nic.setdefault("index", nic_attr.index)
        nic.update(plugin_info.get(nic_name, {}))

        bandwidth = saveint(nic.get("Speed"))
        # Some interfaces report several exabyte as bandwidth when down..
        if bandwidth > 1024 ** 5:
            # Greater than petabyte
            bandwidth = 0

        # Automatically group teamed interfaces
        guid = nic.get("GUID")
        group = teaming_info.get(guid, {}).get("TeamName") if isinstance(guid, str) else None

        # if we have no status, but link information, we assume IF is connected
        connection_status = nic.get("NetConnectionStatus")
        if not connection_status:
            connection_status = "2"

        oper_status, oper_status_name = _CONNECTION_STATES[str(connection_status)]

        if_table.append(
            Interface(
                index=str(nic["index"]),
                descr=nic_name,
                alias=str(nic.get("NetConnectionID", nic_name)),
                type="loopback" in nic_name.lower() and "24" or "6",
                speed=bandwidth or saveint(nic["10"]),
                oper_status=oper_status,
                in_octets=saveint(nic["-246"]),
                in_ucast=saveint(nic["14"]),
                in_bcast=saveint(nic["16"]),
                in_discards=saveint(nic["18"]),
                in_errors=saveint(nic["20"]),
                out_octets=saveint(nic["-4"]),
                out_ucast=saveint(nic["26"]),
                out_bcast=saveint(nic["28"]),
                out_discards=saveint(nic["30"]),
                out_errors=saveint(nic["32"]),
                out_qlen=saveint(nic["34"]),
                phys_address=mac_address_from_hexstring(str(nic.get("MACAddress", ""))),
                oper_status_name=oper_status_name,
                group=group,
            )
        )

    return if_table


def _parse_winperf_if_nic_attrs(
    raw_nic_names: NICNames,
    agent_section: AgentSection,
) -> NICAttrs:
    nic_attrs: NICAttrs = {}
    for idx, raw_nic_name in enumerate(raw_nic_names):
        nic_name = winperf_if_canonize_nic_name(raw_nic_name)
        nic_attrs.setdefault(
            nic_name,
            NICAttr(idx + 1, {counter: int(line[idx]) for counter, line in agent_section.items()}),
        )
    return nic_attrs


def parse_winperf_if_(string_table: StringTable) -> Section:
    (
        agent_timestamp,
        raw_nic_names,
        agent_section,
        plugin_section,
        dhcp_section,
        teaming_section,
    ) = _parse_winperf_if_section(string_table)

    # Based on the raw nic names we structure the interface table
    nic_attrs = _parse_winperf_if_nic_attrs(raw_nic_names, agent_section)
    nic_names = list(nic_attrs)

    teaming_info = _prepare_winperf_if_teaming_section(teaming_section)
    plugin_info = _prepare_winperf_if_plugin_section(nic_names, plugin_section, teaming_info)

    if_table = _get_if_table(nic_attrs, plugin_info, teaming_info)
    dhcp_info = _prepare_winperf_if_dhcp_section(nic_names, dhcp_section)
    return agent_timestamp, if_table, dhcp_info


class SectionCounters(NamedTuple):
    timestamp: Optional[float]
    interfaces: Mapping[str, Interface]
    found_windows_if: bool
    found_mk_dhcp_enabled: bool


def _parse_timestamp_and_instance_names(
    line: Line,
    lines: Lines,
) -> Tuple[Optional[float], Sequence[str]]:
    # The lines containing timestamp and nic names are consecutive:
    # [u'1418225545.73', u'510']
    # [u'8', u'instances:', 'NAME', ...]
    agent_timestamp = None
    try:
        # There may be other lines with same length but different
        # format. Thus we have to check if the current one is the
        # right one containing the agent timestamp.
        # In second place there's another integer which is a strong
        # hint for the 'agent timestamp'-line.
        agent_timestamp = float(line[0])
        int(line[1])
    except ValueError:
        pass

    try:
        line = next(lines)
    except StopIteration:
        instances: Sequence[str] = []
    else:
        instances = line[2:]
    return agent_timestamp, instances


def _parse_counters(
    raw_nic_names: Sequence[str],
    agent_section: Mapping[str, Line],
) -> Mapping[str, Interface]:
    interfaces: MutableMapping[str, Interface] = {}
    for idx, raw_nic_name in enumerate(raw_nic_names):
        name = _canonize_name(raw_nic_name)
        counters = {counter: int(line[idx]) for counter, line in agent_section.items()}
        interfaces.setdefault(
            name,
            Interface(
                index=str(idx + 1),
                descr=name,
                alias=name,
                type="loopback" in name.lower() and "24" or "6",
                speed=counters["10"],
                oper_status="1",
                in_octets=counters["-246"],
                in_ucast=counters["14"],
                in_bcast=counters["16"],
                in_discards=counters["18"],
                in_errors=counters["20"],
                out_octets=counters["-4"],
                out_ucast=counters["26"],
                out_bcast=counters["28"],
                out_discards=counters["30"],
                out_errors=counters["32"],
                out_qlen=counters["34"],
                oper_status_name="Connected",
            ),
        )
    return interfaces


def _filter_out_deprecated_plugin_lines(
    string_table: StringTable,
) -> Tuple[StringTable, bool, bool]:
    native_agent_data: StringTable = []
    found_windows_if = False
    found_mk_dhcp_enabled = False

    for line in (lines := iter(string_table)):

        # from mk_dhcp_enabled.bat
        if line[0].startswith("[dhcp_start]"):
            found_mk_dhcp_enabled = True
            for l in lines:
                if l[0].startswith("[dhcp_end]"):
                    break
            continue

        # from windows_if.ps1 or wmic_if.bat
        if line[0].startswith("[teaming_start]"):
            found_windows_if = True
            for l in lines:
                if l[0].startswith("[teaming_end]"):
                    break
            continue

        # from windows_if.ps1 or wmic_if.bat
        if {"Node", "MACAddress", "Name", "NetConnectionID", "NetConnectionStatus"}.issubset(line):
            found_windows_if = True
            for l in lines:
                if len(l) < 4:
                    native_agent_data.append(l)
                    break
            continue

        native_agent_data.append(line)

    return native_agent_data, found_windows_if, found_mk_dhcp_enabled


def parse_winperf_if(string_table: StringTable) -> SectionCounters:
    agent_timestamp = None
    raw_nic_names: Sequence[str] = []
    agent_section: MutableMapping[str, Line] = {}

    # There used to be only a single winperf_if-section which contained both the native agent data
    # and plugin data which is now located in the sections winperf_if_... For compatibily reasons,
    # we still handle this case by filtering out the plugin data and advising the user to update
    # the agent.
    (
        string_table_filtered,
        found_windows_if,
        found_mk_dhcp_enabled,
    ) = _filter_out_deprecated_plugin_lines(string_table)

    for line in (lines := iter(string_table_filtered)):  # pylint:disable=superfluous-parens
        if len(line) in (2, 3) and not line[-1].endswith("count"):
            # Do not consider lines containing counters:
            # ['-122', '38840302775', 'bulk_count']
            # ['10', '10000000000', 'large_rawcount']
            agent_timestamp, raw_nic_names = _parse_timestamp_and_instance_names(
                line,
                lines,
            )
        else:
            agent_section.setdefault(line[0], line[1:])

    return SectionCounters(
        timestamp=agent_timestamp,
        interfaces=_parse_counters(
            raw_nic_names,
            agent_section,
        ),
        found_windows_if=found_windows_if,
        found_mk_dhcp_enabled=found_mk_dhcp_enabled,
    )


register.agent_section(
    name="winperf_if",
    parse_function=parse_winperf_if_,
    supersedes=["if", "if64"],
)


class TeamingData(NamedTuple):
    team_name: str
    name: str


SectionTeaming = Mapping[str, TeamingData]


def parse_winperf_if_teaming(string_table: StringTable) -> SectionTeaming:
    return {
        guid: TeamingData(
            team_name=line_dict["TeamName"],
            name=_canonize_name(name),
        )
        for line_dict in (
            _line_to_mapping(
                string_table[0],
                line,
            )
            for line in string_table[1:]
        )
        for guid, name in zip(
            line_dict["GUID"].split(";"),
            line_dict["MemberDescriptions"].split(";"),
        )
    }


# TODO: register once restructuring is complete, otherwise a unit test for non-used sections fails
# register.agent_section(
#     name='winperf_if_teaming',
#     parse_function=parse_winperf_if_teaming,
# )


class AdditionalIfData(NamedTuple):
    name: str
    alias: str
    speed: int
    oper_status: str
    oper_status_name: str
    mac_address: str
    guid: Optional[str]  # wmic_if.bat does not produce this


SectionExtended = Collection[AdditionalIfData]

# Windows NetConnectionStatus Table to ifOperStatus Table
# 1 up
# 2 down
# 3 testing
# 4 unknown
# 5 dormant
# 6 notPresent
# 7 lowerLayerDown
_NetConnectionStatus_TO_OPER_STATUS: Mapping[str, Tuple[str, str]] = {
    "0": ("2", "Disconnected"),
    "1": ("2", "Connecting"),
    "2": ("1", "Connected"),
    "3": ("2", "Disconnecting"),
    "4": ("2", "Hardware not present"),
    "5": ("2", "Hardware disabled"),
    "6": ("2", "Hardware malfunction"),
    "7": ("7", "Media disconnected"),
    "8": ("2", "Authenticating"),
    "9": ("2", "Authentication succeeded"),
    "10": ("2", "Authentication failed"),
    "11": ("2", "Invalid address"),
    "12": ("2", "Credentials required"),
}


def parse_winperf_if_win32_networkadapter(string_table: StringTable) -> SectionExtended:
    return [
        AdditionalIfData(
            name=_canonize_name(line_dict["Name"]),
            alias=line_dict["NetConnectionID"],
            # Some interfaces report several exabyte as bandwidth when down ...
            speed=speed
            if "Speed" in line_dict and (speed := saveint(line_dict["Speed"])) <= 1024 ** 5
            else 0,
            oper_status=oper_status,
            oper_status_name=oper_status_name,
            mac_address=line_dict["MACAddress"],
            guid=line_dict.get("GUID"),
        )
        for line_dict in (
            _line_to_mapping(
                string_table[0],
                line,
            )
            for line in string_table[1:]
        )
        for oper_status, oper_status_name in [
            _NetConnectionStatus_TO_OPER_STATUS.get(
                line_dict["NetConnectionStatus"],
                ("2", "Disconnected"),
            )
        ]
        # we need to ignore data on interfaces in the optional
        # wmic section which are marked as non-existing, since
        # it may happen that there are non-existing interfaces
        # with the same nic_name as an active one (at least on HP
        # hardware)
        if line_dict["NetConnectionStatus"] != "4"
    ]


# TODO: register once restructuring is complete, otherwise a unit test for non-used sections fails
# register.agent_section(
#     name='winperf_if_win32_networkadapter',
#     parse_function=parse_winperf_if_win32_networkadapter,
#     parsed_section_name='winperf_if_extended',
# )

SectionDHPC = Collection[Mapping[str, str]]


def parse_winperf_if_dhcp(string_table: StringTable) -> SectionDHPC:
    # wmic is bugged on some windows versions such that we can't use proper csv output, only
    # visual tables. Those aren't properly split up by the check_mk parser.
    # Try to fix that mess
    return [
        _line_to_mapping(
            # assumption 1: the two header fields contain no spaces
            string_table[0],
            [
                # assumption 2: only the description contains spaces
                " ".join(line[:-1]),
                line[-1],
            ],
        )
        for line in string_table[1:]
    ]


# TODO: register once restructuring is complete, otherwise a unit test for non-used sections fails
# register.agent_section(
#     name='winperf_if_dhcp',
#     parse_function=parse_winperf_if_dhcp,
# )


def _normalize_name(
    name: str,
    names: Collection[str],
) -> str:
    """
    >>> _normalize_name("my interface #3", ["my interface 1", "my interface 2", "my interface 3"])
    'my interface 3'
    >>> _normalize_name("my interface(R)", ["my interface[R]", "another interface(?)"])
    'my interface(R)'
    """
    # Intel[R] PRO 1000 MT-Desktopadapter__3   (perf counter)
    # Intel(R) PRO/1000 MT-Desktopadapter 3    (wmic name)
    # Intel(R) PRO/1000 MT-Desktopadapter #3   (wmic InterfaceDescription)
    mod_name = name
    for from_token, to_token in [("/", " "), ("(", "["), (")", "]"), ("#", " ")]:
        for n in names:
            if from_token in n:
                # we do not modify it if this character is in any of the counter names
                break
        else:
            mod_name = mod_name.replace(from_token, to_token).replace("  ", " ")
    return mod_name


def _match_add_data_to_interfaces(
    interface_names: Collection[str],
    section_teaming: SectionTeaming,
    section_extended: SectionExtended,
):
    additional_data: MutableMapping[str, AdditionalIfData] = {}

    for add_data in section_extended:
        if add_data.guid is not None and (teaming_entry := section_teaming.get(add_data.guid)):
            name = teaming_entry.name
        else:
            name = add_data.name

        # Exact match
        if name in interface_names:
            additional_data.setdefault(name, add_data)
            continue

        # In the perf counters the nics have strange suffixes, e.g.
        # Ethernetadapter der AMD-PCNET-Familie 2 - Paketplaner-Miniport, while
        # in wmic it's only named "Ethernetadapter der AMD-PCNET-Familie 2".
        if (
            mod_name := _normalize_name(
                name,
                interface_names,
            )
        ) in interface_names:
            additional_data.setdefault(mod_name, add_data)
            continue

        for name in interface_names:
            if name.startswith(mod_name + " "):
                l = len(mod_name)
                if not (name[l:].strip()[0]).isdigit():
                    additional_data.setdefault(name, add_data)
                    break

    return additional_data


def _merge_sections(
    interfaces: Mapping[str, Interface],
    section_teaming: Optional[SectionTeaming],
    section_extended: Optional[SectionExtended],
) -> SectionInterfaces:

    section_teaming = section_teaming or {}
    additional_data = (
        _match_add_data_to_interfaces(
            interfaces,
            section_teaming,
            section_extended,
        )
        if section_extended
        else {}
    )

    return [
        Interface(
            **{
                **asdict(interface),
                **dict(
                    alias=add_if_data.alias,
                    speed=add_if_data.speed or interface.speed,
                    group=section_teaming[add_if_data.guid].team_name
                    if add_if_data.guid in section_teaming
                    else None,
                    oper_status=add_if_data.oper_status,
                    oper_status_name=add_if_data.oper_status_name,
                    phys_address=mac_address_from_hexstring(add_if_data.mac_address),
                ),
            }
        )
        if (add_if_data := additional_data.get(name))
        else interface
        for name, interface in interfaces.items()
    ]


def discover_winperf_if(
    params: Sequence[Mapping[str, Any]],
    section: Section,
) -> DiscoveryResult:
    yield from discover_interfaces(
        params,
        section[1],
    )


def check_winperf_if(
    item: str,
    params: Mapping[str, Any],
    section: Section,
) -> CheckResult:
    agent_timestamp, if_table, dhcp_info = section
    yield from check_multiple_interfaces(
        item,
        params,
        if_table,
        group_name="Teaming",
        timestamp=agent_timestamp,
    )

    dhcp_result = check_if_dhcp(item, dhcp_info)
    if dhcp_result:
        yield dhcp_result


def check_if_dhcp(
    item: str,
    dhcp_info: SubSection,
) -> Optional[Result]:
    for nic_name, attrs in dhcp_info.items():
        try:
            match = int(attrs["index"]) == int(item)
        except (KeyError, ValueError):
            match = nic_name == item

        if not match:
            continue

        dhcp_enabled = attrs["DHCPEnabled"]
        if dhcp_enabled == "TRUE":
            return Result(
                state=State.WARN,
                summary="DHCP: enabled",
            )
        return Result(
            state=State.OK,
            summary="DHCP: %s" % dhcp_enabled,
        )
    return None


def _check_dhcp(
    item: str,
    interface_names: Collection[str],
    section_dhcp: SectionDHPC,
) -> Optional[Result]:
    for dhcp_data in section_dhcp:
        try:
            match = int(dhcp_data["index"]) == int(item)
        except (KeyError, ValueError):
            match = (
                _normalize_name(
                    dhcp_data["Description"],
                    interface_names,
                )
                == item
            )

        if not match:
            continue

        if dhcp_data["DHCPEnabled"] == "TRUE":
            return Result(
                state=State.WARN,
                summary="DHCP: enabled",
            )
        return Result(
            state=State.OK,
            summary="DHCP: disabled",
        )
    return None


def _check_deprecated_plugins(
    windows_if: bool,
    mk_dhcp_enabled: bool,
) -> CheckResult:
    if windows_if:
        yield Result(
            state=State.CRIT,
            summary="Detected deprecated version of plugin 'windows_if.ps1' or 'wmic_if.bat' "
            "(bakery ruleset 'Network interfaces on Windows'). Please update agent.",
        )
    if mk_dhcp_enabled:
        yield Result(
            state=State.CRIT,
            summary="Detected deprecated version of plugin 'mk_dhcp_enabled.bat'. Please update agent.",
        )


register.check_plugin(
    name="winperf_if",
    service_name="Interface %s",
    discovery_ruleset_name="inventory_if_rules",
    discovery_ruleset_type=register.RuleSetType.ALL,
    discovery_default_parameters=dict(DISCOVERY_DEFAULT_PARAMETERS),
    discovery_function=discover_winperf_if,
    check_ruleset_name="if",
    check_default_parameters=CHECK_DEFAULT_PARAMETERS,
    check_function=check_winperf_if,
)


def inventory_winperf_if(section: Section) -> InventoryResult:
    params: InventoryParams = {
        "usage_port_types": [
            "6",
            "32",
            "62",
            "117",
            "127",
            "128",
            "129",
            "180",
            "181",
            "182",
            "205",
            "229",
        ],
    }
    total_ethernet_ports = 0
    available_ethernet_ports = 0

    for interface in sorted(
        section[1],
        key=lambda iface: int(iface.index[-1]),
    ):

        if interface.type in ("231", "232") or not interface.speed:
            continue  # Useless entries for "TenGigabitEthernet2/1/21--Uncontrolled"
            # Ignore useless half-empty tables (e.g. Viprinet-Router)

        if_available = None
        if interface.type in params["usage_port_types"]:
            total_ethernet_ports += 1
            if if_available := interface.oper_status == "2":
                available_ethernet_ports += 1

        yield TableRow(
            path=["networking", "interfaces"],
            key_columns={
                "index": int(interface.index[-1]),
                "description": interface.descr,
                "alias": interface.alias,
                "speed": int(interface.speed),
                "phys_address": render_mac_address(interface.phys_address),
                "oper_status": int(interface.oper_status[0]),
                "port_type": int(interface.type),
                "available": if_available,
            },
        )

    yield Attributes(
        path=["networking"],
        inventory_attributes={
            "available_ethernet_ports": available_ethernet_ports,
            "total_ethernet_ports": total_ethernet_ports,
            "total_interfaces": len(section[1]),
        },
    )


# TODO: make this plugin use the inventory ruleset inv_if
register.inventory_plugin(
    name="winperf_if",
    inventory_function=inventory_winperf_if,
)
