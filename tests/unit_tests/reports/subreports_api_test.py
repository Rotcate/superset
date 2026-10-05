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
# pylint: disable=redefined-outer-name, unused-argument, import-outside-toplevel
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest
from marshmallow import ValidationError
from pytest_mock import MockerFixture
from sqlalchemy.orm.session import Session

from superset import security_manager
from superset.commands.report.exceptions import (
    ReportScheduleForbiddenError,
    SubreportInvalidError,
)
from superset.db_engine_specs.postgres import PostgresEngineSpec
from superset.exceptions import SupersetSecurityException
from superset.reports.models import ReportSchedule, ReportScheduleType, Subreport
from superset.reports.subreports import SubreportQueryResult
from superset.utils import json
from tests.unit_tests.conftest import with_feature_flags

SQL = "SELECT * FROM orders WHERE customer_id = :customer_id"
MAPPING = {"customer_id": "$F{customer_id}"}
PAYLOAD = {
    "name": "Orders",
    "sql_query": SQL,
    "database_id": 1,
    "param_mapping": MAPPING,
}
NATIVE_FILTERS = [
    {
        "nativeFilterId": "NATIVE_FILTER-1",
        "columnName": "customer_id",
        "columnLabel": "Customer",
        "filterType": "filter_select",
        "filterValues": [42],
    }
]
RESPONSE_KEYS = {
    "id",
    "name",
    "database_id",
    "sql_query",
    "param_mapping",
    "position",
    "viz_type",
    "template",
}
CMD = "superset.commands.report.subreport"


def _parent(id_: int = 1) -> ReportSchedule:
    return ReportSchedule(
        id=id_,
        name=f"parent {id_}",
        type=ReportScheduleType.REPORT,
        dashboard_id=10,
        extra_json=json.dumps({"dashboard": {"nativeFilters": NATIVE_FILTERS}}),
    )


def _subreport(parent: ReportSchedule, id_: int = 5, position: int = 0) -> Subreport:
    return Subreport(
        id=id_,
        name=f"sub {id_}",
        sql_query=SQL,
        database_id=1,
        param_mapping=MAPPING,
        position=position,
        viz_type="table",
        template={},
        parent_schedule=parent,
    )


def _apply(item: Subreport, attributes: dict[str, Any]) -> Subreport:
    for key, value in attributes.items():
        setattr(item, key, value)
    return item


@pytest.fixture
def database() -> MagicMock:
    database = MagicMock()
    database.id = 1
    database.db_engine_spec = PostgresEngineSpec
    database.get_default_catalog.return_value = None
    database.resolve_query_default_schema.return_value = "public"
    database.mutate_sql_based_on_config.side_effect = lambda sql, is_split: sql
    return database


@pytest.fixture
def env(
    mocker: MockerFixture, database: MagicMock, full_api_access: None
) -> dict[str, Any]:
    """Two parents the user can see; the DAOs and data access are mocked."""
    parents = {1: _parent(1), 2: _parent(2)}
    children = {5: _subreport(parents[1], 5), 6: _subreport(parents[2], 6)}

    def find_by_parent(parent_id: int, subreport_id: int) -> Subreport | None:
        child = children.get(subreport_id)
        return child if child and child.parent_schedule.id == parent_id else None

    mocker.patch(
        "superset.daos.report.ReportScheduleDAO.find_by_id",
        side_effect=lambda pk: parents.get(pk),
    )
    mocker.patch(
        "superset.daos.report.SubreportDAO.find_by_parent",
        side_effect=find_by_parent,
    )
    mocker.patch(
        "superset.daos.database.DatabaseDAO.find_by_id",
        side_effect=lambda pk: database if pk == 1 else None,
    )
    mocker.patch(
        "superset.reports.schemas.db.session.get",
        side_effect=lambda model, pk: database if pk in (1, 2) else None,
    )
    return {
        "parents": parents,
        "children": children,
        "editorship": mocker.patch.object(security_manager, "raise_for_editorship"),
        "prepare": mocker.patch(f"{CMD}.prepare_subreport_sql"),
        "create": mocker.patch(
            "superset.daos.report.SubreportDAO.create",
            side_effect=lambda attributes: Subreport(id=7, **attributes),
        ),
        "update": mocker.patch(
            "superset.daos.report.SubreportDAO.update", side_effect=_apply
        ),
        "delete": mocker.patch("superset.daos.report.SubreportDAO.delete"),
    }


