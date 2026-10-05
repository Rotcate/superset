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
"""Tests for the report subreports migration (SQLite round trip)."""

from importlib import import_module
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from pytest_mock import MockerFixture
from sqlalchemy import Column, create_engine, inspect, Integer, MetaData, Table
from sqlalchemy.engine import Engine

migration: ModuleType = import_module(
    "superset.migrations.versions.2026-10-05_12-00_4c1d8e2f9a07_add_report_subreports"
)


@pytest.fixture
def engine() -> Engine:
    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    for name in ("report_schedule", "dbs", "ab_user"):
        Table(name, metadata, Column("id", Integer, primary_key=True))
    metadata.create_all(engine)
    return engine


def _columns(engine: Engine, table: str) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns(table)}


def test_upgrade_creates_subreports_and_parent(engine: Engine) -> None:
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()

    assert migration.SUBREPORT_TABLE in inspect(engine).get_table_names()
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
    } <= _columns(engine, migration.SUBREPORT_TABLE)
    assert "parent_schedule_id" in _columns(engine, "report_schedule")
    fks = inspect(engine).get_foreign_keys(migration.SUBREPORT_TABLE)
    parent_fk = next(fk for fk in fks if fk["referred_table"] == "report_schedule")
    assert parent_fk["options"].get("ondelete") == "CASCADE"


def test_downgrade_is_reversible(engine: Engine) -> None:
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.downgrade()

    assert migration.SUBREPORT_TABLE not in inspect(engine).get_table_names()
    assert "parent_schedule_id" not in _columns(engine, "report_schedule")


def test_downgrade_keeps_subreport_fk_index_until_table_drop(
    engine: Engine, mocker: MockerFixture
) -> None:
    """Let dropping the child table remove its FK-backed indexes on MySQL."""
    drop_index = mocker.spy(migration, "drop_index")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.downgrade()

    drop_index.assert_called_once_with(
        migration.REPORT_SCHEDULE_TABLE, migration.PARENT_INDEX_NAME
    )
    assert migration.SUBREPORT_TABLE not in inspect(engine).get_table_names()
