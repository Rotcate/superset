/**
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.  The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License.  You may obtain a copy of the License at
 *
 *   http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing,
 * software distributed under the License is distributed on an
 * "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 * KIND, either express or implied.  See the License for the
 * specific language governing permissions and limitations
 * under the License.
 */

/**
 * Types mirroring enums in `superset/reports/models.py`:
 */
export type ReportScheduleType = 'Alert' | 'Report';
export type ReportCreationMethod = 'charts' | 'dashboards' | 'alerts_reports';

export type ReportRecipientType = 'Email' | 'Slack' | 'Webhook';

export enum ReportType {
  Dashboards = 'dashboards',
  Charts = 'charts',
}

export enum NotificationFormats {
  Text = 'TEXT',
  PNG = 'PNG',
  CSV = 'CSV',
  XLSX = 'XLSX',
}
export interface ReportObject {
  id?: number;
  active: boolean;
  crontab: string;
  dashboard?: number;
  chart?: number;
  dashboard_id?: number | null;
  chart_id?: number | null;
  description?: string;
  log_retention: number;
  name: string;
  recipients: [
    {
      recipient_config_json: {
        target: string;
        ccTarget: string;
        bccTarget: string;
      };
      type: ReportRecipientType;
    },
  ];
  report_format: string;
  timezone: string;
  type: ReportScheduleType;
  validator_config_json: {} | null;
  validator_type: string;
  working_timeout: number;
  creation_method: string;
  force_screenshot: boolean;
  editors?: number[];
  custom_width?: number | null;
  error?: string;
  retry_on_failure?: boolean;
  retry_max_attempts?: number;
  send_failed_reports?: boolean;
  retry_notify_owners?: boolean;
  retry_notify_recipients?: boolean;
  parent_schedule_id?: number | null;
  subreports?: SubreportObject[];
}

/**
 * Subreports: parameterized SQL queries rendered inside a parent report's
 * notification. Mirrors `Subreport` in `superset/reports/models.py`.
 */
export enum SubreportVizType {
  Table = 'table',
  Chart = 'chart',
}

export enum SubreportChartType {
  Bar = 'bar',
  Line = 'line',
}

export interface SubreportTemplate {
  title?: string;
  columns?: string[];
  max_rows?: number;
  chart_type?: SubreportChartType;
  x_column?: string;
  y_columns?: string[];
}

/**
 * Maps a named SQL placeholder (`:customer_id`) to a parent field reference
 * (`$F{customer_id}`).
 */
export type SubreportParamMapping = Record<string, string>;

export interface SubreportObject {
  id?: number;
  uuid?: string;
  parent_schedule_id?: number;
  name: string;
  sql_query: string;
  database_id: number | null;
  param_mapping: SubreportParamMapping;
  position: number;
  viz_type: SubreportVizType;
  template: SubreportTemplate;
}

export type SubreportContextFieldSource = 'native_filter' | 'chart_data';

export interface SubreportContextField {
  name: string;
  source?: SubreportContextFieldSource | string;
  label?: string | null;
  filter_id?: string | null;
  filter_type?: string | null;
  reference?: string;
}

export interface SubreportListResponse {
  result: SubreportObject[];
  context_fields?: SubreportContextField[];
}

export type SubreportPreviewValue =
  | string
  | number
  | boolean
  | null
  | Array<string | number | boolean | null>;

export interface SubreportPreviewPayload {
  database_id: number;
  sql_query: string;
  param_mapping: SubreportParamMapping;
  values: Record<string, SubreportPreviewValue>;
  row_limit?: number;
}

export interface SubreportPreviewColumn {
  name: string;
  type?: string;
}

export type SubreportPreviewRecord = Record<string, unknown>;

export interface SubreportPreviewResult {
  columns: Array<string | SubreportPreviewColumn>;
  data: SubreportPreviewRecord[];
  row_count: number;
  truncated?: boolean;
}
