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
Migration 4c1d8e2f9a07 (report subreports): the metadata database is at the
migrated shape, and an upgrade/downgrade/upgrade round trip on a file-backed
SQLite database preserves existing report schedules.
"""

from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect

from superset import db

migration: ModuleType = import_module(
    "superset.migrations.versions.2026-10-05_12-00_4c1d8e2f9a07_add_report_subreports"
)


def _columns(engine: sa.engine.Engine, table: str) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns(table)}


def _run(engine: sa.engine.Engine, fn: Any) -> None:
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            fn()


def test_metadata_database_is_migrated(app_context: Any) -> None:
    engine = db.engine
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
    parent_fks = [
        fk
        for fk in inspect(engine).get_foreign_keys("report_schedule")
        if fk["constrained_columns"] == ["parent_schedule_id"]
    ]
    assert parent_fks
    assert parent_fks[0]["referred_table"] == "report_schedule"


def test_round_trip_preserves_report_schedules(tmp_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'subreports.db'}")
    metadata = sa.MetaData()
    sa.Table(
        "report_schedule",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(150)),
    )
    sa.Table("dbs", metadata, sa.Column("id", sa.Integer, primary_key=True))
    sa.Table("ab_user", metadata, sa.Column("id", sa.Integer, primary_key=True))
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO report_schedule (id, name) VALUES (1, 'a'), (2, 'b')")
        )
        connection.execute(sa.text("INSERT INTO dbs (id) VALUES (1)"))

    _run(engine, migration.upgrade)
    with engine.begin() as connection:
        connection.execute(
            sa.text("UPDATE report_schedule SET parent_schedule_id = 1 WHERE id = 2")
        )
        connection.execute(
            sa.text(
                "INSERT INTO report_subreport "
                "(parent_schedule_id, name, sql_query, database_id) "
                "VALUES (1, 's', 'SELECT 1', 1)"
            )
        )
        rows = connection.execute(
            sa.text("SELECT position, viz_type FROM report_subreport")
        ).fetchall()
    assert [tuple(row) for row in rows] == [(0, "table")]

    _run(engine, migration.downgrade)
    assert migration.SUBREPORT_TABLE not in inspect(engine).get_table_names()
    assert _columns(engine, "report_schedule") == {"id", "name"}
    with engine.connect() as connection:
        rows = connection.execute(
            sa.text("SELECT id, name FROM report_schedule ORDER BY id")
        ).fetchall()
    assert [tuple(row) for row in rows] == [(1, "a"), (2, "b")]

    _run(engine, migration.upgrade)
    assert "parent_schedule_id" in _columns(engine, "report_schedule")
