# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""Commands for subreports nested under a parent report schedule."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from functools import partial
from typing import Any

from flask_babel import gettext as _
from marshmallow import ValidationError

from superset import db, security_manager
from superset.commands.base import BaseCommand
from superset.commands.report.exceptions import (
    ReportScheduleForbiddenError,
    ReportScheduleNotFoundError,
    SubreportCreateFailedError,
    SubreportDeleteFailedError,
    SubreportInvalidError,
    SubreportNotFoundError,
    SubreportUpdateFailedError,
)
from superset.daos.database import DatabaseDAO
from superset.daos.report import ReportScheduleDAO, SubreportDAO
from superset.exceptions import SupersetParseError, SupersetSecurityException
from superset.models.core import Database
from superset.reports.models import (
    ReportSchedule,
    ReportScheduleType,
    Subreport,
    SubreportVizType,
)
from superset.reports.subreports import (
    ensure_subreports_enabled,
    get_available_context_field_names,
    get_sql_parameters,
    prepare_subreport_sql,
    resolve_context,
    run_subreport_query,
    SubreportAccessDeniedError,
    SubreportContext,
    SubreportError,
    SubreportInvalidSQLError,
    SubreportParameterError,
    SubreportQueryResult,
    validate_param_mapping,
    validate_parent_schedule,
    validate_subreport_definition,
)
from superset.utils.decorators import on_error, transaction

logger = logging.getLogger(__name__)

# Attributes a client may set on a subreport. ``parent_schedule_id`` is always
# taken from the URL and can never be set or reassigned from the payload.
SUBREPORT_ATTRIBUTES = (
    "name",
    "sql_query",
    "database_id",
    "param_mapping",
    "position",
    "viz_type",
    "template",
)
DEFINITION_ATTRIBUTES = frozenset(
    {"sql_query", "database_id", "param_mapping", "viz_type", "template"}
)


def get_parent_schedule(parent_schedule_id: int, *, for_update: bool) -> ReportSchedule:
    """
    Resolve a parent through the ``ReportScheduleDAO`` base filter, so a
    caller can never reach another user's children by id.

    :param for_update: Also require the current user to be an editor of the
        parent, as for any other change to the report schedule
    :raises ReportScheduleNotFoundError: If the parent is not visible
    :raises ReportScheduleForbiddenError: If the parent cannot be edited
    """
    ensure_subreports_enabled()
    parent = ReportScheduleDAO.find_by_id(parent_schedule_id)
    if parent is None:
        raise ReportScheduleNotFoundError()
    if for_update:
        try:
            security_manager.raise_for_editorship(parent)
        except SupersetSecurityException as ex:
            raise ReportScheduleForbiddenError() from ex
    return parent


def ensure_report_parent(parent: ReportSchedule) -> None:
    """
    Subreports belong to reports only; alerts never carry them.

    :raises SubreportInvalidError: If ``parent`` is not a report
    """
    if parent.type != ReportScheduleType.REPORT:
        raise SubreportInvalidError(
            exceptions=[
                ValidationError(
                    _("Only reports can have subreports."),
                    field_name="parent_schedule_id",
                )
            ]
        )


def get_subreport_database(database_id: int) -> Database:
    """
    Resolve a database through the ``DatabaseDAO`` base filter.

    :raises SubreportInvalidError: If the database does not exist
    :raises SubreportAccessDeniedError: If the user cannot access it
    """
    if database := DatabaseDAO.find_by_id(database_id):
        return database
    if db.session.get(Database, database_id) is None:
        raise SubreportInvalidError(
            exceptions=[
                ValidationError(_("Database does not exist"), field_name="database_id")
            ]
        )
    raise SubreportAccessDeniedError(_("You don't have access to this database."))


def _field_for(ex: SubreportError) -> str:
    if isinstance(ex, SubreportParameterError):
        return "param_mapping"
    if isinstance(ex, SubreportInvalidSQLError):
        return "sql_query"
    return "template"


def validate_subreport_access(
    database: Database, sql_query: str, param_mapping: Any
) -> None:
    """
    Run the checks shared with preview and execution (parsing, data access,
    denylists, RLS, row limit, SQL mutator) as the saving user, with every
    parameter bound to ``NULL`` so no parent values are needed.

    :raises SubreportError: If the SQL is invalid or not accessible
    """
    parameters = get_sql_parameters(sql_query, database.db_engine_spec.engine)
    mapping = validate_param_mapping(param_mapping, parameters)
    context = SubreportContext(values={field: [None] for field in mapping.values()})
    try:
        prepare_subreport_sql(database, sql_query, param_mapping, context)
    except SupersetParseError as ex:
        raise SubreportInvalidSQLError(
            _("Subreport SQL could not be parsed."), exception=ex
        ) from ex


