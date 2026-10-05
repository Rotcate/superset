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
"""Add report_subreport table and report_schedule.parent_schedule_id

Revision ID: 4c1d8e2f9a07
Revises: 7e2c9a4f1b83
Create Date: 2026-10-05 12:00:00.000000

"""

import sqlalchemy as sa
from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy_utils import UUIDType

from superset.migrations.shared.utils import (
    add_columns,
    create_fks_for_table,
    create_index,
    create_table,
    drop_columns,
    drop_fks_for_table,
    drop_index,
    drop_table,
)

# revision identifiers, used by Alembic.
revision = "4c1d8e2f9a07"
down_revision = "7e2c9a4f1b83"

SUBREPORT_TABLE = "report_subreport"
REPORT_SCHEDULE_TABLE = "report_schedule"
PARENT_FK_NAME = "fk_report_schedule_parent_schedule_id_report_schedule"
PARENT_INDEX_NAME = "ix_report_schedule_parent_schedule_id"
SUBREPORT_PARENT_INDEX_NAME = "ix_report_subreport_parent_schedule_id"


def upgrade():
    """
    Create ``report_subreport`` (ordered SQL subreports owned by a schedule) and
    add the nullable self-referencing ``report_schedule.parent_schedule_id``.
    """
    create_table(
        SUBREPORT_TABLE,
        Column("id", Integer, primary_key=True),
        Column("uuid", UUIDType(binary=True), nullable=True),
        Column(
            "parent_schedule_id",
            Integer,
            sa.ForeignKey(
                f"{REPORT_SCHEDULE_TABLE}.id",
                name="fk_report_subreport_parent_schedule_id_report_schedule",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        Column("name", String(150), nullable=False),
        Column("sql_query", Text, nullable=False),
        Column(
            "database_id",
            Integer,
            sa.ForeignKey("dbs.id", name="fk_report_subreport_database_id_dbs"),
            nullable=False,
        ),
        Column("param_mapping", sa.JSON(), nullable=True),
        Column("position", Integer, nullable=False, server_default="0"),
        Column("viz_type", String(50), nullable=False, server_default="table"),
        Column("template", sa.JSON(), nullable=True),
        # AuditMixinNullable columns
        Column("created_on", DateTime, nullable=True),
        Column("changed_on", DateTime, nullable=True),
        Column(
            "created_by_fk",
            Integer,
            sa.ForeignKey(
                "ab_user.id", name="fk_report_subreport_created_by_fk_ab_user"
            ),
            nullable=True,
        ),
        Column(
            "changed_by_fk",
            Integer,
            sa.ForeignKey(
                "ab_user.id", name="fk_report_subreport_changed_by_fk_ab_user"
            ),
            nullable=True,
        ),
        sa.UniqueConstraint("uuid", name="uq_report_subreport_uuid"),
    )
    create_index(SUBREPORT_TABLE, SUBREPORT_PARENT_INDEX_NAME, ["parent_schedule_id"])

    add_columns(
        REPORT_SCHEDULE_TABLE,
        Column("parent_schedule_id", Integer, nullable=True),
    )
    create_index(REPORT_SCHEDULE_TABLE, PARENT_INDEX_NAME, ["parent_schedule_id"])
    create_fks_for_table(
        foreign_key_name=PARENT_FK_NAME,
        table_name=REPORT_SCHEDULE_TABLE,
        referenced_table=REPORT_SCHEDULE_TABLE,
        local_cols=["parent_schedule_id"],
        remote_cols=["id"],
        ondelete="SET NULL",
    )


def downgrade():
    """Drop ``report_subreport`` and ``report_schedule.parent_schedule_id``."""
    drop_fks_for_table(REPORT_SCHEDULE_TABLE, [PARENT_FK_NAME])
    drop_index(REPORT_SCHEDULE_TABLE, PARENT_INDEX_NAME)
    drop_columns(REPORT_SCHEDULE_TABLE, "parent_schedule_id")

    drop_index(SUBREPORT_TABLE, SUBREPORT_PARENT_INDEX_NAME)
    drop_table(SUBREPORT_TABLE)
