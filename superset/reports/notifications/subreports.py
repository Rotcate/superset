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
Rendering helpers for subreport sections inside report notifications.

Charts and PDF table pages are drawn with Pillow (already used for PDF
generation) so no browser round trip or extra plotting dependency is needed.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping, Sequence
from io import BytesIO
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

import pandas as pd
from flask_babel import gettext as __
from PIL import Image, ImageDraw, ImageFont
from werkzeug.utils import secure_filename

from superset.exceptions import SupersetException
from superset.reports.notifications.base import SubreportContent
from superset.reports.subreports import dataframe_to_csv

logger = logging.getLogger(__name__)

ZIP_LOCAL_FILE_HEADER = b"PK\x03\x04"

CHART_SIZE = (960, 480)
CHART_PALETTE = (
    (31, 168, 201),
    (69, 78, 124),
    (90, 193, 137),
    (255, 127, 68),
    (102, 102, 102),
    (224, 67, 85),
    (252, 199, 0),
    (168, 104, 183),
)
TEXT_COLOR = (42, 63, 95)
GRID_COLOR = (225, 230, 238)
AXIS_COLOR = (150, 160, 175)
MAX_X_LABELS = 12
MAX_LABEL_CHARS = 16
MAX_CELL_CHARS = 40
TABLE_ROWS_PER_PAGE = 40
MAX_TABLE_PAGE_WIDTH = 2400
# Slack markdown text is only rendered reliably up to roughly 4k characters
SLACK_TEXT_BUDGET = 4000


class SubreportRenderError(SupersetException):
    status = 422
    message = __("The subreport result could not be rendered.")