def validate_subreport(
    parent: ReportSchedule,
    definition: Mapping[str, Any],
    available_fields: Iterable[str] | None = None,
) -> Database:
    """
    Validate a full subreport definition for ``parent`` and return its
    database.

    :raises SubreportInvalidError: If the definition is invalid
    :raises SubreportAccessDeniedError: If the user cannot access its data
    """
    ensure_report_parent(parent)
    database = get_subreport_database(definition["database_id"])
    fields_ = (
        set(available_fields)
        if available_fields is not None
        else get_available_context_field_names(parent)
    )
    try:
        validate_subreport_definition(
            sql_query=definition["sql_query"],
            database=database,
            param_mapping=definition.get("param_mapping") or {},
            viz_type=definition.get("viz_type") or SubreportVizType.TABLE.value,
            template=definition.get("template") or {},
            available_fields=fields_,
        )
        validate_subreport_access(
            database, definition["sql_query"], definition.get("param_mapping") or {}
        )
    except SubreportAccessDeniedError:
        raise
    except SubreportError as ex:
        raise SubreportInvalidError(
            exceptions=[ValidationError(ex.message, field_name=_field_for(ex))]
        ) from ex
    return database


def _attributes(data: Mapping[str, Any]) -> dict[str, Any]:
    attributes = {key: data[key] for key in SUBREPORT_ATTRIBUTES if key in data}
    if "template" in attributes and attributes["template"] is None:
        attributes["template"] = {}
    if "param_mapping" in attributes and attributes["param_mapping"] is None:
        attributes["param_mapping"] = {}
    return attributes


def build_subreports(
    parent: ReportSchedule, items: Iterable[Mapping[str, Any]]
) -> list[Subreport]:
    """
    Validate and build the subreports nested in a report schedule payload.

    Validation errors are reported per item under ``subreports``.

    :raises SubreportInvalidError: If any definition is invalid
    :raises SubreportAccessDeniedError: If the user cannot access any query
    """
    available_fields = get_available_context_field_names(parent)
    subreports: list[Subreport] = []
    errors: dict[int, Any] = {}
    for index, item in enumerate(items):
        try:
            validate_subreport(parent, item, available_fields)
        except SubreportInvalidError as ex:
            errors[index] = ex.normalized_messages()
            continue
        subreports.append(Subreport(**_attributes(item)))
    if errors:
        raise SubreportInvalidError(
            exceptions=[ValidationError({"subreports": errors})]
        )
    return subreports


class CreateSubreportCommand(BaseCommand):
    def __init__(self, parent_schedule_id: int, data: dict[str, Any]):
        self._parent_schedule_id = parent_schedule_id
        self._properties = data.copy()
        self._parent: ReportSchedule | None = None

    @transaction(on_error=partial(on_error, reraise=SubreportCreateFailedError))
    def run(self) -> Subreport:
        self.validate()
        assert self._parent is not None  # noqa: S101
        return SubreportDAO.create(
            attributes={
                **_attributes(self._properties),
                "parent_schedule_id": self._parent.id,
            }
        )

    def validate(self) -> None:
        self._parent = get_parent_schedule(self._parent_schedule_id, for_update=True)
        validate_subreport(self._parent, self._properties)


class UpdateSubreportCommand(BaseCommand):
    def __init__(
        self, parent_schedule_id: int, subreport_id: int, data: dict[str, Any]
    ):
        self._parent_schedule_id = parent_schedule_id
        self._subreport_id = subreport_id
        self._properties = data.copy()
        self._model: Subreport | None = None

    @transaction(on_error=partial(on_error, reraise=SubreportUpdateFailedError))
    def run(self) -> Subreport:
        self.validate()
        assert self._model is not None  # noqa: S101
        return SubreportDAO.update(self._model, _attributes(self._properties))

    def validate(self) -> None:
        parent = get_parent_schedule(self._parent_schedule_id, for_update=True)
        self._model = SubreportDAO.find_by_parent(parent.id, self._subreport_id)
        if self._model is None:
            raise SubreportNotFoundError()
        if DEFINITION_ATTRIBUTES & set(self._properties):
            merged = {
                "database_id": self._model.database_id,
                "sql_query": self._model.sql_query,
                "param_mapping": self._model.param_mapping,
                "viz_type": self._model.viz_type,
                "template": self._model.template,
                **_attributes(self._properties),
            }
            validate_subreport(parent, merged)


