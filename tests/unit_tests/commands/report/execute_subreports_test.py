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
from datetime import datetime
from io import BytesIO
from typing import Any
from unittest.mock import Mock
from uuid import uuid4
from zipfile import ZipFile

import pandas as pd
import pytest
from celery.exceptions import SoftTimeLimitExceeded
from pytest_mock import MockerFixture

from superset.commands.report.exceptions import ReportScheduleSubreportFailedError
from superset.commands.report.execute import _inherit_context, BaseReportState
from superset.reports.models import (
    ReportDataFormat,
    ReportSchedule,
    ReportScheduleType,
)
from superset.reports.subreports import (
    SubreportContext,
    SubreportExecutionError,
    SubreportParameterError,
    SubreportResult,
)

EXECUTE = "superset.commands.report.execute"


def _subreport(
    subreport_id: int,
    position: int,
    *,
    name: str | None = None,
    viz_type: str = "table",
    template: dict[str, Any] | None = None,
) -> Mock:
    subreport = Mock()
    subreport.id = subreport_id
    subreport.position = position
    subreport.name = name or f"Sub {subreport_id}"
    subreport.viz_type = viz_type
    subreport.template = template or {}
    return subreport


def _schedule(
    mocker: MockerFixture,
    *,
    schedule_id: int = 1,
    name: str = "Parent",
    report_format: ReportDataFormat = ReportDataFormat.PNG,
    chart: bool = False,
    subreports: list[Mock] | None = None,
    children: list[Mock] | None = None,
    native_filters: list[dict[str, Any]] | None = None,
) -> Mock:
    schedule = mocker.Mock(spec=ReportSchedule)
    schedule.id = schedule_id
    schedule.name = name
    schedule.type = ReportScheduleType.REPORT
    schedule.active = True
    schedule.report_format = report_format
    schedule.description = "desc"
    schedule.email_subject = None
    schedule.force_screenshot = False
    schedule.working_timeout = None
    schedule.include_cta = True
    schedule.recipients = []
    schedule.editors = []
    schedule.subreports = subreports or []
    schedule.children = children or []
    if chart:
        schedule.chart = mocker.Mock()
        schedule.chart.slice_name = "Chart"
        schedule.dashboard = None
        schedule.dashboard_id = None
    else:
        schedule.chart = None
        schedule.dashboard = mocker.Mock()
        schedule.dashboard.dashboard_title = "Dash"
        schedule.dashboard_id = 10
    schedule.extra = {"dashboard": {"nativeFilters": native_filters or []}}
    return schedule


def _state(mocker: MockerFixture, schedule: Mock) -> BaseReportState:
    state = BaseReportState(schedule, datetime(2024, 1, 1), uuid4())
    mocker.patch.object(state, "_get_log_data", return_value={})
    mocker.patch.object(state, "_get_url", return_value="http://superset/report")
    return state


def _result(subreport: Any, context: SubreportContext, **_: Any) -> SubreportResult:
    return SubreportResult(
        subreport_id=subreport.id,
        name=subreport.name,
        position=subreport.position,
        viz_type=subreport.viz_type,
        template=subreport.template,
        df=pd.DataFrame({"month": ["Jan", "Feb"], "value": [1, 2]}),
        truncated=False,
    )


@pytest.fixture
def run_subreport(mocker: MockerFixture) -> Mock:
    return mocker.patch(f"{EXECUTE}.execute_subreport", side_effect=_result)


def test_no_subreports_keeps_existing_behavior(
    mocker: MockerFixture, run_subreport: Mock
) -> None:
    state = _state(mocker, _schedule(mocker, chart=True))
    mocker.patch.object(state, "_get_screenshots", return_value=[b"img"])
    embedded = mocker.patch.object(state, "_get_embedded_data")

    content = state._get_notification_content()

    assert content.screenshots == [b"img"]
    assert content.subreports == []
    assert content.subreports_csv_bundled is False
    run_subreport.assert_not_called()
    embedded.assert_not_called()


def test_subreports_run_once_ordered_by_position_then_id(
    mocker: MockerFixture, run_subreport: Mock
) -> None:
    schedule = _schedule(
        mocker,
        subreports=[_subreport(5, 2), _subreport(9, 1), _subreport(3, 1)],
        native_filters=[{"columnName": "customer_id", "filterValues": [7]}],
    )
    state = _state(mocker, schedule)
    mocker.patch.object(state, "_get_screenshots", return_value=[b"img"])

    content = state._get_notification_content()

    assert [call.args[0].id for call in run_subreport.call_args_list] == [3, 9, 5]
    assert [item.name for item in content.subreports] == ["Sub 3", "Sub 9", "Sub 5"]
    context = run_subreport.call_args.args[1]
    assert context.values["customer_id"] == [7]
    assert run_subreport.call_args.kwargs["timeout_seconds"] > 0


