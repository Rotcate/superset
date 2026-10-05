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
