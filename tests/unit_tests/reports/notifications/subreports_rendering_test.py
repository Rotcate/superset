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
from zipfile import ZipFile

import pandas as pd
import pytest
from PIL import Image

from superset.reports.notifications.base import SubreportContent
from superset.reports.notifications.subreports import (
    bundle_csv,
    display_frame,
    escape_slack,
    render_chart_png,
    render_pdf_pages,
    slack_subreports_text,
    subreport_csv_files,
    subreport_file_name,
    subreport_image_files,
    SubreportRenderError,
)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _table(name: str = "Orders", **kwargs: object) -> SubreportContent:
    return SubreportContent(
        name=name,
        kind="table",
        data=pd.DataFrame({"id": [1, 2], "item": ["a", "b"]}),
        **kwargs,  # type: ignore[arg-type]
    )


def test_display_frame_orders_and_validates_columns() -> None:
    df = pd.DataFrame({"a": [1], "b": [2], "c": [3]})
    assert list(display_frame(df, ["c", "a"]).columns) == ["c", "a"]
    assert list(display_frame(df, None).columns) == ["a", "b", "c"]
    with pytest.raises(SubreportRenderError, match="missing"):
        display_frame(df, ["missing"])


def test_subreport_file_names_are_ordered_and_safe() -> None:
    content = _table(name="../../etc/passwd <x>")
    name = subreport_file_name(0, content, "csv")
    assert name.startswith("01_")
    assert "/" not in name
    assert ".." not in name
    assert "<" not in name
    assert subreport_file_name(1, _table(name="///"), "csv") == "02_subreport.csv"


def test_subreport_csv_files_are_ordered_and_escape_formulas(app_context: None) -> None:
    first = SubreportContent(
        name="First", kind="table", data=pd.DataFrame({"x": ["=cmd()"]})
    )
    second = _table(name="Second")
    snapshot = SubreportContent(name="Snap", kind="snapshot", images=[b"png"])
    files = subreport_csv_files([first, snapshot, second])
    assert list(files) == ["01_First.csv", "03_Second.csv"]
    assert b"'=cmd()" in files["01_First.csv"]


def test_bundle_csv_contains_parent_and_children(app_context: None) -> None:
    bundled = bundle_csv("My Report", b"a,b\n1,2\n", [_table(), _table(name="Two")])
    with ZipFile(BytesIO(bundled)) as archive:
        assert archive.namelist() == [
            "My_Report.csv",
            "subreports/01_Orders.csv",
            "subreports/02_Two.csv",
        ]
        assert archive.read("My_Report.csv") == b"a,b\n1,2\n"
        assert b"id,item" in archive.read("subreports/01_Orders.csv")


def test_bundle_csv_flattens_parent_zip(app_context: None) -> None:
    parent = BytesIO()
    with ZipFile(parent, "w") as archive:
        archive.writestr("query_1.csv", b"a\n1\n")
        archive.writestr("../query_2.csv", b"b\n2\n")
    bundled = bundle_csv("Report", parent.getvalue(), [_table()])
    with ZipFile(BytesIO(bundled)) as archive:
        names = archive.namelist()
    assert names == [
        "report/query_1.csv",
        "report/query_2.csv",
        "subreports/01_Orders.csv",
    ]


def test_escape_slack_neutralizes_links_and_mentions() -> None:
    assert escape_slack("<!channel> <@U1> & <http://x|y>") == (
        "&lt;!channel&gt; &lt;@U1&gt; &amp; &lt;http://x|y&gt;"
    )


def test_slack_text_names_sections_and_escapes_cells() -> None:
    content = SubreportContent(
        name="<!here> Orders",
        kind="table",
        data=pd.DataFrame({"note": ["<@U1>```x"]}),
    )
    chart = SubreportContent(name="Trend", kind="chart", images=[b"png"])
    text = slack_subreports_text([content, chart], 4000)
    assert "*&lt;!here&gt; Orders*" in text
    assert "<@U1>" not in text
    assert "&lt;@U1&gt;'''x" in text
    assert "*Trend*\n(rendered as an image)" in text
    assert text.index("Orders") < text.index("Trend")


def test_slack_text_truncates_to_budget() -> None:
    content = SubreportContent(
        name="Big",
        kind="table",
        data=pd.DataFrame({"value": [f"row-{i}" * 5 for i in range(500)]}),
    )
    text = slack_subreports_text([content], 1500)
    assert len(text) <= 1500
    assert "(table was truncated)" in text
    many = [_table(name=f"T{i}") for i in range(200)]
    text = slack_subreports_text(many, 1000)
    assert len(text) <= 1000
    assert "(more subreports were omitted)" in text


@pytest.mark.parametrize("chart_type", ["bar", "line"])
def test_render_chart_png(chart_type: str) -> None:
    df = pd.DataFrame(
        {"month": ["Jan", "Feb", "Mar"], "a": [1, -2, 3], "b": [2, None, 1]}
    )
    template = {"chart_type": chart_type, "x_column": "month", "y_columns": ["a", "b"]}
    png = render_chart_png("Sales", df, template)
    assert png.startswith(PNG_SIGNATURE)
    assert Image.open(BytesIO(png)).size == (960, 480)


def test_render_chart_png_handles_no_numeric_data() -> None:
    df = pd.DataFrame({"month": ["Jan"], "a": ["n/a"]})
    png = render_chart_png("Empty", df, {"x_column": "month", "y_columns": ["a"]})
    assert png.startswith(PNG_SIGNATURE)


def test_render_chart_png_missing_column() -> None:
    df = pd.DataFrame({"month": ["Jan"]})
    with pytest.raises(SubreportRenderError, match="revenue"):
        render_chart_png("x", df, {"x_column": "month", "y_columns": ["revenue"]})


def test_render_pdf_pages_includes_every_section() -> None:
    big = SubreportContent(
        name="Big",
        kind="table",
        data=pd.DataFrame({"n": range(85)}),
        truncated=True,
    )
    chart = SubreportContent(name="Chart", kind="chart", images=[b"chart-png"])
    snapshot = SubreportContent(name="Child", kind="snapshot", images=[b"a", b"b"])
    pages = render_pdf_pages([big, chart, snapshot])
    # 85 rows at 40 rows per page -> 3 table pages
    assert len(pages) == 6
    assert all(page.startswith(PNG_SIGNATURE) for page in pages[:3])
    assert pages[3:] == [b"chart-png", b"a", b"b"]


def test_subreport_image_files() -> None:
    chart = SubreportContent(name="Chart", kind="chart", images=[b"c"])
    snapshot = SubreportContent(name="Child", kind="snapshot", images=[b"a", b"b"])
    files = subreport_image_files([_table(), chart, snapshot])
    assert [(name, image) for name, _, image in files] == [
        ("02_Chart.png", b"c"),
        ("03_Child_1.png", b"a"),
        ("03_Child_2.png", b"b"),
    ]
