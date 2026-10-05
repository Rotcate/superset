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

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd
import pytest

from superset.sql.parameters import (
    bind_statement_parameters,
    get_statement_parameters,
    SQLParameterError,
    SQLQueryValidationError,
    validate_readonly_statement,
)
from superset.sql.parse import SQLStatement


def test_bind_preserves_statement_and_parameter_order() -> None:
    statement = SQLStatement("SELECT :value WHERE :value IN (:items)", "postgresql")
    validate_readonly_statement(statement)
    assert get_statement_parameters(statement) == ["value", "items"]
    bound = bind_statement_parameters(statement, {"value": [1], "items": [1, 2]})
    assert " ".join(bound.format().split()) == "SELECT 1 WHERE 1 IN (1, 2)"
    assert get_statement_parameters(statement) == ["value", "items"]
    assert get_statement_parameters(bound) == []
    validate_readonly_statement(bound, allow_placeholders=False)


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM orders",
        "SELECT * INTO other FROM orders",
        "SELECT * FROM orders FOR UPDATE",
        "SELECT * FROM :table",
        "SELECT * FROM orders LIMIT :limit",
        "SELECT :value",
    ],
)
def test_rejects_unsafe_or_unbound_statements(sql: str) -> None:
    with pytest.raises(SQLQueryValidationError):
        validate_readonly_statement(
            SQLStatement(sql, "postgresql"), allow_placeholders=False
        )


@pytest.mark.parametrize("engine", ["postgresql", "mysql", "sqlite"])
def test_string_parameters_roundtrip_as_literals(engine: str) -> None:
    statement = SQLStatement("SELECT :value", engine)
    bound = bind_statement_parameters(statement, {"value": ["O'Reilly\\north"]})
    reparsed = SQLStatement(bound.format(), engine)
    assert reparsed.format() == bound.format()
    assert not reparsed.tables
    validate_readonly_statement(reparsed, allow_placeholders=False)


@pytest.mark.parametrize("values", [[], [1, 2]])
def test_scalar_parameters_require_one_value(values: list[int]) -> None:
    with pytest.raises(SQLParameterError, match="exactly one"):
        bind_statement_parameters(SQLStatement("SELECT :value"), {"value": values})


def test_binding_limits() -> None:
    statement = SQLStatement("SELECT 1 WHERE 1 IN (:items)")
    with pytest.raises(SQLParameterError):
        bind_statement_parameters(statement, {"items": [1, 2]}, max_values=1)
    with pytest.raises(SQLParameterError, match="too long"):
        bind_statement_parameters(statement, {"items": ["long"]}, max_string_length=2)
    with pytest.raises(SQLParameterError, match="finite"):
        bind_statement_parameters(statement, {"items": [float("inf")]})
    with pytest.raises(SQLParameterError, match="No value"):
        bind_statement_parameters(statement, {})


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT ?",
        "SELECT t.:column",
        "SELECT :value.field",
        "SELECT * FROM :table",
        "SELECT * FROM orders AS :alias",
        "SELECT * FROM orders LIMIT :limit",
        "SELECT * FROM orders OFFSET :offset",
    ],
)
def test_rejects_invalid_value_placeholder_locations(sql: str) -> None:
    statement = SQLStatement(sql, "postgresql")
    with pytest.raises(SQLQueryValidationError):
        validate_readonly_statement(statement)
    with pytest.raises(SQLQueryValidationError):
        get_statement_parameters(statement)
    with pytest.raises(SQLQueryValidationError):
        bind_statement_parameters(statement, {})


@pytest.mark.parametrize(
    ("value", "literal"),
    [
        (None, "NULL"),
        (True, "TRUE"),
        (False, "FALSE"),
        (42, "42"),
        (1.25, "1.25"),
        (np.int64(42), "42"),
        (np.float64(1.25), "1.25"),
        (np.bool_(False), "FALSE"),
        (Decimal("1.25"), "1.25"),
        (date(2026, 1, 2), "'2026-01-02'"),
        (datetime(2026, 1, 2, 3, 4, 5), "'2026-01-02T03:04:05'"),
        (pd.Timestamp("2026-01-02T03:04:05"), "'2026-01-02T03:04:05'"),
    ],
)
def test_bind_typed_scalar_literals(value: Any, literal: str) -> None:
    bound = bind_statement_parameters(SQLStatement("SELECT :value"), {"value": [value]})
    assert " ".join(bound.format().split()) == f"SELECT {literal}"
    validate_readonly_statement(bound, allow_placeholders=False)


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("-inf"), Decimal("NaN"), Decimal("Infinity")],
)
def test_rejects_nonfinite_parameter_values(value: Any) -> None:
    with pytest.raises(SQLParameterError, match="finite"):
        bind_statement_parameters(SQLStatement("SELECT :value"), {"value": [value]})


@pytest.mark.parametrize("value", [{"key": "value"}, [1], object()])
def test_rejects_unsupported_parameter_values(value: Any) -> None:
    with pytest.raises(SQLParameterError, match="Unsupported parameter value type"):
        bind_statement_parameters(SQLStatement("SELECT :value"), {"value": [value]})


def test_in_parameters_preserve_static_values() -> None:
    statement = SQLStatement("SELECT 1 WHERE 1 IN (0, :items, 3)")
    bound = bind_statement_parameters(statement, {"items": [1, 2]})
    assert " ".join(bound.format().split()) == "SELECT 1 WHERE 1 IN (0, 1, 2, 3)"
    with pytest.raises(SQLParameterError, match="between 1"):
        bind_statement_parameters(statement, {"items": []})
