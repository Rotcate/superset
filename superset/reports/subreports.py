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
"""
Shared context, validation and execution helpers for report subreports.

A subreport is a single read-only ``SELECT`` whose named value placeholders
(``:customer_id``) are bound from the parent report's context through a
``param_mapping`` such as ``{"customer_id": "$F{customer_id}"}``.

Every caller (preview API, notification rendering) must go through
:func:`run_subreport_query` so that validation, authorization, RLS, denylists
and resource bounds are identical on every path. Values are always bound as
typed literals in the parsed AST; they are never interpolated as text and the
SQL is never rendered through Jinja.
"""

from __future__ import annotations

import datetime
import logging
import math
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, TYPE_CHECKING, TypedDict

import numpy as np
import pandas as pd
from flask import current_app as app
from flask_babel import gettext as _
from sqlglot import exp

from superset import db, is_feature_enabled, security_manager
from superset.exceptions import (
    OAuth2Error,
    OAuth2RedirectError,
    SupersetException,
    SupersetParseError,
    SupersetSecurityException,
    SupersetTimeoutException,
)
from superset.reports.models import (
    ReportSchedule,
    ReportScheduleType,
    Subreport,
    SubreportVizType,
)
from superset.sql.parse import LimitMethod, SQLScript, SQLStatement, Table
from superset.utils import json
from superset.utils.core import get_column_name, get_metric_name, timeout
from superset.utils.csv import df_to_escaped_csv

if TYPE_CHECKING:
    from superset.models.core import Database
    from superset.models.slice import Slice

logger = logging.getLogger(__name__)

FEATURE_FLAG = "ALERT_REPORTS"
DEFAULT_ROW_LIMIT = 1000
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_MAX_PARAMETER_VALUES = 1000
DEFAULT_MAX_SCHEDULE_DEPTH = 5
MAX_PARAMETER_STRING_LENGTH = 10_000

PLACEHOLDER_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
FIELD_REFERENCE_RE = re.compile(r"^\$F\{([^{}]+)\}$")
# Subreport SQL is never rendered through Jinja; reject template syntax so it
# cannot be mistaken for supported templating and is never fed to a renderer.
JINJA_SYNTAX_RE = re.compile(r"{{|{%|{#")

ContextSource = Literal["native_filter", "chart_data"]


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class SubreportError(SupersetException):
    status = 422
    message = _("Subreport is invalid.")


class SubreportsDisabledError(SubreportError):
    status = 404
    message = _("Subreports are not enabled.")


class SubreportInvalidSQLError(SubreportError):
    pass


class SubreportParameterError(SubreportError):
    pass


class SubreportAccessDeniedError(SubreportError):
    status = 403
    message = _("You don't have access to run this subreport.")


class SubreportTimeoutError(SubreportError):
    status = 408
    message = _("The subreport query exceeded its timeout.")


class SubreportExecutionError(SubreportError):
    status = 500
    message = _("An error occurred while running the subreport query.")


class SubreportScheduleError(SubreportError):
    pass


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def ensure_subreports_enabled() -> None:
    """Subreports ride on the existing ``ALERT_REPORTS`` feature flag."""
    if not is_feature_enabled(FEATURE_FLAG):
        raise SubreportsDisabledError()


def get_row_limit(requested: int | None = None) -> int:
    """
    Effective row cap: ``ALERT_REPORTS_SUBREPORT_ROW_LIMIT`` (default 1000),
    optionally lowered by ``requested``, never above ``SQL_MAX_ROW``.
    """
    configured = int(
        app.config.get("ALERT_REPORTS_SUBREPORT_ROW_LIMIT", DEFAULT_ROW_LIMIT)
    )
    limit = configured if requested is None else min(int(requested), configured)
    if sql_max_row := app.config.get("SQL_MAX_ROW"):
        limit = min(limit, int(sql_max_row))
    return max(1, limit)


def get_timeout_seconds(requested: int | None = None) -> int:
    configured = int(
        app.config.get(
            "ALERT_REPORTS_SUBREPORT_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS
        )
    )
    value = configured if requested is None else min(int(requested), configured)
    return max(1, value)


def _max_parameter_values() -> int:
    return int(
        app.config.get(
            "ALERT_REPORTS_SUBREPORT_MAX_PARAMETER_VALUES",
            DEFAULT_MAX_PARAMETER_VALUES,
        )
    )


