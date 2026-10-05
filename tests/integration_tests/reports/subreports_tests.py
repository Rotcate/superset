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
# Hostile and malformed SQL literals are deliberate test inputs.
# ruff: noqa: S608
"""
Integration tests for report subreports against the real metadata and
examples databases: CRUD and object-level authorization, SQL validation,
parameter binding, parent context, preview, RLS, denylists, nested schedules
and scheduled delivery.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from typing import Any
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from flask.testing import FlaskClient
from flask_appbuilder.security.sqla.models import Role, User
from freezegun import freeze_time
from sqlalchemy import text

from superset import db, security_manager
from superset.commands.report.exceptions import ReportScheduleSubreportFailedError
from superset.commands.report.execute import AsyncExecuteReportScheduleCommand
from superset.connectors.sqla.models import RowLevelSecurityFilter, SqlaTable
from superset.key_value.models import KeyValueEntry
from superset.models.core import Log
from superset.models.dashboard import Dashboard
from superset.reports.models import (
    ReportDataFormat,
    ReportExecutionLog,
    ReportRecipients,
    ReportRecipientType,
    ReportSchedule,
    ReportScheduleType,
    ReportState,
    Subreport,
)
from superset.subjects.models import Subject
from superset.subjects.types import SubjectType
from superset.tasks.scheduler import scheduler
from superset.utils import json
from superset.utils.database import get_example_database
from tests.integration_tests.conftest import with_feature_flags
from tests.integration_tests.constants import ADMIN_USERNAME
from tests.integration_tests.dashboard_utils import create_table_metadata
from tests.integration_tests.reports.utils import (
    _subjects_for_users,
    insert_report_schedule,
    SCREENSHOT_FILE,
    TEST_ID,
)
from tests.integration_tests.test_app import app, login

PASSWORD = "general"  # noqa: S105
PREFIX = "sr_it_"
ORDERS = "sr_it_orders"
SECRETS = "sr_it_secrets"
DASHBOARD_SLUG = "sr-it-parent"

EDITOR_ROLE = "sr_it_report_editor"
REPORTS_ONLY_ROLE = "sr_it_reports_only"
EAST_ROLE = "sr_it_east"
WEST_ROLE = "sr_it_west"

OWNER = "sr_it_owner"
WEST = "sr_it_west_user"
OTHER = "sr_it_other"
NODATA = "sr_it_nodata"

CUSTOMER_FILTER = {
    "nativeFilterId": "NATIVE_FILTER-customer",
    "name": "Customer",
    "columnName": "customer_id",
    "filterType": "filter_select",
    "filterValues": [1, 2, 3],
}
IN_CUSTOMERS = (
    f"SELECT customer_id, region, amount FROM {ORDERS} "  # noqa: S608
    "WHERE customer_id IN (:customer_id) ORDER BY customer_id"
)
CUSTOMER_MAPPING = {"customer_id": "$F{customer_id}"}


@dataclass(frozen=True)
class SubreportEnv:
    """Identifiers of the objects created for one test."""

    database_id: int
    dashboard_id: int
    parent_id: int
    other_parent_id: int
    ambiguous_parent_id: int


def _execute(sql: str) -> None:
    database = get_example_database()
    with database.get_sqla_engine() as engine:
        with engine.begin() as connection:
            connection.execute(text(sql))


def _drop_tables() -> None:
    for table in (ORDERS, SECRETS):
        _execute(f"DROP TABLE IF EXISTS {table}")


def _role_subject(role: Role) -> Subject:
    return (
        db.session.query(Subject)
        .filter_by(role_id=role.id, type=SubjectType.ROLE)
        .one()
    )


def _user(username: str) -> User:
    user = security_manager.find_user(username=username)
    assert user is not None
    return user


def _add_role(name: str, permissions: list[tuple[str, str]]) -> Role:
    role = security_manager.add_role(name)
    for permission, view in permissions:
        pvm = security_manager.find_permission_view_menu(permission, view)
        if pvm is None:
            pvm = security_manager.add_permission_view_menu(permission, view)
        security_manager.add_permission_role(role, pvm)
    return role


def _add_user(username: str, roles: list[Role]) -> User:
    security_manager.add_user(
        username,
        username,
        username,
        f"{username}@subreports.test",
        [security_manager.find_role("Gamma"), *roles],
        password=PASSWORD,
    )
    db.session.commit()
    return _user(username)


def _recipients() -> list[ReportRecipients]:
    return [
        ReportRecipients(
            type=ReportRecipientType.EMAIL,
            recipient_config_json=json.dumps({"target": "sr@subreports.test"}),
        )
    ]


def _insert_parent(
    name: str,
    editors: list[User],
    dashboard: Dashboard,
    native_filters: list[dict[str, Any]],
    report_type: ReportScheduleType = ReportScheduleType.REPORT,
) -> ReportSchedule:
    return insert_report_schedule(
        type=report_type,
        name=f"{PREFIX}{name}",
        crontab="0 9 * * *",
        dashboard=dashboard,
        editors=_subjects_for_users(editors),
        recipients=_recipients(),
        report_format=ReportDataFormat.PNG,
        extra={"dashboard": {"nativeFilters": native_filters}},
        sql="SELECT 1" if report_type == ReportScheduleType.ALERT else None,
        database=get_example_database()
        if report_type == ReportScheduleType.ALERT
        else None,
    )


def _cleanup_schedules() -> None:
    schedules = (
        db.session.query(ReportSchedule)
        .filter(ReportSchedule.name.like(f"{PREFIX}%"))
        .all()
    )
    ids = [schedule.id for schedule in schedules]
    if not ids:
        return
    for model, column in (
        (Subreport, Subreport.parent_schedule_id),
        (ReportExecutionLog, ReportExecutionLog.report_schedule_id),
        (ReportRecipients, ReportRecipients.report_schedule_id),
    ):
        db.session.query(model).filter(column.in_(ids)).delete(
            synchronize_session=False
        )
    for schedule in schedules:
        schedule.parent_schedule_id = None
    db.session.flush()
    for schedule in schedules:
        db.session.delete(schedule)
    db.session.commit()


def _cleanup_users() -> None:
    users = [
        user
        for username in (OWNER, WEST, OTHER, NODATA)
        if (user := security_manager.find_user(username=username))
    ]
    user_ids = [user.id for user in users]
    # Report execution writes key-value state and event logs as the executor.
    for column in ("created_by_fk", "changed_by_fk"):
        db.session.query(KeyValueEntry).filter(
            getattr(KeyValueEntry, column).in_(user_ids)
        ).update({column: None}, synchronize_session=False)
    db.session.query(Log).filter(Log.user_id.in_(user_ids)).delete(
        synchronize_session=False
    )
    for user in users:
        db.session.delete(user)
    db.session.commit()
    for role_name in (EDITOR_ROLE, REPORTS_ONLY_ROLE, EAST_ROLE, WEST_ROLE):
        if role := security_manager.find_role(role_name):
            db.session.delete(role)
    db.session.commit()


def _cleanup() -> None:
    db.session.rollback()
    _cleanup_schedules()
    for rls in db.session.query(RowLevelSecurityFilter).filter(
        RowLevelSecurityFilter.name.like(f"{PREFIX}%")
    ):
        db.session.delete(rls)
    if dashboard := db.session.query(Dashboard).filter_by(slug=DASHBOARD_SLUG).first():
        db.session.delete(dashboard)
    db.session.commit()
    _cleanup_users()
    for table in db.session.query(SqlaTable).filter(
        SqlaTable.table_name.in_([ORDERS, SECRETS])
    ):
        db.session.delete(table)
    db.session.commit()
    _drop_tables()


@pytest.fixture
def env(app_context: Any) -> Iterator[SubreportEnv]:
    _cleanup()
    _execute(
        f"CREATE TABLE {ORDERS} "
        "(customer_id INTEGER, region VARCHAR(16), amount INTEGER)"
    )
    _execute(
        f"INSERT INTO {ORDERS} VALUES "  # noqa: S608
        "(1, 'east', 10), (2, 'east', 20), (3, 'west', 30), (4, 'west', 40)"
    )
    _execute(f"CREATE TABLE {SECRETS} (id INTEGER, secret VARCHAR(32))")
    _execute(f"INSERT INTO {SECRETS} VALUES (1, 's3cr3t')")  # noqa: S608
    database = get_example_database()
    orders = create_table_metadata(ORDERS, database)
    orders.catalog = database.get_default_catalog()
    secrets = create_table_metadata(SECRETS, database)
    secrets.catalog = database.get_default_catalog()
    db.session.commit()

    report_perms = [("can_read", "ReportSchedule"), ("can_write", "ReportSchedule")]
    editor_role = _add_role(
        EDITOR_ROLE, [*report_perms, ("datasource_access", orders.perm)]
    )
    reports_only_role = _add_role(REPORTS_ONLY_ROLE, report_perms)
    east_role = _add_role(EAST_ROLE, [])
    west_role = _add_role(WEST_ROLE, [])
    db.session.commit()

    owner = _add_user(OWNER, [editor_role, east_role])
    west = _add_user(WEST, [editor_role, west_role])
    other = _add_user(OTHER, [editor_role])
    nodata = _add_user(NODATA, [reports_only_role])
    admin = _user(ADMIN_USERNAME)

    for name, role, clause in (
        ("east", east_role, "region = 'east'"),
        ("west", west_role, "region = 'west'"),
    ):
        rls = RowLevelSecurityFilter(
            name=f"{PREFIX}rls_{name}", filter_type="Regular", clause=clause
        )
        rls.tables = [orders]
        rls.subjects = [_role_subject(role)]
        db.session.add(rls)

    dashboard = Dashboard(
        dashboard_title="Subreport parent", slug=DASHBOARD_SLUG, published=True
    )
    db.session.add(dashboard)
    db.session.commit()

    parent = _insert_parent(
        "parent",
        [admin, owner, west, nodata],
        dashboard,
        [
            CUSTOMER_FILTER,
            {
                "nativeFilterId": "NATIVE_FILTER-region",
                "columnName": "region",
                "filterValues": ["east"],
            },
        ],
    )
    other_parent = _insert_parent("other_parent", [other], dashboard, [CUSTOMER_FILTER])
    ambiguous_parent = _insert_parent(
        "ambiguous_parent",
        [admin],
        dashboard,
        [
            {
                "nativeFilterId": "NATIVE_FILTER-a",
                "columnName": "region",
                "filterValues": ["east"],
            },
            {
                "nativeFilterId": "NATIVE_FILTER-b",
                "columnName": "region",
                "filterValues": ["west"],
            },
        ],
    )
    yield SubreportEnv(
        database_id=database.id,
        dashboard_id=dashboard.id,
        parent_id=parent.id,
        other_parent_id=other_parent.id,
        ambiguous_parent_id=ambiguous_parent.id,
    )
    _cleanup()


def _login(client: FlaskClient, username: str) -> None:
    client.get("/logout/", follow_redirects=True)
    login(client, username, PASSWORD)


def _url(parent_id: int, suffix: str = "") -> str:
    return f"/api/v1/report/{parent_id}/subreport/{suffix}"


def _definition(env: SubreportEnv, **overrides: Any) -> dict[str, Any]:
    return {
        "name": "Orders",
        "database_id": env.database_id,
        "sql_query": IN_CUSTOMERS,
        "param_mapping": CUSTOMER_MAPPING,
        "position": 0,
        "viz_type": "table",
        "template": {"title": "Orders"},
        **overrides,
    }


def _preview(client: FlaskClient, env: SubreportEnv, sql: str, **overrides: Any) -> Any:
    payload = {
        "database_id": env.database_id,
        "sql_query": sql,
        "param_mapping": {},
        "values": {},
        **overrides,
    }
    parent_id = overrides.pop("parent_id", None) or env.parent_id
    payload.pop("parent_id", None)
    return client.post(_url(parent_id, "execute_preview"), json=payload)


def _subreport_count() -> int:
    return db.session.query(Subreport).count()


def _create(env: SubreportEnv, **overrides: Any) -> Subreport:
    subreport = Subreport(
        parent_schedule_id=overrides.pop("parent_schedule_id", env.parent_id),
        **{
            key: value
            for key, value in _definition(env, **overrides).items()
            if key != "database_id"
        },
        database_id=env.database_id,
    )
    db.session.add(subreport)
    db.session.commit()
    return subreport


# --------------------------------------------------------------------------- #
# Feature flag
# --------------------------------------------------------------------------- #
@with_feature_flags(ALERT_REPORTS=False)
def test_routes_are_hidden_when_alert_reports_is_off(
    env: SubreportEnv, test_client: FlaskClient
) -> None:
    subreport = _create(env)
    _login(test_client, ADMIN_USERNAME)
    assert test_client.get(_url(env.parent_id)).status_code == 404
    assert test_client.get(_url(env.parent_id, str(subreport.id))).status_code == 404
    assert (
        test_client.post(_url(env.parent_id), json=_definition(env)).status_code == 404
    )
    assert _preview(test_client, env, "SELECT 1 AS one").status_code == 404
    assert (
        test_client.post(
            "/api/v1/subreport/execute_preview",
            json={
                "parent_schedule_id": env.parent_id,
                "database_id": env.database_id,
                "sql_query": "SELECT 1 AS one",
            },
        ).status_code
        == 404
    )
    assert _subreport_count() == 1


# --------------------------------------------------------------------------- #
# CRUD and object-level authorization
# --------------------------------------------------------------------------- #
def test_editor_crud_round_trip(env: SubreportEnv, test_client: FlaskClient) -> None:
    _login(test_client, OWNER)
    rv = test_client.post(_url(env.parent_id), json=_definition(env, position=1))
    assert rv.status_code == 201, rv.json
    table_id = rv.json["id"]
    rv = test_client.post(
        _url(env.parent_id),
        json=_definition(
            env,
            name="By region",
            sql_query=f"SELECT region, SUM(amount) AS amount FROM {ORDERS} "  # noqa: S608
            "GROUP BY region",
            param_mapping={},
            position=0,
            viz_type="chart",
            template={
                "chart_type": "bar",
                "x_column": "region",
                "y_columns": ["amount"],
            },
        ),
    )
    assert rv.status_code == 201, rv.json
    chart_id = rv.json["id"]

    rv = test_client.get(_url(env.parent_id))
    assert rv.status_code == 200
    assert [item["id"] for item in rv.json["result"]] == [chart_id, table_id]
    assert set(rv.json["result"][0]) == {
        "id",
        "name",
        "database_id",
        "sql_query",
        "param_mapping",
        "position",
        "viz_type",
        "template",
    }
    fields_ = {item["name"]: item for item in rv.json["context_fields"]}
    assert fields_["customer_id"]["reference"] == "$F{customer_id}"
    assert fields_["customer_id"]["source"] == "native_filter"
    assert fields_["customer_id"]["filter_id"] == "NATIVE_FILTER-customer"

    rv = test_client.get(_url(env.parent_id, str(table_id)))
    assert rv.status_code == 200
    assert rv.json["result"]["param_mapping"] == CUSTOMER_MAPPING

    rv = test_client.put(
        _url(env.parent_id, str(table_id)), json={"name": "Renamed", "position": 7}
    )
    assert rv.status_code == 200, rv.json
    stored = db.session.get(Subreport, table_id)
    db.session.refresh(stored)
    assert (stored.name, stored.position) == ("Renamed", 7)
    assert stored.parent_schedule_id == env.parent_id

    rv = test_client.delete(_url(env.parent_id, str(table_id)))
    assert rv.status_code == 200
    db.session.expire_all()
    assert db.session.get(Subreport, table_id) is None
    assert test_client.get(_url(env.parent_id, str(table_id))).status_code == 404


def test_report_schedule_post_with_nested_subreports(
    env: SubreportEnv, test_client: FlaskClient
) -> None:
    _login(test_client, ADMIN_USERNAME)
    rv = test_client.post(
        "/api/v1/report/",
        json={
            "type": ReportScheduleType.REPORT,
            "name": f"{PREFIX}nested_post",
            "crontab": "0 9 * * *",
            "dashboard": env.dashboard_id,
            "creation_method": "alerts_reports",
            "report_format": ReportDataFormat.PNG,
            "recipients": [
                {
                    "type": ReportRecipientType.EMAIL,
                    "recipient_config_json": {"target": "sr@subreports.test"},
                }
            ],
            "subreports": [
                _definition(env, sql_query=ALL_ORDERS, param_mapping={}),
                _definition(env, name="Second", sql_query=ALL_ORDERS, param_mapping={}),
            ],
        },
    )
    assert rv.status_code == 201, rv.json
    schedule = db.session.get(ReportSchedule, rv.json["id"])
    assert [item.name for item in schedule.subreports] == ["Orders", "Second"]


def test_foreign_parent_cannot_read_or_modify_children(
    env: SubreportEnv, test_client: FlaskClient
) -> None:
    subreport = _create(env)
    _login(test_client, OTHER)
    child = str(subreport.id)
    # The parent is not visible to a non-editor.
    assert test_client.get(_url(env.parent_id)).status_code == 404
    assert test_client.get(_url(env.parent_id, child)).status_code == 404
    assert (
        test_client.post(_url(env.parent_id), json=_definition(env)).status_code == 404
    )
    assert (
        test_client.put(_url(env.parent_id, child), json={"name": "x"}).status_code
        == 404
    )
    assert test_client.delete(_url(env.parent_id, child)).status_code == 404
    assert _preview(test_client, env, "SELECT 1 AS one").status_code == 404
    # A child id is never reachable through another (own) parent.
    assert test_client.get(_url(env.other_parent_id, child)).status_code == 404
    assert (
        test_client.put(
            _url(env.other_parent_id, child), json={"name": "x"}
        ).status_code
        == 404
    )
    assert test_client.delete(_url(env.other_parent_id, child)).status_code == 404
    db.session.expire_all()
    stored = db.session.get(Subreport, subreport.id)
    assert (stored.name, stored.parent_schedule_id) == ("Orders", env.parent_id)


def test_payload_cannot_reassign_parent(
    env: SubreportEnv, test_client: FlaskClient
) -> None:
    subreport = _create(env)
    _login(test_client, ADMIN_USERNAME)
    rv = test_client.post(
        _url(env.parent_id),
        json=_definition(env, parent_schedule_id=env.other_parent_id),
    )
    assert rv.status_code == 400
    rv = test_client.put(
        _url(env.parent_id, str(subreport.id)),
        json={"parent_schedule_id": env.other_parent_id},
    )
    assert rv.status_code == 400
    db.session.expire_all()
    assert db.session.get(Subreport, subreport.id).parent_schedule_id == env.parent_id


def test_datasource_and_database_denial(
    env: SubreportEnv, test_client: FlaskClient
) -> None:
    _login(test_client, NODATA)
    assert (
        test_client.post(_url(env.parent_id), json=_definition(env)).status_code == 403
    )
    rv = _preview(
        test_client,
        env,
        IN_CUSTOMERS,
        param_mapping=CUSTOMER_MAPPING,
    )
    assert rv.status_code == 403

    # The owner can query ORDERS but has no access to the SECRETS dataset.
    _login(test_client, OWNER)
    secret_sql = f"SELECT secret FROM {SECRETS}"  # noqa: S608
    rv = test_client.post(
        _url(env.parent_id),
        json=_definition(env, sql_query=secret_sql, param_mapping={}),
    )
    assert rv.status_code == 403
    assert _preview(test_client, env, secret_sql).status_code == 403
    # A table the user can read does not unlock a joined one.
    rv = _preview(
        test_client,
        env,
        f"SELECT o.customer_id FROM {ORDERS} o JOIN {SECRETS} s "  # noqa: S608
        "ON o.customer_id = s.id",
    )
    assert rv.status_code == 403

    rv = test_client.post(
        _url(env.parent_id), json=_definition(env, database_id=999_999)
    )
    assert rv.status_code in (400, 422)
    assert "database_id" in json.dumps(rv.json)
    assert _subreport_count() == 0


# --------------------------------------------------------------------------- #
# SQL validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "sql",
    [
        f"DELETE FROM {ORDERS}",
        f"UPDATE {ORDERS} SET amount = 0",
        f"INSERT INTO {ORDERS} VALUES (9, 'x', 9)",
        f"DROP TABLE {ORDERS}",
        f"SELECT 1 AS one; DELETE FROM {ORDERS}",
        f"SELECT * INTO sr_it_copy FROM {ORDERS}",
        f"WITH d AS (DELETE FROM {ORDERS} RETURNING *) SELECT * FROM d",
        f"PRAGMA table_info({ORDERS})",
        "SELECT {{ current_username() }} AS who",
        f"SELEC customer_id FROM {ORDERS}",
        f"SELECT customer_id FROM {ORDERS} WHERE",
    ],
)
def test_non_select_sql_is_rejected(
    env: SubreportEnv, test_client: FlaskClient, sql: str
) -> None:
    _login(test_client, ADMIN_USERNAME)
    rv = test_client.post(
        _url(env.parent_id), json=_definition(env, sql_query=sql, param_mapping={})
    )
    assert rv.status_code in (400, 422), rv.json
    assert "sql_query" in json.dumps(rv.json)
    assert _preview(test_client, env, sql).status_code in (400, 422)
    assert _subreport_count() == 0
    df = get_example_database().get_df(f"SELECT COUNT(*) AS n FROM {ORDERS}")  # noqa: S608
    assert df["n"][0] == 4


@pytest.mark.parametrize(
    "sql",
    [
        f"WITH east AS (SELECT * FROM {ORDERS} WHERE region = 'east') "  # noqa: S608
        "SELECT customer_id FROM east",
        f"SELECT customer_id FROM {ORDERS} WHERE customer_id = 1 "  # noqa: S608
        f"UNION SELECT customer_id FROM {ORDERS} WHERE customer_id = 2",
    ],
)
def test_read_only_cte_and_union_are_accepted(
    env: SubreportEnv, test_client: FlaskClient, sql: str
) -> None:
    _login(test_client, ADMIN_USERNAME)
    rv = test_client.post(
        _url(env.parent_id), json=_definition(env, sql_query=sql, param_mapping={})
    )
    assert rv.status_code == 201, rv.json
    rv = _preview(test_client, env, sql)
    assert rv.status_code == 200, rv.json
    assert rv.json["result"]["row_count"] == 2


# --------------------------------------------------------------------------- #
# Parameters and injection
# --------------------------------------------------------------------------- #
REGION_SQL = (
    f"SELECT customer_id FROM {ORDERS} WHERE region = :region "  # noqa: S608
    "ORDER BY customer_id"
)
REGION_MAPPING = {"region": "$F{region}"}


@pytest.mark.parametrize(
    "value",
    [
        "east' OR '1'='1",
        f"east'; DROP TABLE {ORDERS}; --",
        "{{ 1 + 1 }}",
        "east\\' OR 1=1 --",
    ],
)
def test_parameter_values_are_bound_as_literals(
    env: SubreportEnv, test_client: FlaskClient, value: str
) -> None:
    _login(test_client, ADMIN_USERNAME)
    rv = _preview(
        test_client,
        env,
        REGION_SQL,
        param_mapping=REGION_MAPPING,
        values={"region": value},
    )
    assert rv.status_code == 200, rv.json
    assert rv.json["result"]["row_count"] == 0
    df = get_example_database().get_df(f"SELECT COUNT(*) AS n FROM {ORDERS}")  # noqa: S608
    assert df["n"][0] == 4


@pytest.mark.parametrize(
    ("sql", "mapping"),
    [
        (f"SELECT * FROM {ORDERS} WHERE region = :region", {}),
        (f"SELECT * FROM {ORDERS} WHERE region = :region", {"region": "east"}),
        (
            f"SELECT * FROM {ORDERS} WHERE region = :region",
            {"region": "east' OR '1'='1"},
        ),
        (f"SELECT * FROM {ORDERS} WHERE region = :region", {"region": "$F{secret}"}),
        ("SELECT * FROM :region", REGION_MAPPING),
        (f"SELECT :region FROM {ORDERS} LIMIT :region", REGION_MAPPING),
    ],
)
def test_invalid_parameter_mapping_is_rejected(
    env: SubreportEnv,
    test_client: FlaskClient,
    sql: str,
    mapping: dict[str, str],
) -> None:
    _login(test_client, ADMIN_USERNAME)
    rv = test_client.post(
        _url(env.parent_id), json=_definition(env, sql_query=sql, param_mapping=mapping)
    )
    assert rv.status_code in (400, 422), rv.json
    rv = _preview(
        test_client, env, sql, param_mapping=mapping, values={"region": "east"}
    )
    assert rv.status_code in (400, 422), rv.json
    assert _subreport_count() == 0


# --------------------------------------------------------------------------- #
# Parent context and cardinality
# --------------------------------------------------------------------------- #
def _customer_ids(response: Any) -> list[int]:
    return [row["customer_id"] for row in response.json["result"]["data"]]


def test_native_filter_context_and_cardinality(
    env: SubreportEnv, test_client: FlaskClient
) -> None:
    _login(test_client, ADMIN_USERNAME)
    # IN (...) expands every native filter value safely.
    rv = _preview(test_client, env, IN_CUSTOMERS, param_mapping=CUSTOMER_MAPPING)
    assert rv.status_code == 200, rv.json
    assert _customer_ids(rv) == [1, 2, 3]

    # Scalar comparison with several values is a validation error, never an
    # arbitrary pick.
    equality = f"SELECT customer_id FROM {ORDERS} WHERE customer_id = :customer_id"  # noqa: S608
    rv = _preview(test_client, env, equality, param_mapping=CUSTOMER_MAPPING)
    assert rv.status_code == 422
    assert "customer_id" in rv.json["message"]

    # Single-valued native filter binds to a scalar comparison.
    rv = _preview(test_client, env, REGION_SQL, param_mapping=REGION_MAPPING)
    assert rv.status_code == 200, rv.json
    assert _customer_ids(rv) == [1, 2]

    # Explicit preview values override the parent's filter values.
    rv = _preview(
        test_client,
        env,
        equality,
        param_mapping=CUSTOMER_MAPPING,
        values={"customer_id": 4},
    )
    assert rv.status_code == 200, rv.json
    assert _customer_ids(rv) == [4]

    # No value at all.
    rv = _preview(
        test_client,
        env,
        equality,
        param_mapping=CUSTOMER_MAPPING,
        values={"customer_id": []},
    )
    assert rv.status_code == 422
    assert "customer_id" in rv.json["message"]


def test_ambiguous_native_filters_are_rejected(
    env: SubreportEnv, test_client: FlaskClient
) -> None:
    _login(test_client, ADMIN_USERNAME)
    rv = _preview(
        test_client,
        env,
        REGION_SQL,
        param_mapping=REGION_MAPPING,
        parent_id=env.ambiguous_parent_id,
    )
    assert rv.status_code == 422
    assert "ambiguous" in rv.json["message"]


def test_preview_routes_and_row_limits(
    env: SubreportEnv, test_client: FlaskClient
) -> None:
    _login(test_client, ADMIN_USERNAME)
    body = {
        "database_id": env.database_id,
        "sql_query": f"SELECT customer_id FROM {ORDERS} ORDER BY customer_id",  # noqa: S608
    }
    rv = test_client.post("/api/v1/subreport/execute_preview", json=body)
    assert rv.status_code == 400
    rv = test_client.post(
        "/api/v1/subreport/execute_preview",
        json={**body, "parent_schedule_id": env.parent_id, "row_limit": 2},
    )
    assert rv.status_code == 200, rv.json
    assert rv.json["result"] == {
        "columns": ["customer_id"],
        "data": [{"customer_id": 1}, {"customer_id": 2}],
        "row_count": 2,
        "truncated": True,
    }
    with patch.dict(app.config, {"ALERT_REPORTS_SUBREPORT_ROW_LIMIT": 3}):
        rv = test_client.post(
            "/api/v1/subreport/execute_preview",
            json={**body, "parent_schedule_id": env.parent_id, "row_limit": 1000},
        )
    assert rv.status_code == 200, rv.json
    assert rv.json["result"]["row_count"] == 3
    assert rv.json["result"]["truncated"] is True


# --------------------------------------------------------------------------- #
# RLS and denylists
# --------------------------------------------------------------------------- #
ALL_ORDERS = f"SELECT customer_id FROM {ORDERS} ORDER BY customer_id"  # noqa: S608


@pytest.mark.parametrize(
    ("username", "expected"),
    [(OWNER, [1, 2]), (WEST, [3, 4]), (ADMIN_USERNAME, [1, 2, 3, 4])],
)
def test_rls_is_applied_per_user(
    env: SubreportEnv, test_client: FlaskClient, username: str, expected: list[int]
) -> None:
    _login(test_client, username)
    with patch.dict(app.config, {"RLS_IN_SQLLAB": False}):
        rv = _preview(test_client, env, ALL_ORDERS)
        assert rv.status_code == 200, rv.json
        assert _customer_ids(rv) == expected
        rv = _preview(
            test_client,
            env,
            f"WITH x AS (SELECT * FROM {ORDERS}) "  # noqa: S608
            "SELECT customer_id FROM x ORDER BY customer_id",
        )
        assert rv.status_code == 200, rv.json
        assert _customer_ids(rv) == expected


def test_denylisted_functions_and_tables(
    env: SubreportEnv, test_client: FlaskClient
) -> None:
    _login(test_client, ADMIN_USERNAME)
    engine = get_example_database().db_engine_spec.engine
    function_sql = "SELECT ABS(-1) AS v"
    with patch.dict(app.config, {"DISALLOWED_SQL_FUNCTIONS": {engine: {"abs"}}}):
        rv = _preview(test_client, env, function_sql)
        assert rv.status_code == 403
        rv = test_client.post(
            _url(env.parent_id),
            json=_definition(env, sql_query=function_sql, param_mapping={}),
        )
        assert rv.status_code == 403

    with patch.dict(app.config, {"DISALLOWED_SQL_TABLES": {engine: {SECRETS}}}):
        for sql in (
            f"SELECT secret FROM {SECRETS}",  # noqa: S608
            f"SELECT customer_id FROM {ORDERS} "  # noqa: S608
            f"UNION SELECT id FROM {SECRETS}",
            f"WITH s AS (SELECT * FROM {SECRETS}) SELECT * FROM s",  # noqa: S608
        ):
            rv = _preview(test_client, env, sql)
            assert rv.status_code == 403, (sql, rv.json)
    assert _subreport_count() == 0


# --------------------------------------------------------------------------- #
# Nested schedules
# --------------------------------------------------------------------------- #
def test_nested_schedule_cycles_are_rejected(
    env: SubreportEnv, test_client: FlaskClient
) -> None:
    admin = _user(ADMIN_USERNAME)
    dashboard = db.session.get(Dashboard, env.dashboard_id)
    child = _insert_parent("child", [admin], dashboard, [])
    grandchild = _insert_parent("grandchild", [admin], dashboard, [])
    alert = _insert_parent(
        "alert", [admin], dashboard, [], report_type=ReportScheduleType.ALERT
    )
    child_id, grandchild_id, alert_id = child.id, grandchild.id, alert.id
    _login(test_client, ADMIN_USERNAME)

    def put(schedule_id: int, parent_id: int | None) -> Any:
        return test_client.put(
            f"/api/v1/report/{schedule_id}", json={"parent_schedule_id": parent_id}
        )

    assert put(child_id, env.parent_id).status_code == 200
    assert put(grandchild_id, child_id).status_code == 200
    for schedule_id, parent_id in (
        (env.parent_id, env.parent_id),
        (env.parent_id, child_id),
        (env.parent_id, grandchild_id),
        (child_id, grandchild_id),
        (child_id, alert_id),
    ):
        rv = put(schedule_id, parent_id)
        assert rv.status_code == 400, (schedule_id, parent_id, rv.json)
        assert "parent_schedule_id" in rv.json["message"]

    # A non-editor of the parent cannot attach their schedule to it.
    _login(test_client, OTHER)
    rv = put(env.other_parent_id, env.parent_id)
    assert rv.status_code == 400
    assert "parent_schedule_id" in rv.json["message"]

    db.session.expire_all()
    assert db.session.get(ReportSchedule, env.parent_id).parent_schedule_id is None
    assert db.session.get(ReportSchedule, child_id).parent_schedule_id == env.parent_id
    assert db.session.get(ReportSchedule, grandchild_id).parent_schedule_id == child_id
    other_parent = db.session.get(ReportSchedule, env.other_parent_id)
    assert other_parent.parent_schedule_id is None


@patch("superset.tasks.scheduler.execute.apply_async")
def test_scheduler_never_enqueues_child_schedules(
    apply_async: MagicMock, env: SubreportEnv
) -> None:
    dashboard = db.session.get(Dashboard, env.dashboard_id)
    child = _insert_parent("child", [_user(ADMIN_USERNAME)], dashboard, [])
    child.parent_schedule_id = env.parent_id
    db.session.commit()
    with freeze_time("2020-01-01T09:00:00Z"):
        scheduler()
    enqueued = {call.args[0][0] for call in apply_async.call_args_list}
    assert env.parent_id in enqueued
    assert child.id not in enqueued


# --------------------------------------------------------------------------- #
# Scheduled execution
# --------------------------------------------------------------------------- #
def _run_report(schedule_id: int) -> None:
    with freeze_time("2020-01-01T00:00:00Z"):
        AsyncExecuteReportScheduleCommand(TEST_ID, schedule_id, datetime.utcnow()).run()


def _last_log_state(schedule_id: int) -> str:
    log = (
        db.session.query(ReportExecutionLog)
        .filter_by(report_schedule_id=schedule_id)
        .order_by(ReportExecutionLog.id.desc())
        .first()
    )
    assert log is not None
    return log.state


@pytest.mark.parametrize("operation", ["create", "update", "nested"])
@patch("superset.reports.notifications.email.send_email_smtp")
@patch("superset.utils.screenshots.DashboardScreenshot.get_screenshot")
def test_composed_sql_uses_current_editor_rls(
    screenshot_mock: MagicMock,
    email_mock: MagicMock,
    env: SubreportEnv,
    test_client: FlaskClient,
    operation: str,
) -> None:
    screenshot_mock.return_value = SCREENSHOT_FILE
    parent = db.session.get(ReportSchedule, env.parent_id)
    parent.created_by = parent.changed_by = _user(OWNER)
    existing = _create(env) if operation == "update" else None
    db.session.commit()
    _login(test_client, WEST)
    if operation == "create":
        response = test_client.post(_url(parent.id), json=_definition(env))
    elif operation == "update":
        assert existing is not None
        response = test_client.put(
            _url(parent.id, str(existing.id)), json={"sql_query": IN_CUSTOMERS}
        )
    else:
        response = test_client.put(
            f"/api/v1/report/{parent.id}", json={"subreports": [_definition(env)]}
        )
    assert response.status_code in (200, 201), response.json
    db.session.expire_all()
    assert db.session.get(ReportSchedule, env.parent_id).changed_by_fk == _user(WEST).id
    _run_report(env.parent_id)
    assert _last_log_state(env.parent_id) == ReportState.SUCCESS
    body = email_mock.call_args[0][2]
    assert "<td>west</td>" in body
    assert "<td>east</td>" not in body


@pytest.mark.parametrize("operation", ["attach", "edit", "detach", "delete"])
def test_child_schedule_writes_change_root_editor(
    env: SubreportEnv, test_client: FlaskClient, operation: str
) -> None:
    parent = db.session.get(ReportSchedule, env.parent_id)
    dashboard = db.session.get(Dashboard, env.dashboard_id)
    child = _insert_parent("editor_child", [_user(OWNER), _user(WEST)], dashboard, [])
    if operation != "attach":
        child.parent_schedule_id = parent.id
    parent.created_by = parent.changed_by = _user(OWNER)
    db.session.commit()
    _login(test_client, WEST)
    if operation == "delete":
        response = test_client.delete(f"/api/v1/report/?q=!({child.id})")
    else:
        changes = {
            "attach": {"parent_schedule_id": parent.id},
            "edit": {"name": f"{PREFIX}edited_child"},
            "detach": {"parent_schedule_id": None},
        }
        response = test_client.put(
            f"/api/v1/report/{child.id}", json=changes[operation]
        )
    assert response.status_code == 200, response.json
    db.session.expire_all()
    assert db.session.get(ReportSchedule, env.parent_id).changed_by_fk == _user(WEST).id


@pytest.mark.parametrize("operation", ["edit", "detach", "delete"])
def test_child_editor_cannot_change_another_editors_composition(
    env: SubreportEnv, test_client: FlaskClient, operation: str
) -> None:
    parent = db.session.get(ReportSchedule, env.parent_id)
    dashboard = db.session.get(Dashboard, env.dashboard_id)
    child = _insert_parent(
        "unshared_child", [_user(OTHER), _user(OWNER)], dashboard, []
    )
    child.parent_schedule_id = parent.id
    db.session.commit()
    _login(test_client, OTHER)
    if operation == "delete":
        response = test_client.delete(f"/api/v1/report/?q=!({child.id})")
    else:
        changes: dict[str, Any] = (
            {"name": f"{PREFIX}denied_child"}
            if operation == "edit"
            else {"parent_schedule_id": None}
        )
        response = test_client.put(f"/api/v1/report/{child.id}", json=changes)
    assert response.status_code in (403, 404), response.json
    db.session.expire_all()
    assert db.session.get(ReportSchedule, child.id).parent_schedule_id == env.parent_id


@patch("superset.reports.notifications.email.send_email_smtp")
@patch("superset.utils.screenshots.DashboardScreenshot.get_screenshot")
def test_scheduled_report_composes_subreports_and_children(
    screenshot_mock: MagicMock, email_mock: MagicMock, env: SubreportEnv
) -> None:
    screenshot_mock.return_value = SCREENSHOT_FILE
    owner = _user(OWNER)
    dashboard = db.session.get(Dashboard, env.dashboard_id)
    # The owner is the executor, so the owner's RLS (east only) applies.
    parent = _insert_parent("executed", [owner], dashboard, [CUSTOMER_FILTER])
    child = _insert_parent("executed_child", [owner], dashboard, [])
    child.parent_schedule_id = parent.id
    db.session.commit()
    _create(env, parent_schedule_id=parent.id, name="Orders", position=0)
    _create(
        env,
        parent_schedule_id=parent.id,
        name="By region",
        position=1,
        sql_query=f"SELECT region, SUM(amount) AS amount FROM {ORDERS} "  # noqa: S608
        "WHERE customer_id IN (:customer_id) GROUP BY region",
        viz_type="chart",
        template={"chart_type": "bar", "x_column": "region", "y_columns": ["amount"]},
    )
    _create(
        env,
        parent_schedule_id=child.id,
        name="Totals",
        sql_query=f"SELECT SUM(amount) AS total FROM {ORDERS} "  # noqa: S608
        "WHERE customer_id IN (:customer_id)",
    )
    parent_id, child_id = parent.id, child.id

    _run_report(parent_id)

    assert _last_log_state(parent_id) == ReportState.SUCCESS
    email_mock.assert_called_once()
    body = email_mock.call_args[0][2]
    for heading in ("Orders", "By region", f"{PREFIX}executed_child", "Totals"):
        assert heading in body
    assert body.index("<h3>Orders</h3>") < body.index("<h3>By region</h3>")
    assert body.index("<h3>By region</h3>") < body.index(
        f"<h3>{PREFIX}executed_child</h3>"
    )
    data = email_mock.call_args[1]["data"]
    csv_names = sorted(data)
    orders_csv = pd.read_csv(BytesIO(data[csv_names[0]]))
    assert orders_csv["customer_id"].tolist() == [1, 2]
    totals_csv = pd.read_csv(BytesIO(data[csv_names[-1]]))
    assert totals_csv["total"].tolist() == [30]
    # Parent screenshot, subreport chart and the composed child snapshot.
    assert len(email_mock.call_args[1]["images"]) == 3
    # The child is composed into its parent, not delivered separately.
    assert (
        db.session.query(ReportExecutionLog)
        .filter_by(report_schedule_id=child_id)
        .count()
        == 0
    )


@patch("superset.reports.notifications.email.send_email_smtp")
@patch("superset.utils.screenshots.DashboardScreenshot.get_screenshot")
def test_scheduled_report_fails_closed_on_subreport_error(
    screenshot_mock: MagicMock, email_mock: MagicMock, env: SubreportEnv
) -> None:
    screenshot_mock.return_value = SCREENSHOT_FILE
    _create(
        env,
        sql_query=f"SELECT customer_id FROM {ORDERS} "  # noqa: S608
        "WHERE customer_id = :customer_id",
    )
    with pytest.raises(ReportScheduleSubreportFailedError) as excinfo:
        _run_report(env.parent_id)
    assert "accepts exactly one" in str(excinfo.value.message)
    assert _last_log_state(env.parent_id) == ReportState.ERROR
    for call in email_mock.call_args_list:
        # Only failure notifications to owners may be sent, never partial data.
        assert "<h3>Orders</h3>" not in call.args[2]


@patch("superset.reports.notifications.email.send_email_smtp")
@patch("superset.utils.screenshots.DashboardScreenshot.get_screenshot")
def test_report_without_subreports_is_unchanged(
    screenshot_mock: MagicMock, email_mock: MagicMock, env: SubreportEnv
) -> None:
    screenshot_mock.return_value = SCREENSHOT_FILE
    _run_report(env.other_parent_id)
    assert _last_log_state(env.other_parent_id) == ReportState.SUCCESS
    email_mock.assert_called_once()
    assert 'class="subreport"' not in email_mock.call_args[0][2]
    assert not email_mock.call_args[1]["data"]
    assert len(email_mock.call_args[1]["images"]) == 1
