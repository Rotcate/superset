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
from datetime import datetime, timezone
from unittest.mock import Mock

from pytest_mock import MockerFixture

from superset.reports.models import ReportScheduleType


def test_scheduler_skips_composed_child_schedules(mocker: MockerFixture) -> None:
    from superset.tasks import scheduler as scheduler_module

    def schedule(schedule_id: int, parent_id: int | None) -> Mock:
        item = Mock()
        item.id = schedule_id
        item.name = f"report {schedule_id}"
        item.parent_schedule_id = parent_id
        item.type = ReportScheduleType.REPORT
        item.working_timeout = None
        return item

    mocker.patch.object(scheduler_module, "is_feature_enabled", return_value=True)
    mocker.patch.object(
        scheduler_module.ReportScheduleDAO,
        "find_active",
        return_value=[schedule(1, None), schedule(2, 1), schedule(3, None)],
    )
    mocker.patch.object(
        scheduler_module,
        "cron_schedule_window",
        return_value=[datetime(2024, 1, 1, tzinfo=timezone.utc)],
    )
    apply_async = mocker.patch.object(scheduler_module.execute, "apply_async")

    scheduler_module.scheduler()

    assert [call.args[0] for call in apply_async.call_args_list] == [(1,), (3,)]
