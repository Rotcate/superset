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
from io import BytesIO
from typing import Any
from unittest.mock import MagicMock
from zipfile import ZipFile

import pandas as pd
import pytest
from pytest_mock import MockerFixture

from superset.reports.models import ReportRecipients, ReportRecipientType
from superset.reports.notifications.base import NotificationContent, SubreportContent
from superset.reports.notifications.email import EmailNotification
from superset.reports.notifications.slack import SlackNotification
from superset.reports.notifications.slackv2 import SlackV2Notification
from superset.reports.notifications.webhook import WebhookNotification
from superset.utils.core import HeaderDataType


@pytest.fixture
def header_data() -> HeaderDataType:
    return {
        "notification_format": "PNG",
        "notification_type": "Report",
        "editors": [1],
        "notification_source": None,
        "chart_id": None,
        "dashboard_id": None,
        "slack_channels": None,
        "execution_id": "test-execution-id",
    }


def _sections() -> list[SubreportContent]:
    return [
        SubreportContent(
            name="<b>Orders</b>",
            kind="table",
            data=pd.DataFrame({"id": [1], "note": ['<script>alert("x")</script>']}),
            truncated=True,
        ),
        SubreportContent(
            name="Trend",
            kind="chart",
            data=pd.DataFrame({"m": ["Jan"], "v": [1]}),
            images=[b"chart-png"],
        ),
    ]


def _content(header_data: HeaderDataType, **kwargs: Any) -> NotificationContent:
    return NotificationContent(
        name="Parent",
        header_data=header_data,
        description="desc",
        url="http://superset/report",
        **kwargs,
    )


def _email(content: NotificationContent) -> EmailNotification:
    return EmailNotification(
        recipient=ReportRecipients(type=ReportRecipientType.EMAIL), content=content
    )


def test_has_attachments_counts_subreport_images(header_data: HeaderDataType) -> None:
    table_only = _content(header_data, subreports=_sections()[:1])
    assert not table_only.has_attachments
    assert _content(header_data, subreports=_sections()).has_attachments
    assert _content(header_data).subreports == []


def test_email_renders_ordered_escaped_subreports(header_data: HeaderDataType) -> None:
    email = _email(
        _content(header_data, screenshots=[b"parent"], subreports=_sections())
    )
    result = email._get_content()

    body = result.body
    assert "<h3>&lt;b&gt;Orders&lt;/b&gt;</h3>" in body
    assert "<script>" not in body
    assert "&lt;script&gt;" in body
    assert "truncated" in body
    assert body.index("Orders") < body.index("Trend")
    assert result.images is not None
    images = list(result.images.values())
    assert images == [b"parent", b"chart-png"]
    # the parent screenshot is rendered once, the chart inside its section
    assert body.count("cid:") == 2
    assert result.data is not None
    assert list(result.data) == ["01_bOrders_b.csv", "02_Trend.csv"]


def test_email_bundled_csv_is_not_duplicated(header_data: HeaderDataType) -> None:
    email = _email(
        _content(
            header_data,
            csv=b"PK\x03\x04bundle",
            subreports=_sections(),
            subreports_csv_bundled=True,
        )
    )
    result = email._get_content()
    assert result.data == {"Parent.zip": b"PK\x03\x04bundle"}


def test_email_without_subreports_is_unchanged(header_data: HeaderDataType) -> None:
    email = _email(_content(header_data, csv=b"a,b\n"))
    result = email._get_content()
    assert 'class="subreport"' not in result.body
    assert result.data == {"Parent.csv": b"a,b\n"}


def test_slack_body_includes_subreport_tables(header_data: HeaderDataType) -> None:
    notification = SlackNotification(
        recipient=ReportRecipients(
            type=ReportRecipientType.SLACK, recipient_config_json='{"target": "c"}'
        ),
        content=_content(header_data, subreports=_sections()),
    )
    body = notification._get_body(notification._content)
    assert "*&lt;b&gt;Orders&lt;/b&gt;*" in body
    assert "&lt;script&gt;" in body
    assert "*Trend*" in body
    assert len(body) <= 4000


def test_slack_body_without_subreports_is_unchanged(
    header_data: HeaderDataType,
) -> None:
    notification = SlackNotification(
        recipient=ReportRecipients(
            type=ReportRecipientType.SLACK, recipient_config_json='{"target": "c"}'
        ),
        content=_content(header_data),
    )
    content = notification._content
    assert notification._get_body(content) == notification._message_template(
        content=content
    )


