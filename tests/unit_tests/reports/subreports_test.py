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
# pylint: disable=import-outside-toplevel, redefined-outer-name, unused-argument
from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest
from marshmallow import ValidationError
from pytest_mock import MockerFixture

from superset.db_engine_specs.postgres import PostgresEngineSpec
from superset.exceptions import SupersetSecurityException
from superset.reports import subreports
from superset.reports.models import ReportSchedule, ReportScheduleType, Subreport
from superset.reports.subreports import (
    bind_parameters,
    dataframe_to_csv,
    dataframe_to_payload,
    get_available_context_fields,
    get_sql_parameters,
    prepare_subreport_sql,
    resolve_context,
    run_subreport_query,
    SubreportAccessDeniedError,
    SubreportContext,
    SubreportError,
    SubreportInvalidSQLError,
    SubreportParameterError,
    SubreportScheduleError,
    SubreportsDisabledError,
    validate_param_mapping,
    validate_parent_schedule,
    validate_subreport_sql,
    validate_template,
)

ENGINE = "postgresql"
MAPPING = {"customer_id": "$F{customer_id}"}


@pytest.fixture
def database(mocker: MockerFixture) -> MagicMock:
    database = MagicMock()
    database.id = 1
    database.db_engine_spec = PostgresEngineSpec
    database.get_default_catalog.return_value = None
    database.resolve_query_default_schema.return_value = "public"
    database.mutate_sql_based_on_config.side_effect = lambda sql, is_split: sql
    return database


@pytest.fixture
def secured(mocker: MockerFixture, app_context: None) -> dict[str, MagicMock]:
    mocker.patch.object(subreports, "is_feature_enabled", return_value=True)
    return {
        "access": mocker.patch.object(subreports.security_manager, "raise_for_access"),
        "rls": mocker.patch("superset.utils.rls.apply_rls", return_value=False),
    }


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM orders WHERE customer_id = :customer_id",
        "WITH o AS (SELECT * FROM orders) SELECT * FROM o",
        "SELECT a FROM t1 UNION ALL SELECT a FROM t2",
        "SELECT * FROM orders WHERE customer_id IN (:customer_id)",
    ],
)
def test_validate_sql_accepts_read_only_queries(sql: str) -> None:
    validate_subreport_sql(sql, ENGINE)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; SELECT 2",
        "DELETE FROM orders",
        "UPDATE orders SET a = 1",
        "WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d",
        "SELECT * INTO new_orders FROM orders",
        "SELECT * FROM orders WHERE id = {{ current_user_id() }}",
        "SELECT * FROM",
        "SET search_path TO other",
        "SELECT * FROM orders FOR UPDATE",
        "",
    ],
)
def test_validate_sql_fails_closed(sql: str) -> None:
    with pytest.raises(SubreportInvalidSQLError):
        validate_subreport_sql(sql, ENGINE)


def test_validate_sql_rejects_placeholder_outside_value_position() -> None:
    with pytest.raises(SubreportInvalidSQLError):
        validate_subreport_sql("SELECT * FROM t LIMIT :n", ENGINE)
    with pytest.raises(SubreportInvalidSQLError):
        validate_subreport_sql("SELECT ?", "mysql")


def test_param_mapping_validation() -> None:
    params = get_sql_parameters(
        "SELECT * FROM t WHERE a = :customer_id AND b = :customer_id", ENGINE
    )
    assert params == ["customer_id"]
    assert validate_param_mapping(MAPPING, params, {"customer_id"}) == {
        "customer_id": "customer_id"
    }
    with pytest.raises(SubreportParameterError, match="not available"):
        validate_param_mapping(MAPPING, params, {"region"})
    with pytest.raises(SubreportParameterError, match="Missing mapping"):
        validate_param_mapping({}, params)
    with pytest.raises(SubreportParameterError, match="not used"):
        validate_param_mapping({**MAPPING, "other": "$F{x}"}, params)
    with pytest.raises(SubreportParameterError, match=r"\$F"):
        validate_param_mapping({"customer_id": "customer_id"}, params)


def test_bind_parameters_escapes_values_as_literals(app_context: None) -> None:
    context = SubreportContext(values={"customer_id": ["x' OR '1'='1"]})
    sql = bind_parameters(
        "SELECT * FROM orders WHERE customer_id = :customer_id",
        ENGINE,
        MAPPING,
        context,
    )
    assert "customer_id = 'x'' OR ''1''=''1'" in sql
    assert ":customer_id" not in sql


def test_bind_parameters_multi_value(app_context: None) -> None:
    context = SubreportContext(values={"customer_id": [1, 2]})
    sql = bind_parameters(
        "SELECT * FROM orders WHERE customer_id IN (:customer_id)",
        ENGINE,
        MAPPING,
        context,
    )
    assert "IN (1, 2)" in sql
    with pytest.raises(SubreportParameterError, match="exactly one"):
        bind_parameters(
            "SELECT * FROM orders WHERE customer_id = :customer_id",
            ENGINE,
            MAPPING,
            context,
        )
    with pytest.raises(SubreportParameterError, match="No value"):
        bind_parameters(
            "SELECT * FROM orders WHERE customer_id = :customer_id",
            ENGINE,
            MAPPING,
            SubreportContext(),
        )