# --------------------------------------------------------------------------- #
# Data shaping
# --------------------------------------------------------------------------- #
def flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Return ``df`` with flat string column labels and a default index."""
    flat = df.copy()
    flat.columns = [
        (
            " ".join(str(part) for part in column).strip()
            if isinstance(column, tuple)
            else str(column)
        )
        for column in flat.columns
    ]
    return flat.reset_index(drop=True)


def display_frame(
    df: pd.DataFrame, columns: Sequence[str] | None = None
) -> pd.DataFrame:
    """
    Columns configured in a table template, in their configured order.

    :raises SubreportRenderError: If a configured column is not in the result
    """
    flat = flatten_columns(df)
    if not columns:
        return flat
    if missing := [column for column in columns if column not in flat.columns]:
        raise SubreportRenderError(
            __(
                "Subreport columns not found in the query result: %(columns)s",
                columns=", ".join(missing),
            )
        )
    return flat[list(columns)]


def truncation_note(content: SubreportContent) -> str | None:
    if not content.truncated or content.data is None:
        return None
    return __(
        "Showing the first %(rows)s rows; the full result was truncated.",
        rows=len(content.data.index),
    )


# --------------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------------- #
def subreport_file_name(
    index: int, content: SubreportContent, extension: str, suffix: str = ""
) -> str:
    """Stable, filesystem-safe and ordered attachment name for a section."""
    slug = secure_filename(content.name)[:80] or "subreport"
    return f"{index + 1:02d}_{slug}{suffix}.{extension}"


def subreport_csv(content: SubreportContent) -> bytes | None:
    if content.data is None:
        return None
    return dataframe_to_csv(flatten_columns(content.data))


def subreport_csv_entries(
    subreports: Sequence[SubreportContent],
) -> list[tuple[str, SubreportContent, bytes]]:
    """Ordered ``(file_name, section, csv)`` for every section that has data."""
    return [
        (subreport_file_name(index, content, "csv"), content, csv)
        for index, content in enumerate(subreports)
        if (csv := subreport_csv(content)) is not None
    ]


def subreport_csv_files(subreports: Sequence[SubreportContent]) -> dict[str, bytes]:
    """Ordered ``{file_name: csv_bytes}`` for every section that has data."""
    return {name: csv for name, _, csv in subreport_csv_entries(subreports)}


def subreport_image_files(
    subreports: Sequence[SubreportContent],
) -> list[tuple[str, SubreportContent, bytes]]:
    """Ordered ``(file_name, section, png)`` for every rendered image."""
    files: list[tuple[str, SubreportContent, bytes]] = []
    for index, content in enumerate(subreports):
        for image_index, image in enumerate(content.images):
            suffix = f"_{image_index + 1}" if len(content.images) > 1 else ""
            name = subreport_file_name(index, content, "png", suffix)
            files.append((name, content, image))
    return files


def bundle_csv(
    parent_name: str,
    parent_csv: bytes,
    subreports: Sequence[SubreportContent],
) -> bytes:
    """
    ZIP containing the parent CSV export (or the entries of a parent ZIP export)
    followed by one CSV per subreport.
    """
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as bundle:
        if parent_csv.startswith(ZIP_LOCAL_FILE_HEADER):
            with ZipFile(BytesIO(parent_csv)) as parent_zip:
                for info in parent_zip.infolist():
                    if not info.is_dir():
                        bundle.writestr(
                            f"report/{secure_filename(info.filename) or 'data.csv'}",
                            parent_zip.read(info),
                        )
        else:
            name = secure_filename(parent_name)[:80] or "report"
            bundle.writestr(f"{name}.csv", parent_csv)
        for file_name, csv in subreport_csv_files(subreports).items():
            bundle.writestr(f"subreports/{file_name}", csv)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# Slack text
# --------------------------------------------------------------------------- #
def escape_slack(text: str) -> str:
    """Escape Slack control characters so user data cannot form links/mentions."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _slack_cell(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return escape_slack(str(value)).replace("```", "'''").replace("\n", " ")


def _slack_table(df: pd.DataFrame) -> str:
    safe = df.astype(object).where(pd.notna(df), None).map(_slack_cell)
    safe.columns = [_slack_cell(column) for column in safe.columns]
    return f"```\n{safe.to_markdown(index=False)}\n```"


def slack_section_title(content: SubreportContent) -> str:
    return f"*{escape_slack(content.name)}*"


def _slack_section(content: SubreportContent, available: int) -> str | None:
    """One named Slack section no longer than ``available``, or ``None``."""
    title = slack_section_title(content)
    if content.data is None or content.kind != "table":
        candidates = [f"{title}\n{__('(rendered as an image)')}"]
    else:
        df = flatten_columns(content.data)
        candidates = []
        shown = len(df.index)
        while shown > 0:
            lines = [title, _slack_table(df.head(shown))]
            if shown < len(df.index) or content.truncated:
                lines.append(__("(table was truncated)"))
            candidate = "\n".join(lines)
            if len(candidate) <= available:
                candidates.append(candidate)
                break
            shown = shown // 2 if shown > 8 else shown - 1
        rows = len(df.index)
        candidates.append(
            f"{title}\n"
            + __("(%(rows)s rows, too large to show as Slack text)", rows=rows)
        )
        candidates.append(title)
    return next((item for item in candidates if len(item) <= available), None)


def slack_subreports_text(subreports: Sequence[SubreportContent], budget: int) -> str:
    """
    Safe Slack markdown for table sections, never longer than ``budget``.

    Image sections are only named here; they are delivered as files.
    """
    omitted = __("(more subreports were omitted)")
    parts: list[str] = []
    used = 0
    for index, content in enumerate(subreports):
        separator = 2 if parts else 0
        reserve = 0 if index == len(subreports) - 1 else len(omitted) + 2
        section = _slack_section(content, budget - used - separator - reserve)
        if section is None:
            if used + separator + len(omitted) <= budget:
                parts.append(omitted)
            break
        parts.append(section)
        used += separator + len(section)
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# Drawing
# --------------------------------------------------------------------------- #
def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    try:
        return ImageFont.load_default(size=size)
    except (TypeError, OSError):  # pragma: no cover - bitmap-only Pillow builds
        return ImageFont.load_default()


def _clip(value: Any, max_chars: int) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    text = re.sub(r"\s+", " ", str(value))
    return text if len(text) <= max_chars else f"{text[: max_chars - 1]}…"


def _draw_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    font: ImageFont.ImageFont | ImageFont.FreeTypeFont,
    fill: tuple[int, int, int] = TEXT_COLOR,
    anchor: str | None = None,
) -> None:
    try:
        draw.text(xy, text, font=font, fill=fill, anchor=anchor)
    except (UnicodeEncodeError, ValueError):
        # Bitmap fonts only cover Latin-1 and do not support anchors
        safe = text.encode("latin-1", "replace").decode("latin-1")
        draw.text(xy, safe, font=font, fill=fill)


def _text_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont | ImageFont.FreeTypeFont,
) -> float:
    try:
        return draw.textlength(text, font=font)
    except (UnicodeEncodeError, ValueError):
        return draw.textlength(text.encode("latin-1", "replace").decode(), font=font)


def _to_png(image: Image.Image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _format_number(value: float) -> str:
    if value != 0 and (abs(value) >= 1e6 or abs(value) < 1e-2):
        return f"{value:.2e}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def chart_series(
    df: pd.DataFrame, template: Mapping[str, Any]
) -> tuple[list[str], dict[str, list[float | None]]]:
    """
    ``(x_labels, {y_column: values})`` for a chart template.

    :raises SubreportRenderError: If configured columns are missing
    """
    flat = flatten_columns(df)
    x_column = str(template.get("x_column") or "")
    y_columns = [str(column) for column in template.get("y_columns") or []]
    if missing := [
        column for column in [x_column, *y_columns] if column not in flat.columns
    ]:
        raise SubreportRenderError(
            __(
                "Subreport chart columns not found in the query result: %(columns)s",
                columns=", ".join(missing),
            )
        )
    labels = [_clip(value, MAX_LABEL_CHARS) for value in flat[x_column].tolist()]
    series: dict[str, list[float | None]] = {}
    for column in y_columns:
        numeric = pd.to_numeric(flat[column], errors="coerce")
        series[column] = [
            None if pd.isna(value) or not math.isfinite(value) else float(value)
            for value in numeric.tolist()
        ]
    return labels, series


def render_chart_png(  # noqa: C901
    title: str,
    df: pd.DataFrame,
    template: Mapping[str, Any],
) -> bytes:
    """Draw a simple bar or line chart for a chart subreport."""
    labels, series = chart_series(df, template)
    chart_type = template.get("chart_type", "bar")
    width, height = CHART_SIZE
    left, right, top, bottom = 80, 24, 56, 96
    plot_w, plot_h = width - left - right, height - top - bottom

    image = Image.new("RGB", CHART_SIZE, "white")
    draw = ImageDraw.Draw(image)
    title_font, font = _font(18), _font(12)
    _draw_text(draw, (left, 18), _clip(title, 90), title_font)

    values = [v for column in series.values() for v in column if v is not None]
    if not labels or not values:
        _draw_text(
            draw,
            (width / 2, height / 2),
            __("No numeric data to chart"),
            font,
            anchor="mm",
        )
        return _to_png(image)

    low, high = min(values), max(values)
    if chart_type == "bar":
        low, high = min(low, 0.0), max(high, 0.0)
    if math.isclose(low, high):
        low, high = low - 1, high + 1
    span = high - low

    def y_of(value: float) -> float:
        return top + plot_h - (value - low) / span * plot_h

    ticks = 5
    for tick in range(ticks + 1):
        value = low + span * tick / ticks
        y = y_of(value)
        draw.line([(left, y), (left + plot_w, y)], fill=GRID_COLOR)
        _draw_text(draw, (left - 8, y), _format_number(value), font, anchor="rm")
    draw.line([(left, top), (left, top + plot_h)], fill=AXIS_COLOR)
    draw.line([(left, top + plot_h), (left + plot_w, top + plot_h)], fill=AXIS_COLOR)

    count = len(labels)
    slot = plot_w / count
    names = list(series)
    for series_index, name in enumerate(names):
        color = CHART_PALETTE[series_index % len(CHART_PALETTE)]
        points: list[tuple[float, float]] = []
        for index, point in enumerate(series[name]):
            if point is None:
                if len(points) > 1 and chart_type == "line":
                    draw.line(points, fill=color, width=2)
                points = []
                continue
            if chart_type == "bar":
                bar_w = max(1.0, slot * 0.8 / len(names))
                x0 = left + index * slot + slot * 0.1 + series_index * bar_w
                y0, y1 = sorted((y_of(point), y_of(0.0)))
                draw.rectangle([x0, y0, x0 + bar_w - 1, y1], fill=color)
            else:
                points.append((left + index * slot + slot / 2, y_of(point)))
        if chart_type == "line":
            if len(points) > 1:
                draw.line(points, fill=color, width=2)
            for x, y in points if count <= 100 else []:
                draw.ellipse([x - 2, y - 2, x + 2, y + 2], fill=color)

    step = max(1, math.ceil(count / MAX_X_LABELS))
    for index in range(0, count, step):
        x = left + index * slot + slot / 2
        _draw_text(draw, (x, top + plot_h + 14), labels[index], font, anchor="mt")

    legend_x: float = left
    legend_y = height - 34
    for series_index, name in enumerate(names):
        color = CHART_PALETTE[series_index % len(CHART_PALETTE)]
        draw.rectangle([legend_x, legend_y, legend_x + 12, legend_y + 12], fill=color)
        label = _clip(name, 30)
        _draw_text(draw, (legend_x + 18, legend_y), label, font)
        legend_x += 36 + _text_width(draw, label, font)
        if legend_x > width - 120:
            break
    return _to_png(image)


def render_table_pngs(title: str, df: pd.DataFrame, note: str | None) -> list[bytes]:
    """Draw a table section as one or more PNG pages for PDF attachments."""
    flat = flatten_columns(df)
    font, title_font = _font(12), _font(16)
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    header = [_clip(column, MAX_CELL_CHARS) for column in flat.columns]
    cells = [
        [_clip(value, MAX_CELL_CHARS) for value in row]
        for row in flat.astype(object).where(pd.notna(flat), None).values.tolist()
    ]
    widths = [
        max(
            [_text_width(measure, text, font) for text in [header[col]]]
            + [_text_width(measure, row[col], font) for row in cells]
        )
        + 16
        for col in range(len(header))
    ]
    shown = len(widths)
    while shown > 1 and sum(widths[:shown]) > MAX_TABLE_PAGE_WIDTH:
        shown -= 1
    if shown < len(widths):
        omitted = __(
            "%(count)s more columns are available in the CSV export.",
            count=len(widths) - shown,
        )
        note = f"{note} {omitted}" if note else omitted
    widths = widths[:shown]
    page_width = int(max(640, sum(widths) + 40))
    row_h = 22
    pages: list[bytes] = []
    chunks = [
        cells[start : start + TABLE_ROWS_PER_PAGE]
        for start in range(0, max(len(cells), 1), TABLE_ROWS_PER_PAGE)
    ]
    for page_index, chunk in enumerate(chunks):
        page_height = 60 + row_h * (len(chunk) + 1) + (30 if note else 0)
        image = Image.new("RGB", (page_width, page_height), "white")
        draw = ImageDraw.Draw(image)
        heading = _clip(title, 120)
        if len(chunks) > 1:
            heading = f"{heading} ({page_index + 1}/{len(chunks)})"
        _draw_text(draw, (20, 16), heading, title_font)
        y = 50
        for row_index, row in enumerate([header[:shown], *[r[:shown] for r in chunk]]):
            x = 20.0
            if row_index == 0:
                draw.rectangle(
                    [20, y, 20 + sum(widths), y + row_h], fill=(240, 243, 248)
                )
            for col, text in enumerate(row):
                _draw_text(draw, (x + 8, y + 5), text, font)
                x += widths[col]
            draw.line([(20, y + row_h), (20 + sum(widths), y + row_h)], fill=GRID_COLOR)
            y += row_h
        if note:
            _draw_text(draw, (20, y + 8), note, font)
        pages.append(_to_png(image))
    return pages


def render_pdf_pages(subreports: Sequence[SubreportContent]) -> list[bytes]:
    """PNG pages appended to a parent PDF so it contains every section."""
    pages: list[bytes] = []
    for content in subreports:
        if content.kind == "table" and content.data is not None:
            pages.extend(
                render_table_pngs(content.name, content.data, truncation_note(content))
            )
        else:
            pages.extend(content.images)
    return pages