def _deny_editorship(env: dict[str, Any]) -> None:
    env["editorship"].side_effect = SupersetSecurityException(MagicMock())


@with_feature_flags(ALERT_REPORTS=False)
@pytest.mark.parametrize(
    "method,url",
    [
        ("get", "/api/v1/report/1/subreport/"),
        ("post", "/api/v1/report/1/subreport/"),
        ("get", "/api/v1/report/1/subreport/5"),
        ("put", "/api/v1/report/1/subreport/5"),
        ("delete", "/api/v1/report/1/subreport/5"),
        ("post", "/api/v1/report/1/subreport/execute_preview"),
        ("post", "/api/v1/subreport/execute_preview"),
    ],
)
def test_routes_require_feature_flag(
    client: Any, env: dict[str, Any], method: str, url: str
) -> None:
    rv = getattr(client, method)(url, json=PAYLOAD)
    assert rv.status_code == 404
    env["create"].assert_not_called()


@with_feature_flags(ALERT_REPORTS=True)
def test_list(client: Any, env: dict[str, Any]) -> None:
    parent = env["parents"][1]
    _subreport(parent, 8, position=-1)

    rv = client.get("/api/v1/report/1/subreport/")

    assert rv.status_code == 200
    result = rv.json["result"]
    assert [item["id"] for item in result] == [8, 5]
    assert all(set(item) == RESPONSE_KEYS for item in result)
    assert result[1]["param_mapping"] == MAPPING
    assert rv.json["context_fields"] == [
        {
            "name": "customer_id",
            "source": "native_filter",
            "label": "Customer",
            "filter_id": "NATIVE_FILTER-1",
            "filter_type": "filter_select",
            "reference": "$F{customer_id}",
        }
    ]


@with_feature_flags(ALERT_REPORTS=True)
def test_missing_or_inaccessible_parent_is_404(
    client: Any, env: dict[str, Any]
) -> None:
    for rv in (
        client.get("/api/v1/report/99/subreport/"),
        client.get("/api/v1/report/99/subreport/5"),
        client.post("/api/v1/report/99/subreport/", json=PAYLOAD),
        client.put("/api/v1/report/99/subreport/5", json={"name": "x"}),
        client.delete("/api/v1/report/99/subreport/5"),
        client.post(
            "/api/v1/report/99/subreport/execute_preview",
            json={"sql_query": SQL, "database_id": 1, "param_mapping": MAPPING},
        ),
    ):
        assert rv.status_code == 404


@with_feature_flags(ALERT_REPORTS=True)
def test_cross_parent_child_is_404(client: Any, env: dict[str, Any]) -> None:
    # Subreport 6 belongs to parent 2 and must not be reachable via parent 1.
    assert client.get("/api/v1/report/1/subreport/6").status_code == 404
    assert (
        client.put("/api/v1/report/1/subreport/6", json={"name": "x"}).status_code
        == 404
    )
    assert client.delete("/api/v1/report/1/subreport/6").status_code == 404
    env["update"].assert_not_called()
    env["delete"].assert_not_called()

    rv = client.get("/api/v1/report/2/subreport/6")
    assert rv.status_code == 200
    assert rv.json["id"] == 6
    assert set(rv.json["result"]) == RESPONSE_KEYS


@with_feature_flags(ALERT_REPORTS=True)
def test_read_allowed_but_mutations_require_editorship(
    client: Any, env: dict[str, Any]
) -> None:
    _deny_editorship(env)
    assert client.get("/api/v1/report/1/subreport/").status_code == 200
    assert client.get("/api/v1/report/1/subreport/5").status_code == 200
    assert client.post("/api/v1/report/1/subreport/", json=PAYLOAD).status_code == 403
    assert (
        client.put("/api/v1/report/1/subreport/5", json={"name": "x"}).status_code
        == 403
    )
    assert client.delete("/api/v1/report/1/subreport/5").status_code == 403
    assert (
        client.post(
            "/api/v1/report/1/subreport/execute_preview",
            json={"sql_query": SQL, "database_id": 1, "param_mapping": MAPPING},
        ).status_code
        == 403
    )
    env["create"].assert_not_called()
    env["update"].assert_not_called()
    env["delete"].assert_not_called()


