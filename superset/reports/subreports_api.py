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
"""REST API for subreports nested under a parent report schedule."""

from __future__ import annotations

import logging
from typing import Any

from flask import request, Response
from flask_appbuilder.api import expose, permission_name, protect, safe
from flask_appbuilder.hooks import before_request
from marshmallow import ValidationError

from superset import is_feature_enabled
from superset.commands.exceptions import CommandException
from superset.commands.report.exceptions import (
    ReportScheduleForbiddenError,
    ReportScheduleNotFoundError,
    SubreportCreateFailedError,
    SubreportDeleteFailedError,
    SubreportInvalidError,
    SubreportNotFoundError,
    SubreportUpdateFailedError,
)
from superset.commands.report.subreport import (
    CreateSubreportCommand,
    DeleteSubreportCommand,
    get_parent_schedule,
    PreviewSubreportCommand,
    UpdateSubreportCommand,
)
from superset.daos.report import SubreportDAO
from superset.extensions import event_logger
from superset.reports.models import Subreport
from superset.reports.schemas import (
    SubreportContextFieldSchema,
    SubreportPreviewPostSchema,
    SubreportResponseSchema,
    SubreportSchema,
)
from superset.reports.subreports import (
    dataframe_to_payload,
    FEATURE_FLAG,
    get_available_context_field_names,
    get_available_context_fields,
    sort_subreports,
    SubreportError,
    SubreportQueryResult,
)
from superset.utils import json
from superset.views.base_api import BaseSupersetApi, requires_json, statsd_metrics

logger = logging.getLogger(__name__)

# Subreports are part of a report schedule: reuse its read/write permissions
# (``can_read``/``can_write`` on ``ReportSchedule``, Alpha-only by default).
# Preview runs SQL against a database, so it requires write like other edits.
SUBREPORT_METHOD_PERMISSION_MAP = {
    "get_list": "read",
    "get": "read",
    "post": "write",
    "put": "write",
    "delete": "write",
    "execute_preview": "write",
    "execute_preview_for_parent": "write",
}