# --------------------------------------------------------------------------- #
# Static SQL validation
# --------------------------------------------------------------------------- #
def validate_subreport_sql(  # noqa: C901
    sql: str,
    engine: str,
    *,
    allow_placeholders: bool = True,
) -> SQLStatement:
    """
    Parse ``sql`` with the engine dialect and fail closed unless it is exactly
    one fully parsed, read-only query (``SELECT``, CTE or set operation).

    :param sql: Subreport SQL
    :param engine: ``db_engine_spec.engine`` of the target database
    :param allow_placeholders: ``False`` for rendered SQL, which must not
        contain unbound placeholders
    :returns: The parsed statement
    :raises SubreportInvalidSQLError: If the SQL is not acceptable
    """
    if not sql or not sql.strip():
        raise SubreportInvalidSQLError(_("Subreport SQL is required."))
    # Only authored SQL is checked: rendered SQL may legitimately carry bound
    # string literals containing braces, and is never passed to Jinja.
    if allow_placeholders and JINJA_SYNTAX_RE.search(sql):
        raise SubreportInvalidSQLError(
            _(
                "Jinja templating is not supported in subreport SQL; "
                "use named parameters such as :customer_id."
            )
        )
    try:
        script = SQLScript(sql, engine)
    except SupersetParseError as ex:
        raise SubreportInvalidSQLError(
            _("Subreport SQL could not be parsed."), exception=ex
        ) from ex

    if len(script.statements) != 1:
        raise SubreportInvalidSQLError(
            _("Subreport SQL must contain exactly one statement.")
        )
    if script.has_unparseable_statement:
        raise SubreportInvalidSQLError(_("Subreport SQL could not be fully parsed."))

    statement = script.statements[0]
    if not isinstance(statement, SQLStatement):
        raise SubreportInvalidSQLError(
            _("Subreports are not supported for this database engine.")
        )
    ast = statement._parsed  # pylint: disable=protected-access  # noqa: SLF001
    if not isinstance(ast, (exp.Select, exp.SetOperation)):
        raise SubreportInvalidSQLError(_("Subreport SQL must be a SELECT query."))
    if (
        statement.is_mutating()
        or script.has_mutation()
        or script.changes_default_schema()
        or ast.find(exp.Into, exp.Command, exp.Lock) is not None
    ):
        raise SubreportInvalidSQLError(_("Subreport SQL must be read-only."))

    for placeholder in ast.find_all(exp.Placeholder):
        if not allow_placeholders:
            raise SubreportInvalidSQLError(
                _("Subreport SQL contains an unbound parameter.")
            )
        _check_placeholder(placeholder)
    return statement


def _check_placeholder(placeholder: exp.Placeholder) -> str:
    name = placeholder.name
    if not name or name == "?" or not PLACEHOLDER_NAME_RE.match(name):
        raise SubreportInvalidSQLError(
            _("Subreport parameters must be named, e.g. :customer_id.")
        )
    if placeholder.find_ancestor(exp.Table, exp.TableAlias, exp.Limit, exp.Offset):
        raise SubreportInvalidSQLError(
            _(
                "Parameter :%(name)s can only be used as a value, not as an "
                "identifier, table or limit.",
                name=name,
            )
        )
    if isinstance(placeholder.parent, (exp.Column, exp.Dot, exp.Identifier)):
        raise SubreportInvalidSQLError(
            _("Parameter :%(name)s can only be used as a value.", name=name)
        )
    return name


def get_sql_parameters(sql: str, engine: str) -> list[str]:
    """Return the distinct named placeholders used by ``sql``, in order."""
    statement = validate_subreport_sql(sql, engine)
    ast = statement._parsed  # pylint: disable=protected-access  # noqa: SLF001
    names: list[str] = []
    for placeholder in ast.find_all(exp.Placeholder):
        if placeholder.name not in names:
            names.append(placeholder.name)
    return names


def parse_field_reference(value: Any) -> str:
    """``"$F{customer_id}"`` -> ``"customer_id"``."""
    if not isinstance(value, str) or not (match := FIELD_REFERENCE_RE.match(value)):
        raise SubreportParameterError(
            _("Parameter mappings must reference a parent field as $F{field_name}.")
        )
    return match.group(1).strip()


def validate_param_mapping(
    param_mapping: Any,
    parameters: Collection[str],
    available_fields: Collection[str] | None = None,
) -> dict[str, str]:
    """
    Validate ``{"<placeholder>": "$F{<field>}"}`` against the SQL parameters
    and, when known, the parent's available context fields.

    :returns: ``{placeholder: field}``
    """
    if param_mapping is None:
        param_mapping = {}
    if not isinstance(param_mapping, Mapping):
        raise SubreportParameterError(_("Parameter mapping must be an object."))

    resolved: dict[str, str] = {}
    for name, reference in param_mapping.items():
        if not isinstance(name, str) or not PLACEHOLDER_NAME_RE.match(name):
            raise SubreportParameterError(
                _("Invalid parameter name: %(name)s", name=str(name))
            )
        if name not in parameters:
            raise SubreportParameterError(
                _("Parameter :%(name)s is not used in the SQL.", name=name)
            )
        field_name = parse_field_reference(reference)
        if available_fields is not None and field_name not in available_fields:
            raise SubreportParameterError(
                _(
                    "Field %(field)s is not available from the parent report.",
                    field=field_name,
                )
            )
        resolved[name] = field_name

    if missing := [name for name in parameters if name not in resolved]:
        raise SubreportParameterError(
            _(
                "Missing mapping for parameters: %(names)s",
                names=", ".join(f":{name}" for name in missing),
            )
        )
    return resolved


