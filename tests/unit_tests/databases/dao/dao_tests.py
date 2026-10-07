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

from collections.abc import Iterator

import pytest
from sqlalchemy.orm.session import Session


@pytest.fixture
def session_with_data(session: Session) -> Iterator[Session]:
    from superset.connectors.sqla.models import SqlaTable
    from superset.databases.ssh_tunnel.models import SSHTunnel
    from superset.models.core import Database

    engine = session.get_bind()
    SqlaTable.metadata.create_all(engine)  # pylint: disable=no-member

    database = Database(database_name="my_database", sqlalchemy_uri="sqlite://")
    sqla_table = SqlaTable(
        table_name="my_sqla_table",
        columns=[],
        metrics=[],
        database=database,
    )
    ssh_tunnel = SSHTunnel(
        database_id=database.id,
        database=database,
    )

    session.add(database)
    session.add(sqla_table)
    session.add(ssh_tunnel)
    session.flush()
    yield session
    session.rollback()


def test_database_get_ssh_tunnel(session_with_data: Session) -> None:
    from superset.daos.database import DatabaseDAO
    from superset.databases.ssh_tunnel.models import SSHTunnel

    database = DatabaseDAO.find_by_id(1, skip_base_filter=True)
    assert database is not None
    result = database.ssh_tunnel

    assert result
    assert isinstance(result, SSHTunnel)
    assert 1 == result.database_id


def test_database_get_ssh_tunnel_not_found(session_with_data: Session) -> None:
    from superset.daos.database import DatabaseDAO

    database = DatabaseDAO.find_by_id(2, skip_base_filter=True)
    result = database.ssh_tunnel if database else None

    assert result is None


def test_get_related_objects_preloads_access_check_relationships(
    session: Session,
) -> None:
    """
    ``related_objects`` runs ``can_access_chart`` / ``can_access_dashboard``
    on every result, so the relationships those checks read must already be
    loaded: touching them must not issue one query per chart or dashboard.
    """
    from unittest.mock import patch

    from sqlalchemy import event

    from superset.connectors.sqla.models import SqlaTable
    from superset.daos.database import DatabaseDAO
    from superset.models.core import Database
    from superset.models.dashboard import Dashboard
    from superset.models.slice import Slice

    engine = session.get_bind()
    Dashboard.metadata.create_all(engine)  # pylint: disable=no-member

    database = Database(database_name="related_db", sqlalchemy_uri="sqlite://")
    other_database = Database(database_name="other_db", sqlalchemy_uri="sqlite://")
    datasets = [
        SqlaTable(table_name=f"related_table_{i}", database=database) for i in range(2)
    ]
    other_dataset = SqlaTable(table_name="other_table", database=other_database)
    session.add_all([database, other_database, *datasets, other_dataset])
    session.flush()

    charts = [
        Slice(
            slice_name=f"chart_{i}",
            datasource_id=datasets[i % 2].id,
            datasource_type="table",
        )
        for i in range(4)
    ]
    other_chart = Slice(
        slice_name="other_chart",
        datasource_id=other_dataset.id,
        datasource_type="table",
    )
    dashboards = [
        Dashboard(dashboard_title="dash_0", slices=[charts[0], charts[1]]),
        Dashboard(dashboard_title="dash_1", slices=[charts[2], other_chart]),
        Dashboard(dashboard_title="unrelated", slices=[other_chart]),
    ]
    session.add_all([*charts, other_chart, *dashboards])
    session.flush()
    session.expire_all()

    with patch.object(DatabaseDAO, "find_by_id", return_value=database):
        result = DatabaseDAO.get_related_objects(database.id)

    assert sorted(chart.slice_name for chart in result["charts"]) == [
        "chart_0",
        "chart_1",
        "chart_2",
        "chart_3",
    ]
    assert sorted(dash.dashboard_title for dash in result["dashboards"]) == [
        "dash_0",
        "dash_1",
    ]

    statements: list[str] = []

    def record(*args: object) -> None:
        statements.append(str(args[2]))

    event.listen(engine, "before_cursor_execute", record)
    try:
        for chart in result["charts"]:
            list(chart.editors)
            list(chart.viewers)
            assert chart.resolved_datasource is not None
            list(chart.resolved_datasource.editors)
        for dashboard in result["dashboards"]:
            list(dashboard.editors)
            list(dashboard.viewers)
            for slc in dashboard.slices:
                assert slc.resolved_datasource is not None
                list(slc.resolved_datasource.editors)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert statements == []