@with_feature_flags(ALERT_REPORTS=True)
def test_create(client: Any, env: dict[str, Any], database: MagicMock) -> None:
    rv = client.post("/api/v1/report/1/subreport/", json=PAYLOAD)

    assert rv.status_code == 201
    assert rv.json["id"] == 7
    assert set(rv.json["result"]) == RESPONSE_KEYS
    attributes = env["create"].call_args.kwargs["attributes"]
    assert attributes["parent_schedule_id"] == 1
    assert attributes["param_mapping"] == MAPPING
    # The same secure preparation used by execution validates data access.
    assert env["prepare"].call_args.args[0] is database
    env["editorship"].assert_called_with(env["parents"][1])


@with_feature_flags(ALERT_REPORTS=True)
def test_create_cannot_choose_parent(client: Any, env: dict[str, Any]) -> None:
    rv = client.post(
        "/api/v1/report/1/subreport/", json={**PAYLOAD, "parent_schedule_id": 2}
    )
    assert rv.status_code == 400
    env["create"].assert_not_called()


@with_feature_flags(ALERT_REPORTS=True)
@pytest.mark.parametrize(
    "payload",
    [
        {**PAYLOAD, "sql_query": "DELETE FROM orders"},
        {**PAYLOAD, "sql_query": "SELECT 1; DROP TABLE orders"},
        {**PAYLOAD, "param_mapping": {"customer_id": "$F{region}"}},
        {**PAYLOAD, "param_mapping": {}},
        {**PAYLOAD, "viz_type": "chart", "template": {"chart_type": "pie"}},
        {**PAYLOAD, "database_id": 3},
        {"name": "missing fields"},
    ],
)
def test_create_invalid(
    client: Any, env: dict[str, Any], payload: dict[str, Any]
) -> None:
    rv = client.post("/api/v1/report/1/subreport/", json=payload)
    assert rv.status_code in (400, 422)
    env["create"].assert_not_called()


@with_feature_flags(ALERT_REPORTS=True)
def test_create_inaccessible_database_is_403(client: Any, env: dict[str, Any]) -> None:
    # Database 2 exists but is hidden by the database base filter.
    rv = client.post("/api/v1/report/1/subreport/", json={**PAYLOAD, "database_id": 2})
    assert rv.status_code == 403
    env["create"].assert_not_called()


@with_feature_flags(ALERT_REPORTS=True)
def test_create_inaccessible_data_is_403(client: Any, env: dict[str, Any]) -> None:
    from superset.reports.subreports import SubreportAccessDeniedError

    env["prepare"].side_effect = SubreportAccessDeniedError("denied")
    rv = client.post("/api/v1/report/1/subreport/", json=PAYLOAD)
    assert rv.status_code == 403
    env["create"].assert_not_called()


@with_feature_flags(ALERT_REPORTS=True)
def test_update(client: Any, env: dict[str, Any]) -> None:
    rv = client.put("/api/v1/report/1/subreport/5", json={"name": "New", "position": 3})

    assert rv.status_code == 200
    assert rv.json["result"]["name"] == "New"
    assert rv.json["result"]["position"] == 3
    item, attributes = env["update"].call_args.args
    assert item is env["children"][5]
    assert attributes == {"name": "New", "position": 3}
    env["prepare"].assert_not_called()

    rv = client.put(
        "/api/v1/report/1/subreport/5",
        json={"sql_query": "SELECT * FROM t WHERE customer_id IN (:customer_id)"},
    )
    assert rv.status_code == 200
    env["prepare"].assert_called_once()

    rv = client.put("/api/v1/report/1/subreport/5", json={"sql_query": "DROP TABLE t"})
    assert rv.status_code in (400, 422)
    rv = client.put("/api/v1/report/1/subreport/5", json={"parent_schedule_id": 2})
    assert rv.status_code == 400
    assert env["children"][5].parent_schedule.id == 1