def test_chart_data_is_fetched_once_and_used_as_context(
    mocker: MockerFixture, run_subreport: Mock
) -> None:
    schedule = _schedule(
        mocker,
        chart=True,
        report_format=ReportDataFormat.TEXT,
        subreports=[_subreport(1, 0)],
    )
    state = _state(mocker, schedule)
    chart_df = pd.DataFrame({"customer_id": [1, 2, 2]})
    embedded = mocker.patch.object(state, "_get_embedded_data", return_value=chart_df)

    content = state._get_notification_content()

    embedded.assert_called_once()
    assert content.embedded_data is chart_df
    assert run_subreport.call_args.args[1].values["customer_id"] == [1, 2]


def test_table_and_chart_sections_are_rendered(
    mocker: MockerFixture, run_subreport: Mock
) -> None:
    schedule = _schedule(
        mocker,
        subreports=[
            _subreport(1, 0, template={"columns": ["value"]}),
            _subreport(
                2,
                1,
                viz_type="chart",
                template={
                    "chart_type": "line",
                    "x_column": "month",
                    "y_columns": ["value"],
                },
            ),
        ],
    )
    state = _state(mocker, schedule)
    mocker.patch.object(state, "_get_screenshots", return_value=[b"img"])

    table, chart = state._get_notification_content().subreports

    assert table.kind == "table"
    assert table.data is not None
    assert list(table.data.columns) == ["value"]
    assert chart.kind == "chart"
    assert chart.images[0].startswith(b"\x89PNG")
    assert chart.data is not None


def test_missing_context_fails_clearly(
    mocker: MockerFixture, run_subreport: Mock
) -> None:
    run_subreport.side_effect = SubreportParameterError(
        "No value is available for parent field customer_id."
    )
    state = _state(mocker, _schedule(mocker, subreports=[_subreport(1, 0)]))

    with pytest.raises(ReportScheduleSubreportFailedError) as excinfo:
        state._get_notification_content()

    assert excinfo.value.status == 422
    assert "Sub 1" in excinfo.value.message
    assert "customer_id" in excinfo.value.message


def test_execution_errors_do_not_leak_database_details(
    mocker: MockerFixture, run_subreport: Mock
) -> None:
    run_subreport.side_effect = SubreportExecutionError(
        exception=RuntimeError("password=hunter2 at db.internal")
    )
    state = _state(mocker, _schedule(mocker, subreports=[_subreport(1, 0)]))

    with pytest.raises(ReportScheduleSubreportFailedError) as excinfo:
        state._get_notification_content()

    assert "hunter2" not in str(excinfo.value)
    assert "db.internal" not in excinfo.value.message
    assert excinfo.value.status == 500


def test_celery_soft_timeout_propagates(
    mocker: MockerFixture, run_subreport: Mock
) -> None:
    run_subreport.side_effect = SoftTimeLimitExceeded()
    state = _state(mocker, _schedule(mocker, subreports=[_subreport(1, 0)]))

    with pytest.raises(SoftTimeLimitExceeded):
        state._get_notification_content()


def test_csv_export_bundles_subreports(
    mocker: MockerFixture, run_subreport: Mock
) -> None:
    schedule = _schedule(
        mocker,
        chart=True,
        report_format=ReportDataFormat.CSV,
        subreports=[_subreport(1, 0, name="Orders")],
    )
    state = _state(mocker, schedule)
    mocker.patch.object(
        state, "_get_embedded_data", return_value=pd.DataFrame({"a": [1]})
    )
    mocker.patch.object(state, "_get_data", return_value=b"a\n1\n")

    content = state._get_notification_content()

    assert content.subreports_csv_bundled is True
    assert content.csv is not None
    with ZipFile(BytesIO(content.csv)) as archive:
        assert archive.namelist() == [
            "Parent_Chart.csv",
            "subreports/01_Orders.csv",
        ]


def test_csv_export_without_subreports_is_unchanged(mocker: MockerFixture) -> None:
    schedule = _schedule(mocker, chart=True, report_format=ReportDataFormat.CSV)
    state = _state(mocker, schedule)
    mocker.patch.object(state, "_get_data", return_value=b"a\n1\n")

    content = state._get_notification_content()

    assert content.csv == b"a\n1\n"
    assert content.subreports_csv_bundled is False


