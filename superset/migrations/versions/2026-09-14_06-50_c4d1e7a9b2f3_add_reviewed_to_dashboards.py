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
"""Add a reviewed column to dashboards.

Adds a nullable free-text ``reviewed`` column to ``dashboards`` holding
reviewer notes shown in the Dashboards list.

Revision ID: c4d1e7a9b2f3
Revises: 7e2c9a4f1b83
Create Date: 2026-09-14 06:50:00.000000

"""

import sqlalchemy as sa

from superset.migrations.shared.utils import add_columns, drop_columns

# revision identifiers, used by Alembic.
revision: str = "c4d1e7a9b2f3"
down_revision: str = "7e2c9a4f1b83"


def upgrade() -> None:
    """Add the nullable ``reviewed`` column to ``dashboards``."""
    add_columns("dashboards", sa.Column("reviewed", sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop the ``reviewed`` column from ``dashboards``."""
    drop_columns("dashboards", "reviewed")