def _send_slackv2(
    mocker: MockerFixture, content: NotificationContent
) -> tuple[MagicMock, MagicMock]:
    mocker.patch("superset.reports.notifications.slackv2.get_slack_client")
    upload = mocker.patch(
        "superset.reports.notifications.slackv2._upload_file_to_slack"
    )
    text = mocker.patch("superset.reports.notifications.slackv2.send_slack_text")

    def run(channels: list[str], send: Any, retry_deadline: Any) -> None:
        for channel in channels:
            send(channel, 1.0)

    mocker.patch(
        "superset.reports.notifications.slackv2.send_to_slack_channels",
        side_effect=run,
    )
    SlackV2Notification(
        recipient=ReportRecipients(
            type=ReportRecipientType.SLACKV2, recipient_config_json='{"target": "C1"}'
        ),
        content=content,
    ).send()
    return upload, text


def test_slackv2_uploads_subreport_charts_and_csvs(
    mocker: MockerFixture, header_data: HeaderDataType
) -> None:
    upload, text = _send_slackv2(
        mocker, _content(header_data, screenshots=[b"parent"], subreports=_sections())
    )
    text.assert_not_called()
    uploads = [
        (call.kwargs["filename"], call.kwargs["file"]) for call in upload.call_args_list
    ]
    assert uploads[0] == ("Parent.png", b"parent")
    assert uploads[1] == ("02_Trend.png", b"chart-png")
    assert [name for name, _ in uploads[2:]] == [
        "01_bOrders_b.csv",
        "02_Trend.csv",
    ]
    assert "Orders" in upload.call_args_list[0].kwargs["initial_comment"]
    assert upload.call_args_list[1].kwargs["initial_comment"] == "*Trend*"


def test_slackv2_text_report_posts_body_then_files(
    mocker: MockerFixture, header_data: HeaderDataType
) -> None:
    upload, text = _send_slackv2(
        mocker, _content(header_data, subreports=_sections()[:1])
    )
    assert "Orders" in text.call_args.args[2]
    assert [call.kwargs["filename"] for call in upload.call_args_list] == [
        "01_bOrders_b.csv"
    ]


def test_slackv2_bundled_csv_uses_zip_name(
    mocker: MockerFixture, header_data: HeaderDataType
) -> None:
    upload, _ = _send_slackv2(
        mocker,
        _content(
            header_data,
            csv=b"PK\x03\x04",
            subreports=_sections()[:1],
            subreports_csv_bundled=True,
        ),
    )
    assert [call.kwargs["filename"] for call in upload.call_args_list] == ["Parent.zip"]


def test_webhook_payload_and_files(header_data: HeaderDataType) -> None:
    content = _content(header_data, subreports=_sections())
    content.subreports[0].data = pd.DataFrame(
        {"day": [pd.Timestamp("2024-01-02")], "n": [1]}
    )
    notification = WebhookNotification(
        recipient=ReportRecipients(
            type=ReportRecipientType.WEBHOOK,
            recipient_config_json='{"target": "https://example.com"}',
        ),
        content=content,
    )
    payload = notification._get_req_payload()
    sections = payload["subreports"]
    assert [section["name"] for section in sections] == ["<b>Orders</b>", "Trend"]
    assert sections[0]["data"] == [{"day": "2024-01-02T00:00:00", "n": 1}]
    assert sections[0]["truncated"] is True
    names = [entry[1][0] for entry in notification._get_files()]
    assert names == [
        "subreport_01_bOrders_b.csv",
        "subreport_02_Trend.csv",
        "subreport_02_Trend.png",
    ]


def test_webhook_without_subreports_is_unchanged(header_data: HeaderDataType) -> None:
    notification = WebhookNotification(
        recipient=ReportRecipients(
            type=ReportRecipientType.WEBHOOK,
            recipient_config_json='{"target": "https://example.com"}',
        ),
        content=_content(header_data, csv=b"a\n"),
    )
    assert "subreports" not in notification._get_req_payload()
    assert notification._get_files() == [("files", ("report.csv", b"a\n", "text/csv"))]


def test_bundled_zip_is_readable(header_data: HeaderDataType) -> None:
    from superset.reports.notifications.subreports import bundle_csv

    bundled = bundle_csv("Parent", b"a\n", _sections())
    with ZipFile(BytesIO(bundled)) as archive:
        assert len(archive.namelist()) == 3