class DeleteSubreportCommand(BaseCommand):
    def __init__(self, parent_schedule_id: int, subreport_id: int):
        self._parent_schedule_id = parent_schedule_id
        self._subreport_id = subreport_id
        self._model: Subreport | None = None

    @transaction(on_error=partial(on_error, reraise=SubreportDeleteFailedError))
    def run(self) -> None:
        self.validate()
        assert self._model is not None  # noqa: S101
        SubreportDAO.delete([self._model])

    def validate(self) -> None:
        parent = get_parent_schedule(self._parent_schedule_id, for_update=True)
        self._model = SubreportDAO.find_by_parent(parent.id, self._subreport_id)
        if self._model is None:
            raise SubreportNotFoundError()


class PreviewSubreportCommand(BaseCommand):
    """
    Run an unsaved subreport definition through the same secure path used by
    notifications, as the requesting user.

    Parent values come from the parent's dashboard native filters, overridden
    by the explicit ``values`` in the payload. Chart parents have no rendered
    chart data during preview, so their fields must be supplied in ``values``.
    """

    def __init__(self, parent_schedule_id: int, data: dict[str, Any]):
        self._parent_schedule_id = parent_schedule_id
        self._properties = data.copy()
        self._parent: ReportSchedule | None = None
        self._database: Database | None = None

    def run(self) -> SubreportQueryResult:
        self.validate()
        assert self._parent is not None  # noqa: S101
        assert self._database is not None  # noqa: S101
        context = resolve_context(
            self._parent, explicit_values=self._properties.get("values") or {}
        )
        return run_subreport_query(
            self._database,
            self._properties["sql_query"],
            self._properties.get("param_mapping") or {},
            context,
            row_limit=self._properties.get("row_limit"),
        )

    def validate(self) -> None:
        self._parent = get_parent_schedule(self._parent_schedule_id, for_update=True)
        ensure_report_parent(self._parent)
        self._database = get_subreport_database(self._properties["database_id"])
        parameters = get_sql_parameters(
            self._properties["sql_query"], self._database.db_engine_spec.engine
        )
        validate_param_mapping(
            self._properties.get("param_mapping") or {},
            parameters,
            get_available_context_field_names(self._parent),
        )


def validate_schedule_composition(
    model: ReportSchedule | None,
    properties: Mapping[str, Any],
    exceptions: list[ValidationError],
) -> None:
    """
    Validate ``parent_schedule_id`` and nested ``subreports`` in a report
    schedule create (``model`` is ``None``) or update payload.

    A new or changed parent must be a report the user can see and edit, and
    must not form a cycle or exceed the maximum nesting depth. Schedules
    without these keys are not affected.

    :raises ReportScheduleForbiddenError: If the new parent cannot be edited
    """
    report_type = properties.get("type", model.type if model else None)
    current_parent_id = model.parent_schedule_id if model else None
    parent_id = properties.get("parent_schedule_id", current_parent_id)

    if parent_id is not None and (
        parent_id != current_parent_id or report_type != (model and model.type)
    ):
        try:
            parent = validate_parent_schedule(
                model.id if model else None, parent_id, schedule_type=report_type
            )
        except SubreportError as ex:
            exceptions.append(
                ValidationError(ex.message, field_name="parent_schedule_id")
            )
        else:
            if parent is not None and parent_id != current_parent_id:
                try:
                    security_manager.raise_for_editorship(parent)
                except SupersetSecurityException as ex:
                    raise ReportScheduleForbiddenError() from ex

    if (
        model is not None
        and report_type != model.type
        and report_type != ReportScheduleType.REPORT
        and (model.children or model.subreports)
    ):
        exceptions.append(
            ValidationError(
                _("Only reports can have child schedules or subreports."),
                field_name="type",
            )
        )

    if properties.get("subreports") and report_type != ReportScheduleType.REPORT:
        exceptions.append(
            ValidationError(
                _("Only reports can have subreports."), field_name="subreports"
            )
        )


def apply_nested_subreports(
    model: ReportSchedule, items: Iterable[Mapping[str, Any]]
) -> None:
    """
    Replace the subreports of a saved (flushed) schedule with ``items``,
    validated against the schedule's own context.

    :raises SubreportInvalidError: If any definition is invalid
    :raises SubreportAccessDeniedError: If the user cannot access any query
    """
    ensure_subreports_enabled()
    db.session.flush()
    model.subreports = build_subreports(model, items)