def _dashboard_schedule(filters: list[dict[str, Any]]) -> ReportSchedule:
    schedule = ReportSchedule(dashboard_id=1)
    schedule.extra = {
        "dashboard": {"nativeFilters": filters}  # type: ignore[typeddict-unknown-key]
    }
    return schedule


def test_context_from_native_filters() -> None:
    schedule = _dashboard_schedule(
        [
            {
                "nativeFilterId": "NATIVE_FILTER-1",
                "filterType": "filter_select",
                "columnName": "customer_id",
                "filterValues": [3, 3, 4],
            },
            {"columnName": "region", "filterValues": ["EU"]},
            {"columnName": "region", "filterValues": ["US"]},
        ]
    )
    names = [item.name for item in get_available_context_fields(schedule)]
    assert names == ["customer_id", "region"]
    context = resolve_context(schedule)
    assert context.values["customer_id"] == [3, 4]
    with pytest.raises(SubreportParameterError, match="ambiguous"):
        context.get("region")
    assert resolve_context(schedule, explicit_values={"region": "EU"}).get(
        "region"
    ) == ["EU"]


def test_context_from_chart_data() -> None:
    df = pd.DataFrame({"customer_id": [1, 2, 1, None], "total": [5.0, 6.0, 7.0, 8.0]})
    context = resolve_context(chart_data=df)
    assert context.get("customer_id") == [1.0, 2.0]
    assert context.get("total") == [5.0, 6.0, 7.0, 8.0]


def test_prepare_applies_checks_rls_and_limit(
    database: MagicMock, secured: dict[str, MagicMock]
) -> None:
    sql, catalog, schema, limit = prepare_subreport_sql(
        database,
        "SELECT * FROM orders WHERE customer_id = :customer_id",
        MAPPING,
        SubreportContext(values={"customer_id": ["evil {{ x }}"]}),
        row_limit=10,
    )
    assert "LIMIT 11" in sql
    assert (catalog, schema, limit) == (None, "public", 10)
    secured["rls"].assert_called_once()
    kwargs = secured["access"].call_args.kwargs
    assert kwargs["force_dataset_match"] is True
    # Parameter values never reach the Jinja-aware access check.
    assert "evil" not in kwargs["sql"]
    assert "NULL" in kwargs["sql"]


def test_prepare_applies_rls_when_rls_in_sqllab_disabled(
    database: MagicMock, secured: dict[str, MagicMock], mocker: MockerFixture
) -> None:
    mocker.patch.dict(subreports.app.config, {"RLS_IN_SQLLAB": False})
    prepare_subreport_sql(database, "SELECT * FROM orders", {}, SubreportContext())
    secured["rls"].assert_called_once()


def test_prepare_denies_without_access(
    database: MagicMock, secured: dict[str, MagicMock]
) -> None:
    secured["access"].side_effect = SupersetSecurityException(MagicMock())
    with pytest.raises(SubreportAccessDeniedError):
        prepare_subreport_sql(database, "SELECT * FROM orders", {}, SubreportContext())


def test_prepare_enforces_denylists(
    database: MagicMock, secured: dict[str, MagicMock], mocker: MockerFixture
) -> None:
    mocker.patch.dict(
        subreports.app.config,
        {
            "DISALLOWED_SQL_FUNCTIONS": {ENGINE: {"pg_read_file"}},
            "DISALLOWED_SQL_TABLES": {ENGINE: {"pg_shadow"}},
        },
    )
    with pytest.raises(SubreportAccessDeniedError, match="functions"):
        prepare_subreport_sql(
            database, "SELECT pg_read_file('/etc/passwd')", {}, SubreportContext()
        )
    with pytest.raises(SubreportAccessDeniedError, match="tables"):
        prepare_subreport_sql(
            database, "SELECT * FROM pg_shadow", {}, SubreportContext()
        )


def test_prepare_revalidates_mutated_sql(
    database: MagicMock, secured: dict[str, MagicMock]
) -> None:
    database.mutate_sql_based_on_config.side_effect = (
        lambda sql, is_split: "DELETE FROM orders"
    )
    with pytest.raises(SubreportInvalidSQLError):
        prepare_subreport_sql(database, "SELECT * FROM orders", {}, SubreportContext())

    database.mutate_sql_based_on_config.side_effect = (
        lambda sql, is_split: "SELECT * FROM secrets"
    )
    with pytest.raises(SubreportAccessDeniedError, match="mutator"):
        prepare_subreport_sql(database, "SELECT * FROM orders", {}, SubreportContext())


def test_run_query_truncates_and_uses_get_df(
    database: MagicMock, secured: dict[str, MagicMock]
) -> None:
    database.get_df.return_value = pd.DataFrame({"a": [1, 2, 3]})
    result = run_subreport_query(
        database, "SELECT a FROM t", {}, SubreportContext(), row_limit=2
    )
    assert result.truncated is True
    assert result.df["a"].tolist() == [1, 2]
    assert "LIMIT 3" in database.get_df.call_args.args[0]