@with_feature_flags(ALERT_REPORTS=True)
def test_delete(client: Any, env: dict[str, Any]) -> None:
    rv = client.delete("/api/v1/report/1/subreport/5")
    assert rv.status_code == 200
    env["delete"].assert_called_once_with([env["children"][5]])


@with_feature_flags(ALERT_REPORTS=True)
def test_preview_shares_secure_execution(
    mocker: MockerFixture, client: Any, full_api_access: None, database: MagicMock
) -> None:
    # No query helpers are mocked: preview runs the real secure execution path.
    mocker.patch(
        "superset.daos.report.ReportScheduleDAO.find_by_id",
        side_effect=lambda pk: _parent(pk) if pk == 1 else None,
    )
    mocker.patch("superset.daos.database.DatabaseDAO.find_by_id", return_value=database)
    mocker.patch.object(security_manager, "raise_for_editorship")
    access = mocker.patch.object(security_manager, "raise_for_access")
    rls = mocker.patch("superset.utils.rls.apply_rls", return_value=False)
    database.get_df.return_value = pd.DataFrame(
        {"id": [1, 2], "amount": [1.5, float("nan")]}
    )

    rv = client.post(
        "/api/v1/report/1/subreport/execute_preview",
        json={
            "sql_query": SQL,
            "database_id": 1,
            "param_mapping": MAPPING,
            "values": {"customer_id": 7},
            "row_limit": 1,
        },
    )

    assert rv.status_code == 200, rv.json
    assert rv.json == {
        "result": {
            "columns": ["id", "amount"],
            "data": [{"id": 1, "amount": 1.5}],
            "row_count": 1,
            "truncated": True,
        }
    }
    access.assert_called_once()
    rls.assert_called_once()
    executed_sql = database.get_df.call_args.args[0]
    assert "customer_id = 7" in executed_sql
    assert "LIMIT 2" in executed_sql


@with_feature_flags(ALERT_REPORTS=True)
def test_preview_uses_parent_context_and_explicit_values(
    mocker: MockerFixture, client: Any, env: dict[str, Any], database: MagicMock
) -> None:
    run = mocker.patch(
        f"{CMD}.run_subreport_query",
        return_value=SubreportQueryResult(
            df=pd.DataFrame({"a": [1]}), truncated=False, row_limit=10, executed_sql=""
        ),
    )
    body = {"sql_query": SQL, "database_id": 1, "param_mapping": MAPPING}

    rv = client.post("/api/v1/report/1/subreport/execute_preview", json=body)
    assert rv.status_code == 200
    assert rv.json["result"]["data"] == [{"a": 1}]
    context = run.call_args.args[3]
    assert context.values["customer_id"] == [42]

    rv = client.post(
        "/api/v1/subreport/execute_preview",
        json={**body, "parent_schedule_id": 1, "values": {"customer_id": [1, 2]}},
    )
    assert rv.status_code == 200
    assert run.call_args.args[3].values["customer_id"] == [1, 2]

    assert (
        client.post("/api/v1/subreport/execute_preview", json=body).status_code == 400
    )
    assert (
        client.post(
            "/api/v1/subreport/execute_preview", json={**body, "parent_schedule_id": 99}
        ).status_code
        == 404
    )
    rv = client.post(
        "/api/v1/report/1/subreport/execute_preview",
        json={**body, "param_mapping": {"customer_id": "$F{region}"}},
    )
    assert rv.status_code == 422
    rv = client.post(
        "/api/v1/report/1/subreport/execute_preview",
        json={**body, "sql_query": "UPDATE orders SET a = 1"},
    )
    assert rv.status_code in (400, 422)
    assert run.call_count == 2


@with_feature_flags(ALERT_REPORTS=True)
def test_schedule_put_passes_id_to_cycle_validation(
    mocker: MockerFixture, client: Any, full_api_access: None
) -> None:
    validate = mocker.patch("superset.reports.subreports.validate_parent_schedule")
    command = mocker.patch("superset.reports.api.UpdateReportScheduleCommand")
    command.return_value.run.return_value = MagicMock(id=3)

    rv = client.put("/api/v1/report/3", json={"parent_schedule_id": 1})

    assert rv.status_code == 200
    validate.assert_called_once_with(3, 1, schedule_type=None)
    command.assert_called_once_with(3, {"parent_schedule_id": 1})

    from superset.reports.subreports import SubreportScheduleError

    validate.side_effect = SubreportScheduleError("Report schedule nesting cycle")
    rv = client.put("/api/v1/report/3", json={"parent_schedule_id": 1})
    assert rv.status_code == 400
    assert "parent_schedule_id" in rv.json["message"]