class SubreportRestApi(BaseSupersetApi):
    """
    ``/api/v1/report/<parent_id>/subreport/...`` and
    ``/api/v1/subreport/execute_preview``.

    Every route resolves the parent through the ``ReportScheduleDAO`` base
    filter; mutations and preview also require editorship of the parent.
    """

    route_base = "/api/v1"
    resource_name = "subreport"
    class_permission_name = "ReportSchedule"
    method_permission_name = SUBREPORT_METHOD_PERMISSION_MAP
    allow_browser_login = True
    openapi_spec_tag = "Report Schedules"
    openapi_spec_component_schemas = (
        SubreportContextFieldSchema,
        SubreportPreviewPostSchema,
        SubreportResponseSchema,
        SubreportSchema,
    )

    response_schema = SubreportResponseSchema(
        only=(
            "id",
            "name",
            "database_id",
            "sql_query",
            "param_mapping",
            "position",
            "viz_type",
            "template",
        )
    )
    context_field_schema = SubreportContextFieldSchema()

    @before_request
    def ensure_alert_reports_enabled(self) -> Response | None:
        if not is_feature_enabled(FEATURE_FLAG):
            return self.response_404()
        return None

    def _error_response(self, ex: Exception) -> Response:
        if isinstance(ex, (ReportScheduleNotFoundError, SubreportNotFoundError)):
            return self.response_404()
        if isinstance(ex, ReportScheduleForbiddenError):
            return self.response_403()
        if isinstance(ex, SubreportInvalidError):
            return self.response_422(message=ex.normalized_messages())
        if isinstance(ex, SubreportError):
            return self.response(ex.status, message=str(ex.message))
        if isinstance(
            ex,
            (
                SubreportCreateFailedError,
                SubreportUpdateFailedError,
                SubreportDeleteFailedError,
            ),
        ):
            logger.error("Subreport command failed: %s", ex, exc_info=True)
            return self.response_422(message=str(ex.message))
        raise ex

    def _dump(self, subreport: Subreport) -> dict[str, Any]:
        return self.response_schema.dump(subreport)

    @staticmethod
    def _json_response(payload: dict[str, Any], status: int = 200) -> Response:
        return Response(
            json.dumps(payload, default=json.json_iso_dttm_ser, ignore_nan=True),
            status=status,
            mimetype="application/json",
        )

    @staticmethod
    def _preview_payload(result: SubreportQueryResult) -> dict[str, Any]:
        payload = dataframe_to_payload(result.df, result.truncated)
        return {
            "result": {
                "columns": [column["name"] for column in payload["columns"]],
                "data": payload["data"],
                "row_count": payload["row_count"],
                "truncated": payload["truncated"],
            }
        }

    @expose("/report/<int:parent_id>/subreport/", methods=("GET",))
    @protect()
    @safe
    @statsd_metrics
    @permission_name("get")
    def get_list(self, parent_id: int) -> Response:
        """List the subreports of a report schedule.
        ---
        get:
          summary: List the subreports of a report schedule
          parameters:
          - in: path
            schema:
              type: integer
            name: parent_id
            description: The parent report schedule id
          responses:
            200:
              description: Ordered subreports and the parent context fields
              content:
                application/json:
                  schema:
                    type: object
                    properties:
                      result:
                        type: array
                        items:
                          $ref: '#/components/schemas/SubreportResponseSchema'
                      context_fields:
                        type: array
                        items:
                          $ref: '#/components/schemas/SubreportContextFieldSchema'
            401:
              $ref: '#/components/responses/401'
            403:
              $ref: '#/components/responses/403'
            404:
              $ref: '#/components/responses/404'
        """
        try:
            parent = get_parent_schedule(parent_id, for_update=False)
        except (CommandException, SubreportError) as ex:
            return self._error_response(ex)
        return self._json_response(
            {
                "result": [
                    self._dump(item) for item in sort_subreports(parent.subreports)
                ],
                "context_fields": [
                    item.to_dict() for item in get_available_context_fields(parent)
                ],
            }
        )

    @expose("/report/<int:parent_id>/subreport/<int:pk>", methods=("GET",))
    @protect()
    @safe
    @statsd_metrics
    @permission_name("get")
    def get(self, parent_id: int, pk: int) -> Response:
        """Get a subreport.
        ---
        get:
          summary: Get a subreport
          parameters:
          - in: path
            schema:
              type: integer
            name: parent_id
            description: The parent report schedule id
          - in: path
            schema:
              type: integer
            name: pk
            description: The subreport id
          responses:
            200:
              description: The subreport
              content:
                application/json:
                  schema:
                    type: object
                    properties:
                      id:
                        type: integer
                      result:
                        $ref: '#/components/schemas/SubreportResponseSchema'
            401:
              $ref: '#/components/responses/401'
            403:
              $ref: '#/components/responses/403'
            404:
              $ref: '#/components/responses/404'
        """
        try:
            parent = get_parent_schedule(parent_id, for_update=False)
        except (CommandException, SubreportError) as ex:
            return self._error_response(ex)
        subreport = SubreportDAO.find_by_parent(parent.id, pk)
        if subreport is None:
            return self.response_404()
        return self._json_response(
            {"id": subreport.id, "result": self._dump(subreport)}
        )

    @expose("/report/<int:parent_id>/subreport/", methods=("POST",))
    @protect()
    @safe
    @statsd_metrics
    @permission_name("post")
    @requires_json
    @event_logger.log_this_with_context(
        action=lambda self, *args, **kwargs: f"{self.__class__.__name__}.post",
        log_to_statsd=False,
    )
    def post(self, parent_id: int) -> Response:
        """Create a subreport.
        ---
        post:
          summary: Create a subreport
          parameters:
          - in: path
            schema:
              type: integer
            name: parent_id
            description: The parent report schedule id
          requestBody:
            required: true
            content:
              application/json:
                schema:
                  $ref: '#/components/schemas/SubreportSchema'
          responses:
            201:
              description: Subreport created
              content:
                application/json:
                  schema:
                    type: object
                    properties:
                      id:
                        type: integer
                      result:
                        $ref: '#/components/schemas/SubreportResponseSchema'
            400:
              $ref: '#/components/responses/400'
            401:
              $ref: '#/components/responses/401'
            403:
              $ref: '#/components/responses/403'
            404:
              $ref: '#/components/responses/404'
            422:
              $ref: '#/components/responses/422'
        """
        try:
            parent = get_parent_schedule(parent_id, for_update=True)
            fields_ = get_available_context_field_names(parent)
        except (CommandException, SubreportError) as ex:
            return self._error_response(ex)
        try:
            item = SubreportSchema(available_context_fields=fields_).load(request.json)
        except ValidationError as error:
            return self.response_400(message=error.messages)
        try:
            subreport = CreateSubreportCommand(parent.id, item).run()
        except (CommandException, SubreportError) as ex:
            return self._error_response(ex)
        return self._json_response(
            {"id": subreport.id, "result": self._dump(subreport)}, status=201
        )

    @expose("/report/<int:parent_id>/subreport/<int:pk>", methods=("PUT",))
    @protect()
    @safe
    @statsd_metrics
    @permission_name("put")
    @requires_json
    @event_logger.log_this_with_context(
        action=lambda self, *args, **kwargs: f"{self.__class__.__name__}.put",
        log_to_statsd=False,
    )
    def put(self, parent_id: int, pk: int) -> Response:
        """Update a subreport.
        ---
        put:
          summary: Update a subreport
          description: >-
            Partial update. The parent cannot be changed; the merged
            definition is validated like a new subreport.
          parameters:
          - in: path
            schema:
              type: integer
            name: parent_id
            description: The parent report schedule id
          - in: path
            schema:
              type: integer
            name: pk
            description: The subreport id
          requestBody:
            required: true
            content:
              application/json:
                schema:
                  $ref: '#/components/schemas/SubreportSchema'
          responses:
            200:
              description: Subreport updated
              content:
                application/json:
                  schema:
                    type: object
                    properties:
                      id:
                        type: integer
                      result:
                        $ref: '#/components/schemas/SubreportResponseSchema'
            400:
              $ref: '#/components/responses/400'
            401:
              $ref: '#/components/responses/401'
            403:
              $ref: '#/components/responses/403'
            404:
              $ref: '#/components/responses/404'
            422:
              $ref: '#/components/responses/422'
        """
        try:
            parent = get_parent_schedule(parent_id, for_update=True)
            fields_ = get_available_context_field_names(parent)
        except (CommandException, SubreportError) as ex:
            return self._error_response(ex)
        try:
            item = SubreportSchema(
                available_context_fields=fields_, partial_update=True
            ).load(request.json)
        except ValidationError as error:
            return self.response_400(message=error.messages)
        try:
            subreport = UpdateSubreportCommand(parent.id, pk, item).run()
        except (CommandException, SubreportError) as ex:
            return self._error_response(ex)
        return self._json_response(
            {"id": subreport.id, "result": self._dump(subreport)}
        )

    @expose("/report/<int:parent_id>/subreport/<int:pk>", methods=("DELETE",))
    @protect()
    @safe
    @statsd_metrics
    @permission_name("delete")
    @event_logger.log_this_with_context(
        action=lambda self, *args, **kwargs: f"{self.__class__.__name__}.delete",
        log_to_statsd=False,
    )
    def delete(self, parent_id: int, pk: int) -> Response:
        """Delete a subreport.
        ---
        delete:
          summary: Delete a subreport
          parameters:
          - in: path
            schema:
              type: integer
            name: parent_id
            description: The parent report schedule id
          - in: path
            schema:
              type: integer
            name: pk
            description: The subreport id
          responses:
            200:
              description: Subreport deleted
              content:
                application/json:
                  schema:
                    type: object
                    properties:
                      message:
                        type: string
            401:
              $ref: '#/components/responses/401'
            403:
              $ref: '#/components/responses/403'
            404:
              $ref: '#/components/responses/404'
            422:
              $ref: '#/components/responses/422'
        """
        try:
            DeleteSubreportCommand(parent_id, pk).run()
        except (CommandException, SubreportError) as ex:
            return self._error_response(ex)
        return self.response(200, message="OK")

    def _preview(self, parent_id: int, payload: Any) -> Response:
        try:
            item = SubreportPreviewPostSchema().load(payload)
        except ValidationError as error:
            return self.response_400(message=error.messages)
        try:
            result = PreviewSubreportCommand(parent_id, item).run()
        except (CommandException, SubreportError) as ex:
            return self._error_response(ex)
        return self._json_response(self._preview_payload(result))

    @expose("/report/<int:parent_id>/subreport/execute_preview", methods=("POST",))
    @protect()
    @safe
    @statsd_metrics
    @permission_name("execute_preview_for_parent")
    @requires_json
    @event_logger.log_this_with_context(
        action=lambda self, *args, **kwargs: (
            f"{self.__class__.__name__}.execute_preview"
        ),
        log_to_statsd=False,
    )
    def execute_preview_for_parent(self, parent_id: int) -> Response:
        """Run an unsaved subreport definition for a parent report.
        ---
        post:
          summary: Preview an unsaved subreport
          parameters:
          - in: path
            schema:
              type: integer
            name: parent_id
            description: The parent report schedule id
          requestBody:
            required: true
            content:
              application/json:
                schema:
                  $ref: '#/components/schemas/SubreportPreviewPostSchema'
          responses:
            200:
              description: Preview rows
              content:
                application/json:
                  schema:
                    type: object
                    properties:
                      result:
                        type: object
                        properties:
                          columns:
                            type: array
                            items:
                              type: string
                          data:
                            type: array
                            items:
                              type: object
                          row_count:
                            type: integer
                          truncated:
                            type: boolean
            400:
              $ref: '#/components/responses/400'
            401:
              $ref: '#/components/responses/401'
            403:
              $ref: '#/components/responses/403'
            404:
              $ref: '#/components/responses/404'
            422:
              $ref: '#/components/responses/422'
        """
        return self._preview(parent_id, request.json)

    @expose("/subreport/execute_preview", methods=("POST",))
    @protect()
    @safe
    @statsd_metrics
    @permission_name("execute_preview")
    @requires_json
    @event_logger.log_this_with_context(
        action=lambda self, *args, **kwargs: (
            f"{self.__class__.__name__}.execute_preview"
        ),
        log_to_statsd=False,
    )
    def execute_preview(self) -> Response:
        """Run an unsaved subreport definition; ``parent_schedule_id`` is required.
        ---
        post:
          summary: Preview an unsaved subreport
          requestBody:
            required: true
            content:
              application/json:
                schema:
                  allOf:
                  - $ref: '#/components/schemas/SubreportPreviewPostSchema'
                  - type: object
                    required:
                    - parent_schedule_id
                    properties:
                      parent_schedule_id:
                        type: integer
          responses:
            200:
              description: Preview rows, same shape as the per-parent preview
            400:
              $ref: '#/components/responses/400'
            401:
              $ref: '#/components/responses/401'
            403:
              $ref: '#/components/responses/403'
            404:
              $ref: '#/components/responses/404'
            422:
              $ref: '#/components/responses/422'
        """
        payload = request.json
        if not isinstance(payload, dict):
            return self.response_400(message="Request body must be an object")
        payload = dict(payload)
        parent_id = payload.pop("parent_schedule_id", None)
        if isinstance(parent_id, bool) or not isinstance(parent_id, int):
            return self.response_400(
                message={"parent_schedule_id": ["An integer id is required."]}
            )
        return self._preview(parent_id, payload)