def test_run_query_requires_feature_flag(
    database: MagicMock, mocker: MockerFixture, app_context: None
) -> None:
    mocker.patch.object(subreports, "is_feature_enabled", return_value=False)
    with pytest.raises(SubreportsDisabledError):
        run_subreport_query(database, "SELECT 1", {}, SubreportContext())
    database.get_df.assert_not_called()


def test_execute_subreports_noop_without_subreports() -> None:
    assert subreports.execute_subreports(ReportSchedule()) == []


def test_payload_and_csv(app_context: None) -> None:
    df = pd.DataFrame({"name": ["=1+1", None], "value": [1.5, float("nan")]})
    payload = dataframe_to_payload(df, truncated=True)
    assert payload["columns"][0] == {"name": "name", "type": "object"}
    assert payload["data"] == [
        {"name": "=1+1", "value": 1.5},
        {"name": None, "value": None},
    ]
    assert payload["row_count"] == 2
    assert payload["truncated"] is True
    csv = dataframe_to_csv(df)
    assert csv.decode("utf-8-sig").splitlines()[0] == "name,value"
    assert b"\n=1+1" not in csv


def test_validate_template() -> None:
    assert validate_template("table", {"columns": ["a"]}) == {"columns": ["a"]}
    assert validate_template(
        "chart", {"chart_type": "line", "x_column": "day", "y_columns": ["n"]}
    ) == {"chart_type": "line", "x_column": "day", "y_columns": ["n"]}
    with pytest.raises(SubreportError):
        validate_template("chart", {"x_column": "day"})
    with pytest.raises(SubreportError):
        validate_template("table", {"chart_type": "bar"})
    with pytest.raises(SubreportError):
        validate_template("pie", {})


def _schedule(id_: int, parent: int | None = None) -> ReportSchedule:
    return ReportSchedule(
        id=id_, parent_schedule_id=parent, type=ReportScheduleType.REPORT
    )


def test_validate_parent_schedule(mocker: MockerFixture, app_context: None) -> None:
    schedules = {1: _schedule(1), 2: _schedule(2, 1), 3: _schedule(3, 2)}
    mocker.patch(
        "superset.daos.report.ReportScheduleDAO.find_by_id",
        side_effect=lambda pk: schedules.get(pk),
    )
    mocker.patch.object(
        subreports,
        "_parent_id_of",
        side_effect=lambda pk: schedules[pk].parent_schedule_id,
    )
    mocker.patch.object(subreports, "_subtree_height", return_value=0)

    assert validate_parent_schedule(None, None) is None
    assert validate_parent_schedule(None, 3) is schedules[3]
    with pytest.raises(SubreportScheduleError, match="own parent"):
        validate_parent_schedule(1, 1)
    with pytest.raises(SubreportScheduleError, match="cycle"):
        validate_parent_schedule(1, 3)
    with pytest.raises(SubreportScheduleError, match="not found"):
        validate_parent_schedule(None, 99)
    with pytest.raises(SubreportScheduleError, match="Only reports"):
        validate_parent_schedule(None, 1, schedule_type=ReportScheduleType.ALERT)

    mocker.patch.dict(subreports.app.config, {"ALERT_REPORTS_MAX_SCHEDULE_DEPTH": 2})
    with pytest.raises(SubreportScheduleError, match="nested at most"):
        validate_parent_schedule(None, 3)


def test_model_relationships() -> None:
    columns = set(Subreport.__table__.columns.keys())
    assert {
        "id",
        "uuid",
        "parent_schedule_id",
        "name",
        "sql_query",
        "database_id",
        "param_mapping",
        "position",
        "viz_type",
        "template",
    } <= columns
    parent = ReportSchedule(name="parent")
    child = ReportSchedule(name="child", parent_schedule=parent)
    assert parent.children == [child]
    subreport = Subreport(name="s", parent_schedule=parent)
    assert parent.subreports == [subreport]
    assert subreports.is_independently_scheduled(parent)


def test_subreport_schema(
    mocker: MockerFixture, database: MagicMock, app_context: None
) -> None:
    from superset.reports.schemas import ReportSchedulePutSchema, SubreportSchema

    mocker.patch("superset.reports.schemas.db.session.get", return_value=database)
    payload = {
        "name": "Orders",
        "sql_query": "SELECT * FROM orders WHERE customer_id = :customer_id",
        "database_id": 1,
        "param_mapping": MAPPING,
    }
    loaded = SubreportSchema(available_context_fields={"customer_id"}).load(payload)
    assert loaded["viz_type"] == "table"
    assert loaded["position"] == 0
    with pytest.raises(ValidationError, match="not available"):
        SubreportSchema(available_context_fields={"region"}).load(payload)
    with pytest.raises(ValidationError):
        SubreportSchema().load({**payload, "sql_query": "DELETE FROM orders"})
    with pytest.raises(ValidationError):
        SubreportSchema().load({**payload, "viz_type": "chart"})
    assert SubreportSchema(partial_update=True).load({"position": 2}) == {"position": 2}

    with pytest.raises(ValidationError, match="requires the schedule"):
        ReportSchedulePutSchema().load({"parent_schedule_id": 1})
