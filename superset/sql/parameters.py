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
"""Read-only statement validation and dialect-aware binding of value parameters."""

from __future__ import annotations

import datetime
import math
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd
from flask_babel import gettext as _
from sqlglot import exp

from superset.sql.parse import SQLStatement


class SQLQueryValidationError(ValueError):
    """A statement is not a read-only query or contains invalid placeholders."""


class SQLParameterError(ValueError):
    """A value cannot be safely bound to a query parameter."""


def _check_placeholder(placeholder: exp.Placeholder) -> str:
    name = placeholder.name
    if not name or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise SQLQueryValidationError(
            _("Query parameters must be named, e.g. :customer_id.")
        )
    if placeholder.find_ancestor(exp.Table, exp.TableAlias, exp.Limit, exp.Offset):
        raise SQLQueryValidationError(
            _(
                "Parameter :%(name)s can only be used as a value, not as an "
                "identifier, table or limit.",
                name=name,
            )
        )
    if isinstance(placeholder.parent, (exp.Column, exp.Dot, exp.Identifier)):
        raise SQLQueryValidationError(
            _("Parameter :%(name)s can only be used as a value.", name=name)
        )
    return name


def validate_readonly_statement(
    statement: SQLStatement, *, allow_placeholders: bool = True
) -> None:
    """Require a SELECT/set operation with no mutations, INTO or locks."""
    ast = statement._parsed  # pylint: disable=protected-access  # noqa: SLF001
    if not isinstance(ast, (exp.Select, exp.SetOperation)):
        raise SQLQueryValidationError(_("SQL must be a SELECT query."))
    if statement.is_mutating() or ast.find(exp.Into, exp.Command, exp.Lock):
        raise SQLQueryValidationError(_("SQL must be read-only."))
    for placeholder in ast.find_all(exp.Placeholder):
        if not allow_placeholders:
            raise SQLQueryValidationError(_("SQL contains an unbound parameter."))
        _check_placeholder(placeholder)


def get_statement_parameters(statement: SQLStatement) -> list[str]:
    """Return distinct named value parameters in traversal order."""
    ast = statement._parsed  # pylint: disable=protected-access  # noqa: SLF001
    return list(
        dict.fromkeys(_check_placeholder(p) for p in ast.find_all(exp.Placeholder))
    )


def _to_literal(value: Any, max_string_length: int) -> exp.Expression:  # noqa: C901
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
            raise SQLParameterError(_("Parameter values must be finite."))
        return exp.Literal.number(repr(value))
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise SQLParameterError(_("Parameter values must be finite."))
        return exp.Literal.number(str(value))
    if isinstance(value, (pd.Timestamp, datetime.datetime, datetime.date)):
        return exp.Literal.string(value.isoformat())
    if isinstance(value, str):
        if len(value) > max_string_length:
            raise SQLParameterError(_("Parameter value is too long."))
        return exp.Literal.string(value)
    raise SQLParameterError(
        _("Unsupported parameter value type: %(type)s", type=type(value).__name__)
    )


def bind_statement_parameters(
    statement: SQLStatement,
    values: Mapping[str, Sequence[Any]],
    *,
    max_values: int = 1000,
    max_string_length: int = 10_000,
) -> SQLStatement:
    """Bind typed literals; lists expand only inside IN, never as identifiers."""
    bound = statement._parsed.copy()  # pylint: disable=protected-access  # noqa: SLF001
    for placeholder in list(bound.find_all(exp.Placeholder)):
        name = _check_placeholder(placeholder)
        if name not in values:
            raise SQLParameterError(_("No value for parameter :%(name)s.", name=name))
        parameter_values = values[name]
        parent = placeholder.parent
        if (
            isinstance(parent, exp.In)
            and placeholder.arg_key == "expressions"
            and not parent.args.get("query")
        ):
            if not parameter_values or len(parameter_values) > max_values:
                raise SQLParameterError(
                    _(
                        "Parameter :%(name)s requires between 1 and %(max)s values.",
                        name=name,
                        max=max_values,
                    )
                )
            expressions: list[exp.Expression] = []
            for item in parent.expressions:
                if item is placeholder:
                    expressions.extend(
                        _to_literal(value, max_string_length)
                        for value in parameter_values
                    )
                else:
                    expressions.append(item)
            parent.set("expressions", expressions)
        else:
            if len(parameter_values) != 1:
                raise SQLParameterError(
                    _(
                        "Parameter :%(name)s accepts exactly one value. "
                        "Use IN (:%(name)s) to match multiple values.",
                        name=name,
                    )
                )
            placeholder.replace(_to_literal(parameter_values[0], max_string_length))
    return SQLStatement(ast=bound, engine=statement.engine)
