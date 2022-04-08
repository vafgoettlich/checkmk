# , user!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2020 tribe29 GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.
"""
User Roles

Checkmk always assigns permissions to users via roles — never directly. A role is nothing more than a list of permissions.
It is important that you understand that roles define the level of permissions and not the reference to any hosts or services.
That is what contact groups are for.

As standard Checkmk comes with the following three predefined roles, which can never be deleted, but can be customised at will:

When adding a new custom role, it will be a clone of one of the default roles, i.e it will automatically inherit all of the
permissions of that default role.  Also, when new permissions are added, builtin roles will automatically be permitted or not
permitted and the custom roles will also inherit those permission settings.

* Role: admin
Permissions:  All permissions - especially the right to change permissions.
Function: The Checkmk administrator who is in charge of the monitoring system itself.

* Role: user
Permissions: May only see their own hosts and services, may only make changes in the web interface in folders
authorized for them and generally may not do anything that affects other users.
Function: The normal Checkmk user who uses monitoring and responds to notifications.

* Role: guest
Permissions: May see everything but not change anything.
Function: 'Guest' is intended simply for 'watching', with all guests sharing a common account.
For example, useful for public status monitors hanging on a wall.

"""

from typing import Any, Mapping

from marshmallow import ValidationError

from cmk.gui.http import Response
from cmk.gui.logged_in import user
from cmk.gui.plugins.openapi.restful_objects import (
    constructors,
    Endpoint,
    permissions,
    request_schemas,
    response_schemas,
)
from cmk.gui.plugins.openapi.restful_objects.type_defs import DomainObject
from cmk.gui.plugins.openapi.utils import problem, serve_json
from cmk.gui.type_defs import UserRole
from cmk.gui.utils.roles import get_role_permissions
from cmk.gui.watolib import userroles
from cmk.gui.watolib.userroles import RoleID

PERMISSIONS = permissions.Perm("wato.users")

RW_PERMISSIONS = permissions.AllPerm(
    [
        permissions.Perm("wato.edit"),
        PERMISSIONS,
    ]
)


def serialize_user_role(user_role: UserRole) -> DomainObject:
    extns = {
        "alias": user_role.alias,
        "builtin": user_role.builtin,
        "permissions": get_role_permissions().get(user_role.name),
    }
    if not user_role.builtin:
        extns["basedon"] = user_role.basedon

    return constructors.domain_object(
        domain_type="user_role",
        identifier=user_role.name,
        title=user_role.alias,
        extensions=extns,
        editable=True,
        deletable=not (user_role.builtin),
    )


@Endpoint(
    constructors.object_href("user_role", "{role_id}"),
    "cmk/show",
    method="get",
    tag_group="Setup",
    path_params=[
        {
            "role_id": request_schemas.UserRoleID(
                required=True,
                description="An existing user role.",
                example="userx",
                presence="should_exist",
            )
        }
    ],
    response_schema=response_schemas.UserRoleObject,
    permissions_required=PERMISSIONS,
)
def show_user_role(params: Mapping[str, Any]) -> Response:
    """Show a user role"""
    user.need_permission("wato.users")
    user_role = userroles.get_role(RoleID(params["role_id"]))
    return serve_json(data=serialize_user_role(user_role))


@Endpoint(
    constructors.collection_href("user_role"),
    ".../collection",
    method="get",
    tag_group="Setup",
    response_schema=response_schemas.UserRoleCollection,
    permissions_required=PERMISSIONS,
)
def list_user_roles(params: Mapping[str, Any]) -> Response:
    """Show all user roles"""
    user.need_permission("wato.users")

    return serve_json(
        constructors.collection_object(
            domain_type="user_role",
            value=[
                serialize_user_role(user_role) for user_role in userroles.get_all_roles().values()
            ],
        )
    )


@Endpoint(
    constructors.collection_href("user_role"),
    "cmk/create",
    method="post",
    tag_group="Setup",
    request_schema=request_schemas.CreateUserRole,
    response_schema=response_schemas.UserRoleObject,
    permissions_required=RW_PERMISSIONS,
)
def create_userrole(params: Mapping[str, Any]) -> Response:
    """Create/clone a user role"""
    user.need_permission("wato.users")
    user.need_permission("wato.edit")
    body = params["body"]
    cloned_user_role = userroles.clone_role(
        role_id=RoleID(body["role_id"]),
        new_role_id=body.get("new_role_id"),
        new_alias=body.get("new_alias"),
    )
    return serve_json(serialize_user_role(cloned_user_role))


@Endpoint(
    constructors.object_href("user_role", "{role_id}"),
    ".../delete",
    method="delete",
    tag_group="Setup",
    path_params=[
        {
            "role_id": request_schemas.UserRoleID(
                required=True,
                description="An existing custom user role that you want to delete.",
                example="userx",
                presence="should_exist",
                userrole_type="should_be_custom",
            )
        }
    ],
    output_empty=True,
    permissions_required=RW_PERMISSIONS,
)
def delete_userrole(params: Mapping[str, Any]) -> Response:
    """Delete a user role"""
    user.need_permission("wato.users")
    user.need_permission("wato.edit")
    role_id = RoleID(params["role_id"])
    userroles.delete_role(RoleID(role_id))
    return Response(status=204)


@Endpoint(
    constructors.object_href("user_role", "{role_id}"),
    ".../update",
    method="put",
    tag_group="Setup",
    request_schema=request_schemas.EditUserRole,
    response_schema=response_schemas.UserRoleObject,
    path_params=[
        {
            "role_id": request_schemas.UserRoleID(
                required=True,
                description="An existing user role.",
                example="userx",
                presence="should_exist",
            )
        }
    ],
    permissions_required=RW_PERMISSIONS,
)
def edit_userrole(params: Mapping[str, Any]) -> Response:
    """Edit a user role"""
    user.need_permission("wato.users")
    user.need_permission("wato.edit")
    existing_roleid = params["role_id"]
    body = params["body"]
    userrole_to_edit: UserRole = userroles.get_role(RoleID(existing_roleid))

    if new_alias := body.get("new_alias", userrole_to_edit.alias):
        try:
            userroles.validate_new_alias(userrole_to_edit.alias, new_alias)
        except ValidationError as exc:
            return problem(status=400, title="Invalid alias", detail=str(exc))
        userrole_to_edit.alias = new_alias

    if new_roleid := body.get("new_role_id"):
        try:
            userroles.validate_new_roleid(userrole_to_edit.name, new_roleid)
        except ValidationError as exc:
            return problem(status=400, title="Invalid role id", detail=str(exc))
        userrole_to_edit.name = new_roleid

    if basedon := body.get("new_basedon"):
        if userrole_to_edit.builtin:
            return problem(
                status=400,
                title="Builtin role",
                detail="You can't edit the basedon value of a builtin role.",
            )
        userrole_to_edit.basedon = basedon

    if new_permissions := body.get("new_permissions"):
        userroles.update_permissions(userrole_to_edit, new_permissions.items())

    userroles.save_updated_role(userrole_to_edit, existing_roleid)
    return serve_json(data=serialize_user_role(userrole_to_edit))
