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
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

import pandas as pd

from superset.reports.models import ReportRecipients, ReportRecipientType
from superset.utils.core import HeaderDataType

SubreportContentKind = Literal["table", "chart", "snapshot"]


@dataclass
class SubreportContent:
    """
    One ordered section rendered after the parent report's own content.

    ``table`` sections carry ``data``; ``chart`` sections carry ``data`` and the
    rendered PNG in ``images``; ``snapshot`` sections carry screenshots of a
    composed child schedule's chart or dashboard in ``images``.
    """

    name: str
    kind: SubreportContentKind
    data: Optional[pd.DataFrame] = None
    images: list[bytes] = field(default_factory=list)
    truncated: bool = False
    row_limit: Optional[int] = None
    # Name of the composed child schedule that produced this section, if any
    source: Optional[str] = None


@dataclass
class NotificationContent:
    name: str
    header_data: HeaderDataType  # this is optional to account for error states
    csv: Optional[bytes] = None  # bytes for csv file
    xlsx: Optional[bytes] = None  # bytes for Excel file
    pdf: Optional[bytes] = None  # bytes for PDF file
    screenshots: Optional[list[bytes]] = None  # bytes for a list of screenshots
    text: Optional[str] = None
    description: Optional[str] = ""
    url: Optional[str] = None  # url to chart/dashboard for this screenshot
    embedded_data: Optional[pd.DataFrame] = None
    slack_retry_deadline: Optional[float] = None
    # Populated only when this is a per-retry or final-failure notification
    retry_attempt: Optional[int] = None
    retry_max_attempts: Optional[int] = None
    include_cta: bool = True  # include the call-to-action link back to Superset
    # Ordered subreport and composed child schedule sections
    subreports: list[SubreportContent] = field(default_factory=list)
    # ``csv`` is a ZIP that already bundles every subreport CSV export
    subreports_csv_bundled: bool = False

    @property
    def has_attachments(self) -> bool:
        """Return whether the notification contains any file attachment."""
        return bool(
            self.csv
            or self.xlsx
            or self.pdf
            or self.screenshots
            or any(item.images for item in self.subreports)
        )


class BaseNotification:  # pylint: disable=too-few-public-methods
    """
    Serves has base for all notifications and creates a simple plugin system
    for extending future implementations.
    Child implementations get automatically registered and should identify the
    notification type
    """

    plugins: list[type["BaseNotification"]] = []
    type: Optional[ReportRecipientType] = None
    """
    Child classes set their notification type ex: `type = "email"` this string will be
    used by ReportRecipients.type to map to the correct implementation
    """

    def __init_subclass__(cls, *args: Any, **kwargs: Any) -> None:
        super().__init_subclass__(*args, **kwargs)
        cls.plugins.append(cls)

    def __init__(
        self, recipient: ReportRecipients, content: NotificationContent
    ) -> None:
        self._recipient = recipient
        self._content = content

    def send(self) -> None:
        raise NotImplementedError()