def validate_subreport_definition(
    *,
    sql_query: str,
    database: Database,
    param_mapping: Any,
    viz_type: str,
    template: Mapping[str, Any] | None,
    available_fields: Collection[str] | None = None,
) -> None:
    """Validate a full (possibly merged) subreport definition without running it."""
    engine = database.db_engine_spec.engine
    parameters = get_sql_parameters(sql_query, engine)
    validate_param_mapping(param_mapping, parameters, available_fields)
    validate_template(viz_type, template)


# --------------------------------------------------------------------------- #
# Template / rendering options
# --------------------------------------------------------------------------- #
class SubreportTemplate(TypedDict, total=False):
    """
    Rendering options stored in ``Subreport.template``.

    Table (``viz_type="table"``):
        ``title``, ``columns`` (ordered subset to show), ``max_rows``
    Chart (``viz_type="chart"``):
        ``title``, ``chart_type`` (``bar``/``line``), ``x_column``,
        ``y_columns`` (one or more numeric columns), ``max_rows``
    """

    title: str
    columns: list[str]
    max_rows: int
    chart_type: Literal["bar", "line"]
    x_column: str
    y_columns: list[str]


CHART_TYPES = ("bar", "line")
TABLE_TEMPLATE_KEYS = {"title", "columns", "max_rows"}
CHART_TEMPLATE_KEYS = {"title", "chart_type", "x_column", "y_columns", "max_rows"}


def validate_template(  # noqa: C901
    viz_type: str, template: Mapping[str, Any] | None
) -> SubreportTemplate:
    """Validate and normalize rendering options for ``viz_type``."""
    if viz_type not in {item.value for item in SubreportVizType}:
        raise SubreportError(_("viz_type must be one of: table, chart."))
    template = dict(template or {})
    allowed = (
        TABLE_TEMPLATE_KEYS
        if viz_type == SubreportVizType.TABLE
        else CHART_TEMPLATE_KEYS
    )
    if unknown := sorted(set(template) - allowed):
        raise SubreportError(
            _("Unknown template options: %(keys)s", keys=", ".join(unknown))
        )
    normalized: SubreportTemplate = {}
    if "title" in template:
        if not isinstance(template["title"], str) or len(template["title"]) > 250:
            raise SubreportError(_("template.title must be a string."))
        normalized["title"] = template["title"]
    if "max_rows" in template:
        max_rows = template["max_rows"]
        if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 1:
            raise SubreportError(_("template.max_rows must be a positive integer."))
        normalized["max_rows"] = max_rows
    if viz_type == SubreportVizType.TABLE:
        if "columns" in template:
            normalized["columns"] = _string_list(template["columns"], "columns")
        return normalized

    chart_type = template.get("chart_type", "bar")
    if chart_type not in CHART_TYPES:
        raise SubreportError(_("template.chart_type must be bar or line."))
    x_column = template.get("x_column")
    if not isinstance(x_column, str) or not x_column:
        raise SubreportError(_("template.x_column is required for charts."))
    y_columns = _string_list(template.get("y_columns"), "y_columns")
    if not y_columns:
        raise SubreportError(_("template.y_columns is required for charts."))
    normalized["chart_type"] = chart_type
    normalized["x_column"] = x_column
    normalized["y_columns"] = y_columns
    return normalized


def _string_list(value: Any, key: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise SubreportError(
            _("template.%(key)s must be a list of column names.", key=key)
        )
    return list(value)


# --------------------------------------------------------------------------- #
# Parent context discovery and resolution
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SubreportContextField:
    """A parent field that subreport parameters may reference as ``$F{name}``."""

    name: str
    source: ContextSource
    label: str | None = None
    filter_id: str | None = None
    filter_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": self.source,
            "label": self.label,
            "filter_id": self.filter_id,
            "filter_type": self.filter_type,
            "reference": f"$F{{{self.name}}}",
        }