def test_pdf_contains_subreport_pages(
    mocker: MockerFixture, run_subreport: Mock
) -> None:
    schedule = _schedule(
        mocker,
        report_format=ReportDataFormat.PDF,
        subreports=[
            _subreport(1, 0),
            _subreport(
                2,
                1,
                viz_type="chart",
                template={"x_column": "month", "y_columns": ["value"]},
            ),
        ],
    )
    state = _state(mocker, schedule)
    mocker.patch.object(state, "_get_screenshots", return_value=[b"parent"])
    build = mocker.patch(f"{EXECUTE}.build_pdf_from_screenshots", return_value=b"pdf")

    content = state._get_notification_content()

    assert content.pdf == b"pdf"
    pages = build.call_args.args[0]
    assert pages[0] == b"parent"
    assert len(pages) == 3
    assert all(page.startswith(b"\x89PNG") for page in pages[1:])


def test_composed_child_inherits_context_and_parent_executor(
    mocker: MockerFixture, run_subreport: Mock
) -> None:
    child = _schedule(
        mocker,
        schedule_id=2,
        name="Child",
        subreports=[_subreport(20, 0, name="Lines")],
    )
    parent = _schedule(
        mocker,
        children=[child],
        native_filters=[{"columnName": "customer_id", "filterValues": [7]}],
    )
    state = _state(mocker, parent)
    captured: list[tuple[int, int]] = []

    def screenshots(self: BaseReportState) -> list[bytes]:
        captured.append((self._report_schedule.id, self._executor_schedule.id))
        return [f"shot-{self._report_schedule.id}".encode()]

    mocker.patch.object(BaseReportState, "_get_screenshots", screenshots)

    content = state._get_notification_content()

    # every capture runs as the root schedule's executor
    assert sorted(captured) == [(1, 1), (2, 1)]
    assert [(item.name, item.kind) for item in content.subreports] == [
        ("Child", "snapshot"),
        ("Child: Lines", "table"),
    ]
    assert content.subreports[0].images == [b"shot-2"]
    assert run_subreport.call_args.args[1].values["customer_id"] == [7]


def test_inactive_children_are_not_composed(
    mocker: MockerFixture, run_subreport: Mock
) -> None:
    child = _schedule(mocker, schedule_id=2, subreports=[_subreport(20, 0)])
    child.active = False
    parent = _schedule(mocker, children=[child])
    state = _state(mocker, parent)
    mocker.patch.object(state, "_get_screenshots", return_value=[b"img"])

    assert state._get_notification_content().subreports == []
    run_subreport.assert_not_called()


def test_composition_cycle_is_rejected(mocker: MockerFixture) -> None:
    child = _schedule(mocker, schedule_id=2)
    parent = _schedule(mocker, children=[child])
    child.children = [parent]
    state = _state(mocker, parent)
    mocker.patch.object(BaseReportState, "_get_screenshots", return_value=[b"img"])

    with pytest.raises(ReportScheduleSubreportFailedError, match="cycle"):
        state._get_notification_content()


def test_composition_depth_is_bounded(mocker: MockerFixture, app: Any) -> None:
    grandchild = _schedule(mocker, schedule_id=3)
    child = _schedule(mocker, schedule_id=2, children=[grandchild])
    parent = _schedule(mocker, children=[child])
    state = _state(mocker, parent)
    mocker.patch.object(BaseReportState, "_get_screenshots", return_value=[b"img"])
    mocker.patch.dict(app.config, {"ALERT_REPORTS_MAX_SCHEDULE_DEPTH": 1})

    with pytest.raises(ReportScheduleSubreportFailedError, match="depth"):
        state._get_notification_content()


def test_inherit_context_prefers_child_values() -> None:
    child = SubreportContext(values={"a": [1]}, errors={"b": "ambiguous"})
    parent = SubreportContext(
        values={"a": [9], "b": [9], "c": [3]}, errors={"d": "parent ambiguous"}
    )
    merged = _inherit_context(child, parent)
    assert merged.values == {"a": [1], "c": [3]}
    assert merged.errors == {"b": "ambiguous", "d": "parent ambiguous"}
    assert _inherit_context(child, None) is child


def test_alert_subreports_are_never_composed(
    mocker: MockerFixture, run_subreport: Mock
) -> None:
    schedule = _schedule(mocker, chart=True, subreports=[_subreport(1, 0)])
    schedule.type = ReportScheduleType.ALERT
    state = _state(mocker, schedule)
    mocker.patch.object(state, "_get_screenshots", return_value=[b"img"])
    embedded = mocker.patch.object(state, "_get_embedded_data")

    content = state._get_notification_content()

    assert content.subreports == []
    run_subreport.assert_not_called()
    embedded.assert_not_called()