def test_schedule_composition_validation(mocker: MockerFixture) -> None:
    from superset.commands.report.subreport import validate_schedule_composition
    from superset.reports.subreports import SubreportScheduleError

    validate = mocker.patch(f"{CMD}.validate_parent_schedule")
    editorship = mocker.patch.object(security_manager, "raise_for_editorship")
    model = _parent(3)

    # No composition keys: nothing to validate, existing behavior unchanged.
    exceptions: list[ValidationError] = []
    validate_schedule_composition(model, {"name": "x"}, exceptions)
    validate_schedule_composition(None, {"type": "Report"}, exceptions)
    assert exceptions == []
    validate.assert_not_called()

    validate_schedule_composition(model, {"parent_schedule_id": 1}, exceptions)
    validate.assert_called_once_with(3, 1, schedule_type=ReportScheduleType.REPORT)
    editorship.assert_called_once_with(validate.return_value)

    editorship.side_effect = SupersetSecurityException(MagicMock())
    with pytest.raises(ReportScheduleForbiddenError):
        validate_schedule_composition(
            None, {"type": "Report", "parent_schedule_id": 1}, exceptions
        )

    validate.side_effect = SubreportScheduleError("cycle")
    validate_schedule_composition(model, {"parent_schedule_id": 2}, exceptions)
    assert exceptions[-1].field_name == "parent_schedule_id"

    exceptions = []
    validate_schedule_composition(
        None, {"type": "Alert", "subreports": [PAYLOAD]}, exceptions
    )
    assert exceptions[0].field_name == "subreports"


@pytest.fixture
def tables(session: Session) -> Session:
    ReportSchedule.metadata.create_all(session.get_bind())  # pylint: disable=no-member
    return session


def _saved_parent(session: Session, name: str) -> ReportSchedule:
    parent = ReportSchedule(
        name=name,
        type=ReportScheduleType.REPORT,
        crontab="0 9 * * *",
        dashboard_id=10,
        extra_json=json.dumps({"dashboard": {"nativeFilters": NATIVE_FILTERS}}),
    )
    session.add(parent)
    session.flush()
    return parent


def test_dao_find_by_parent(tables: Session) -> None:
    from superset.daos.report import SubreportDAO

    one, two = _saved_parent(tables, "one"), _saved_parent(tables, "two")
    child = Subreport(name="c", sql_query=SQL, database_id=1, parent_schedule=one)
    tables.add(child)
    tables.flush()

    assert SubreportDAO.find_by_parent(one.id, child.id) is child
    assert SubreportDAO.find_by_parent(two.id, child.id) is None


@with_feature_flags(ALERT_REPORTS=True)
def test_apply_nested_subreports(
    mocker: MockerFixture, tables: Session, database: MagicMock
) -> None:
    from superset.commands.report.subreport import apply_nested_subreports

    mocker.patch("superset.daos.database.DatabaseDAO.find_by_id", return_value=database)
    prepare = mocker.patch(f"{CMD}.prepare_subreport_sql")
    parent = _saved_parent(tables, "parent")

    apply_nested_subreports(parent, [PAYLOAD, {**PAYLOAD, "name": "B", "position": 1}])
    tables.flush()
    assert [item.name for item in parent.subreports] == ["Orders", "B"]
    assert prepare.call_count == 2

    apply_nested_subreports(parent, [{**PAYLOAD, "name": "C"}])
    tables.flush()
    assert [item.name for item in parent.subreports] == ["C"]
    assert tables.query(Subreport).count() == 1

    with pytest.raises(SubreportInvalidError) as excinfo:
        apply_nested_subreports(
            parent, [{**PAYLOAD, "param_mapping": {"customer_id": "$F{region}"}}]
        )
    assert "subreports" in excinfo.value.normalized_messages()