@dataclass
class SubreportContext:
    """
    Resolved parent values: ``values[field]`` is the ordered list of distinct
    values. ``errors[field]`` records fields that cannot be resolved safely
    (e.g. conflicting filters); binding such a field raises.
    """

    values: dict[str, list[Any]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    def get(self, field_name: str) -> list[Any]:
        if field_name in self.errors:
            raise SubreportParameterError(self.errors[field_name])
        if field_name not in self.values or not self.values[field_name]:
            raise SubreportParameterError(
                _(
                    "No value is available for parent field %(field)s.",
                    field=field_name,
                )
            )
        return self.values[field_name]


def _native_filters(report_schedule: ReportSchedule) -> list[dict[str, Any]]:
    if not report_schedule.dashboard_id:
        return []
    extra: dict[str, Any] = dict(report_schedule.extra or {})
    dashboard: dict[str, Any] = extra.get("dashboard") or {}
    filters = dashboard.get("nativeFilters") or []
    if not isinstance(filters, list):
        return []
    return [item for item in filters if isinstance(item, dict)]


def _chart_output_columns(chart: Slice) -> list[str]:  # noqa: C901
    """Best-effort list of columns the parent chart's data will contain."""
    names: list[str] = []

    def add(name: Any) -> None:
        if isinstance(name, str) and name and name not in names:
            names.append(name)

    queries: list[dict[str, Any]] = []
    if chart.query_context:
        try:
            queries = json.loads(chart.query_context).get("queries") or []
        except (TypeError, ValueError):
            logger.warning("Invalid query_context on chart %s", chart.id)
    if not queries:
        form_data = chart.form_data
        queries = [
            {
                "columns": [
                    *(form_data.get("groupby") or []),
                    *(form_data.get("columns") or []),
                    *(form_data.get("all_columns") or []),
                    *([form_data["x_axis"]] if form_data.get("x_axis") else []),
                ],
                "metrics": [
                    *(form_data.get("metrics") or []),
                    *([form_data["metric"]] if form_data.get("metric") else []),
                ],
            }
        ]
    for query in queries:
        for column in query.get("columns") or []:
            try:
                add(get_column_name(column))
            except ValueError:
                continue
        for metric in query.get("metrics") or []:
            try:
                add(get_metric_name(metric))
            except ValueError:
                continue
    return names


def get_available_context_fields(
    report_schedule: ReportSchedule,
) -> list[SubreportContextField]:
    """
    Fields a subreport of ``report_schedule`` may reference.

    Dashboard parents expose configured native filters (``columnName``);
    chart parents expose the chart's output columns and metric labels.
    """
    fields_: dict[str, SubreportContextField] = {}
    for native_filter in _native_filters(report_schedule):
        name = native_filter.get("columnName")
        if isinstance(name, str) and name and name not in fields_:
            fields_[name] = SubreportContextField(
                name=name,
                source="native_filter",
                label=native_filter.get("columnLabel") or native_filter.get("name"),
                filter_id=native_filter.get("nativeFilterId"),
                filter_type=native_filter.get("filterType"),
            )
    if report_schedule.chart is not None:
        for name in _chart_output_columns(report_schedule.chart):
            fields_.setdefault(
                name, SubreportContextField(name=name, source="chart_data")
            )
    return list(fields_.values())


def get_available_context_field_names(report_schedule: ReportSchedule) -> set[str]:
    return {item.name for item in get_available_context_fields(report_schedule)}


def _normalize_values(values: Any) -> list[Any]:
    items: Iterable[Any]
    if isinstance(values, (list, tuple, set, pd.Series, np.ndarray)):
        items = list(values)
    else:
        items = [values]
    distinct: list[Any] = []
    for item in items:
        if isinstance(item, np.generic):
            item = item.item()
        if item is None or (isinstance(item, float) and math.isnan(item)):
            continue
        if item is pd.NaT:
            continue
        if item not in distinct:
            distinct.append(item)
    return distinct


def resolve_context(
    report_schedule: ReportSchedule | None = None,
    *,
    chart_data: pd.DataFrame | None = None,
    explicit_values: Mapping[str, Any] | None = None,
) -> SubreportContext:
    """
    Resolve parent values for parameter binding.

    - Dashboard parents: native filter ``filterValues`` keyed by ``columnName``.
      Two filters on the same column with different values are recorded as an
      error rather than picking one.
    - Chart parents: distinct non-null values of each ``chart_data`` column
      (the rendered chart DataFrame supplied by the report command).
    - Preview: ``explicit_values`` (scalar or list per field) override both.
    """
    context = SubreportContext()
    if report_schedule is not None:
        for native_filter in _native_filters(report_schedule):
            name = native_filter.get("columnName")
            if not isinstance(name, str) or not name:
                continue
            values = _normalize_values(native_filter.get("filterValues") or [])
            if name in context.values and context.values[name] != values:
                context.errors[name] = _(
                    "Multiple dashboard filters on %(field)s have different "
                    "values; the subreport parameter is ambiguous.",
                    field=name,
                )
                continue
            context.values[name] = values
    if chart_data is not None:
        for column in chart_data.columns:
            name = str(column)
            if name in context.values:
                continue
            context.values[name] = _normalize_values(
                pd.unique(chart_data[column].dropna())
            )
    for name, values in (explicit_values or {}).items():
        context.values[str(name)] = _normalize_values(values)
        context.errors.pop(str(name), None)
    return context


# --------------------------------------------------------------------------- #
# Parameter binding
# --------------------------------------------------------------------------- #
def _to_literal(value: Any) -> exp.Expression:  # noqa: C901
    if isinstance(value, np.generic):
        value = value.item()
    if value is None:
        return exp.Null()
    if isinstance(value, bool):
        return exp.Boolean(this=value)
    if isinstance(value, int):
        return exp.Literal.number(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SubreportParameterError(_("Parameter values must be finite."))
        return exp.Literal.number(repr(value))
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise SubreportParameterError(_("Parameter values must be finite."))
        return exp.Literal.number(str(value))
    if isinstance(value, (pd.Timestamp, datetime.datetime, datetime.date)):
        return exp.Literal.string(value.isoformat())
    if isinstance(value, str):
        if len(value) > MAX_PARAMETER_STRING_LENGTH:
            raise SubreportParameterError(_("Parameter value is too long."))
        return exp.Literal.string(value)
    raise SubreportParameterError(
        _("Unsupported parameter value type: %(type)s", type=type(value).__name__)
    )


def _bind_ast(
    ast: exp.Expression,
    mapping: Mapping[str, str],
    context: SubreportContext,
    *,
    null_values: bool = False,
) -> exp.Expression:
    """Return a copy of ``ast`` with every placeholder replaced by literals."""
    bound = ast.copy()
    max_values = _max_parameter_values()
    for placeholder in list(bound.find_all(exp.Placeholder)):
        name = _check_placeholder(placeholder)
        field_name = mapping[name]
        values = [None] if null_values else context.get(field_name)
        parent = placeholder.parent
        in_list = (
            isinstance(parent, exp.In)
            and placeholder.arg_key == "expressions"
            and not parent.args.get("query")
        )
        if in_list:
            if len(values) > max_values:
                raise SubreportParameterError(
                    _(
                        "Parent field %(field)s has more than %(max)s values.",
                        field=field_name,
                        max=max_values,
                    )
                )
            assert isinstance(parent, exp.In)  # noqa: S101
            expressions: list[exp.Expression] = []
            for item in parent.expressions:
                if item is placeholder:
                    expressions.extend(_to_literal(value) for value in values)
                else:
                    expressions.append(item)
            parent.set("expressions", expressions)
            continue
        if len(values) != 1:
            raise SubreportParameterError(
                _(
                    "Parent field %(field)s has %(count)s values but parameter "
                    ":%(name)s accepts exactly one. Use it inside IN (:%(name)s) "
                    "to match multiple values.",
                    field=field_name,
                    count=len(values),
                    name=name,
                )
            )
        placeholder.replace(_to_literal(values[0]))
    return bound


def bind_parameters(
    sql: str,
    engine: str,
    param_mapping: Any,
    context: SubreportContext,
) -> str:
    """
    Bind parent values into ``sql`` and return rendered, revalidated SQL.

    A placeholder used as an ``IN (...)`` member expands to all of the field's
    values; anywhere else it requires exactly one value, otherwise a clear
    :class:`SubreportParameterError` is raised.
    """
    statement = validate_subreport_sql(sql, engine)
    ast = statement._parsed  # pylint: disable=protected-access  # noqa: SLF001
    parameters = [p.name for p in ast.find_all(exp.Placeholder)]
    mapping = validate_param_mapping(param_mapping, parameters)
    rendered = SQLStatement(ast=_bind_ast(ast, mapping, context), engine=engine)
    rendered_sql = rendered.format()
    validate_subreport_sql(rendered_sql, engine, allow_placeholders=False)
    return rendered_sql


# --------------------------------------------------------------------------- #
# Secure execution
# --------------------------------------------------------------------------- #
@dataclass
class SubreportQueryResult:
    df: pd.DataFrame
    truncated: bool
    row_limit: int
    executed_sql: str


def _check_denylists(database: Database, script: SQLScript, schema: str | None) -> None:
    engine = database.db_engine_spec.engine
    disallowed_functions = app.config.get("DISALLOWED_SQL_FUNCTIONS", {}).get(
        engine, set()
    )
    if found := {
        func for func in disallowed_functions if script.check_functions_present({func})
    }:
        raise SubreportAccessDeniedError(
            _(
                "Subreport SQL uses disallowed functions: %(functions)s",
                functions=", ".join(sorted(found)),
            )
        )
    disallowed_tables = app.config.get("DISALLOWED_SQL_TABLES", {}).get(engine, set())
    if disallowed_tables and (
        found := script.get_disallowed_tables(disallowed_tables, schema)
    ):
        raise SubreportAccessDeniedError(
            _(
                "Subreport SQL uses disallowed tables: %(tables)s",
                tables=", ".join(sorted(found)),
            )
        )


def _tables(statement: SQLStatement) -> set[Table]:
    return set(statement.tables)


def _authorize(
    database: Database,
    authz_sql: str,
    catalog: str | None,
    schema: str | None,
) -> None:
    try:
        security_manager.raise_for_access(
            database=database,
            sql=authz_sql,
            catalog=catalog,
            schema=schema,
            force_dataset_match=True,
        )
    except SupersetSecurityException as ex:
        raise SubreportAccessDeniedError(exception=ex) from ex


def prepare_subreport_sql(
    database: Database,
    sql: str,
    param_mapping: Any,
    context: SubreportContext,
    *,
    row_limit: int | None = None,
) -> tuple[str, str | None, str | None, int]:
    """
    Validate, bind, authorize, apply RLS and limit, and return
    ``(executable_sql, catalog, schema, row_limit)``.

    Authorization runs as the current user (the report executor during
    notifications, the requester during preview).
    """
    engine = database.db_engine_spec.engine
    statement = validate_subreport_sql(sql, engine)
    ast = statement._parsed  # pylint: disable=protected-access  # noqa: SLF001
    parameters = [p.name for p in ast.find_all(exp.Placeholder)]
    mapping = validate_param_mapping(param_mapping, parameters)

    bound_sql = SQLStatement(
        ast=_bind_ast(ast, mapping, context), engine=engine
    ).format()
    # Authorize a value-free skeleton: placeholders become NULL so that
    # parameter values never reach the Jinja-aware access check while the set
    # of referenced tables stays identical.
    authz_sql = SQLStatement(
        ast=_bind_ast(ast, mapping, context, null_values=True), engine=engine
    ).format()

    catalog = database.get_default_catalog()
    try:
        schema = database.resolve_query_default_schema(bound_sql, None, catalog)
    except SupersetSecurityException as ex:
        raise SubreportAccessDeniedError(exception=ex) from ex

    _authorize(database, authz_sql, catalog, schema)

    bound_statement = validate_subreport_sql(
        bound_sql, engine, allow_placeholders=False
    )
    bound_script = SQLScript(bound_sql, engine)
    _check_denylists(database, bound_script, schema)
    allowed_tables = _tables(bound_statement)

    # RLS is applied unconditionally: unlike SQL Lab, it is not gated by
    # ``RLS_IN_SQLLAB``.
    from superset.utils.rls import apply_rls

    for item in bound_script.statements:
        apply_rls(database, catalog, schema or "", item)

    effective_limit = get_row_limit(row_limit)
    limited = bound_script.statements[0]
    assert isinstance(limited, SQLStatement)  # noqa: S101
    method = database.db_engine_spec.limit_method
    if method == LimitMethod.FETCH_MANY:
        method = LimitMethod.WRAP_SQL
    current_limit = limited.get_limit_value()
    if current_limit is None or current_limit > effective_limit + 1:
        # Fetch one extra row so truncation can be reported.
        limited.set_limit_value(effective_limit + 1, method)
    executable_sql = limited.format()

    # ``get_df`` passes the SQL through ``SQL_QUERY_MUTATOR``; revalidate the
    # SQL it will actually execute.
    mutated_sql = database.mutate_sql_based_on_config(executable_sql, is_split=True)
    if mutated_sql != executable_sql:
        mutated = validate_subreport_sql(mutated_sql, engine, allow_placeholders=False)
        _check_denylists(database, SQLScript(mutated_sql, engine), schema)
        if not _tables(mutated) <= allowed_tables | _tables(
            SQLStatement(executable_sql, engine)
        ):
            raise SubreportAccessDeniedError(
                _("The SQL query mutator added tables to the subreport query.")
            )
    else:
        validate_subreport_sql(executable_sql, engine, allow_placeholders=False)
    return executable_sql, catalog, schema, effective_limit


def run_subreport_query(
    database: Database,
    sql: str,
    param_mapping: Any,
    context: SubreportContext,
    *,
    row_limit: int | None = None,
    timeout_seconds: int | None = None,
) -> SubreportQueryResult:
    """
    Single secure execution path shared by preview and notifications.

    :raises SubreportError: Subclasses describe validation, access, timeout
        and execution failures; database error details are logged, not
        returned.
    """
    ensure_subreports_enabled()
    executable_sql, catalog, schema, limit = prepare_subreport_sql(
        database, sql, param_mapping, context, row_limit=row_limit
    )
    seconds = get_timeout_seconds(timeout_seconds)
    try:
        with timeout(
            seconds=seconds,
            error_message=_(
                "The subreport query exceeded the %(seconds)s seconds timeout.",
                seconds=seconds,
            ),
        ):
            df = database.get_df(executable_sql, catalog=catalog, schema=schema)
    except SupersetTimeoutException as ex:
        raise SubreportTimeoutError(exception=ex) from ex
    except (OAuth2RedirectError, OAuth2Error):
        raise
    except SupersetSecurityException as ex:
        raise SubreportAccessDeniedError(exception=ex) from ex
    except Exception as ex:  # pylint: disable=broad-except
        logger.exception("Subreport query failed on database %s", database.id)
        raise SubreportExecutionError(exception=ex) from ex

    if df is None:
        df = pd.DataFrame()
    truncated = len(df.index) > limit
    if truncated:
        df = df.head(limit)
    return SubreportQueryResult(
        df=df, truncated=truncated, row_limit=limit, executed_sql=executable_sql
    )


def execute_subreport_query(
    database: Database,
    sql: str,
    param_mapping: Any,
    context: SubreportContext,
    *,
    row_limit: int | None = None,
    timeout_seconds: int | None = None,
) -> pd.DataFrame:
    """Like :func:`run_subreport_query`, returning only the DataFrame."""
    return run_subreport_query(
        database,
        sql,
        param_mapping,
        context,
        row_limit=row_limit,
        timeout_seconds=timeout_seconds,
    ).df


@dataclass
class SubreportResult:
    subreport_id: int
    name: str
    position: int
    viz_type: str
    template: SubreportTemplate
    df: pd.DataFrame
    truncated: bool

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.subreport_id,
            "name": self.name,
            "position": self.position,
            "viz_type": self.viz_type,
            "template": dict(self.template),
            **dataframe_to_payload(self.df, self.truncated),
        }


def execute_subreport(
    subreport: Subreport,
    context: SubreportContext,
    *,
    row_limit: int | None = None,
    timeout_seconds: int | None = None,
) -> SubreportResult:
    template = validate_template(subreport.viz_type, subreport.template)
    limit = row_limit
    if max_rows := template.get("max_rows"):
        limit = min(max_rows, limit) if limit else max_rows
    result = run_subreport_query(
        subreport.database,
        subreport.sql_query,
        subreport.param_mapping,
        context,
        row_limit=limit,
        timeout_seconds=timeout_seconds,
    )
    return SubreportResult(
        subreport_id=subreport.id,
        name=subreport.name,
        position=subreport.position,
        viz_type=subreport.viz_type,
        template=template,
        df=result.df,
        truncated=result.truncated,
    )


def execute_subreports(
    report_schedule: ReportSchedule,
    *,
    chart_data: pd.DataFrame | None = None,
    explicit_values: Mapping[str, Any] | None = None,
) -> list[SubreportResult]:
    """
    Run all subreports of ``report_schedule`` in order. Returns ``[]`` (and runs
    nothing) when the schedule has no subreports, preserving existing behavior.
    """
    subreports = sorted(
        report_schedule.subreports, key=lambda item: (item.position, item.id)
    )
    if not subreports:
        return []
    context = resolve_context(
        report_schedule, chart_data=chart_data, explicit_values=explicit_values
    )
    return [execute_subreport(item, context) for item in subreports]


# --------------------------------------------------------------------------- #
# Rendering helpers
# --------------------------------------------------------------------------- #
def dataframe_to_payload(df: pd.DataFrame, truncated: bool = False) -> dict[str, Any]:
    """
    JSON-ready data shape used by the preview API and notifications::

        {"columns": [{"name": "a", "type": "int64"}],
         "data": [{"a": 1}], "row_count": 1, "truncated": false}
    """
    clean = df.astype(object).where(pd.notna(df), None)
    return {
        "columns": [
            {"name": str(column), "type": str(dtype)}
            for column, dtype in df.dtypes.items()
        ],
        "data": clean.to_dict(orient="records"),
        "row_count": len(df.index),
        "truncated": truncated,
    }


def dataframe_to_csv(df: pd.DataFrame) -> bytes:
    """CSV export using ``CSV_EXPORT`` settings with formula-injection escaping."""
    config = dict(app.config.get("CSV_EXPORT") or {})
    encoding = config.pop("encoding", "utf-8")
    csv = df_to_escaped_csv(df, index=False, **config)
    return str(csv).encode(encoding)


# --------------------------------------------------------------------------- #
# Nested schedule composition
# --------------------------------------------------------------------------- #
def _parent_id_of(schedule_id: int) -> int | None:
    return (
        db.session.query(ReportSchedule.parent_schedule_id)
        .filter(ReportSchedule.id == schedule_id)
        .scalar()
    )


def _child_ids_of(schedule_ids: Collection[int]) -> list[int]:
    return [
        row[0]
        for row in db.session.query(ReportSchedule.id)
        .filter(ReportSchedule.parent_schedule_id.in_(list(schedule_ids)))
        .all()
    ]


def _subtree_height(schedule_id: int, max_depth: int) -> int:
    height, level, seen = 0, [schedule_id], {schedule_id}
    while level:
        children = [cid for cid in _child_ids_of(level) if cid not in seen]
        if not children:
            break
        seen.update(children)
        height += 1
        if height > max_depth:
            break
        level = children
    return height


def validate_parent_schedule(  # noqa: C901
    schedule_id: int | None,
    parent_schedule_id: int | None,
    *,
    schedule_type: str | None = None,
) -> ReportSchedule | None:
    """
    Validate a proposed ``parent_schedule_id`` for ``schedule_id`` (``None`` on
    create). The parent must be a report the current user can access (via
    ``ReportScheduleDAO`` base filter), and the result must be acyclic and at
    most ``ALERT_REPORTS_MAX_SCHEDULE_DEPTH`` levels deep.

    :returns: The parent schedule, or ``None`` when detaching
    """
    if parent_schedule_id is None:
        return None
    if schedule_type is not None and schedule_type != ReportScheduleType.REPORT:
        raise SubreportScheduleError(_("Only reports can have a parent schedule."))
    if schedule_id is not None and parent_schedule_id == schedule_id:
        raise SubreportScheduleError(_("A report cannot be its own parent."))

    from superset.daos.report import ReportScheduleDAO

    parent = ReportScheduleDAO.find_by_id(parent_schedule_id)
    if parent is None:
        raise SubreportScheduleError(_("Parent report schedule not found."))
    if parent.type != ReportScheduleType.REPORT:
        raise SubreportScheduleError(_("The parent schedule must be a report."))

    max_depth = int(
        app.config.get("ALERT_REPORTS_MAX_SCHEDULE_DEPTH", DEFAULT_MAX_SCHEDULE_DEPTH)
    )
    depth, seen, current = 1, {parent_schedule_id}, parent.parent_schedule_id
    while current is not None:
        if current == schedule_id or current in seen:
            raise SubreportScheduleError(_("Report schedule nesting forms a cycle."))
        seen.add(current)
        depth += 1
        if depth > max_depth:
            break
        current = _parent_id_of(current)
    if schedule_id is not None:
        depth += _subtree_height(schedule_id, max_depth)
    if depth >= max_depth + 1:
        raise SubreportScheduleError(
            _(
                "Report schedules can be nested at most %(max)s levels deep.",
                max=max_depth,
            )
        )
    return parent


def is_independently_scheduled(report_schedule: ReportSchedule) -> bool:
    """Child schedules are delivered with their parent, never on their own."""
    return report_schedule.parent_schedule_id is None


def get_composed_schedules(report_schedule: ReportSchedule) -> list[ReportSchedule]:
    """Descendant schedules in depth-first order (by id), with a cycle guard."""
    result: list[ReportSchedule] = []
    seen = {report_schedule.id}

    def walk(node: ReportSchedule) -> None:
        for child in sorted(node.children, key=lambda item: item.id):
            if child.id in seen:
                continue
            seen.add(child.id)
            result.append(child)
            walk(child)

    walk(report_schedule)
    return result


def get_subreport_for_parent(
    parent_schedule_id: int, subreport_id: int
) -> Subreport | None:
    """
    Fetch a subreport only through a parent the current user may access, so a
    caller can never read or modify another parent's children by id.
    """
    from superset.daos.report import ReportScheduleDAO

    if ReportScheduleDAO.find_by_id(parent_schedule_id) is None:
        return None
    return (
        db.session.query(Subreport)
        .filter(
            Subreport.id == subreport_id,
            Subreport.parent_schedule_id == parent_schedule_id,
        )
        .one_or_none()
    )


def sort_subreports(subreports: Sequence[Subreport]) -> list[Subreport]:
    return sorted(subreports, key=lambda item: (item.position, item.id or 0))
