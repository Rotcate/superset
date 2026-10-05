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
import rison from 'rison';
import { SupersetClient } from '@superset-ui/core';
import {
  SubreportContextField,
  SubreportListResponse,
  SubreportObject,
  SubreportParamMapping,
  SubreportPreviewColumn,
  SubreportPreviewPayload,
  SubreportPreviewResult,
  SubreportPreviewValue,
} from 'src/features/reports/types';

export interface DatabaseOption {
  value: number;
  label: string;
}

interface RelatedDatabaseResponse {
  result: { value: number; text: string }[];
  count: number;
}

const FIELD_REFERENCE_RE = /^\$F\{([^{}]+)\}$/;
const PLACEHOLDER_RE = /(?<![:\w]):([A-Za-z_][A-Za-z0-9_]*)/g;
// Strings, quoted identifiers and comments cannot contain bind parameters.
const NON_CODE_RE =
  /'(?:[^']|'')*'|"(?:[^"]|"")*"|`(?:[^`]|``)*`|--[^\n]*|\/\*[\s\S]*?\*\//g;

export const subreportEndpoint = (parentId: number, subreportId?: number) =>
  `/api/v1/report/${parentId}/subreport/${
    subreportId === undefined ? '' : `${subreportId}`
  }`;

export const toFieldReference = (fieldName: string) => `$F{${fieldName}}`;

export const fromFieldReference = (reference: string): string | undefined =>
  FIELD_REFERENCE_RE.exec(reference)?.[1];

/**
 * Best-effort extraction of named `:param` placeholders used to drive the
 * mapping UI. The backend parses the SQL in the engine dialect and remains
 * the source of truth.
 */
export const getSqlParameters = (sql: string): string[] => {
  const code = sql.replace(NON_CODE_RE, ' ');
  const names = new Set<string>();
  for (const match of code.matchAll(PLACEHOLDER_RE)) {
    names.add(match[1]);
  }
  return [...names];
};

/**
 * Parses a preview value typed by the user. Commas separate multiple values;
 * numbers are sent as numbers only when they round-trip unchanged so values
 * such as zip codes with leading zeros stay strings.
 */
export const parsePreviewValue = (
  raw: string,
): SubreportPreviewValue | undefined => {
  const parts = raw
    .split(',')
    .map(part => part.trim())
    .filter(part => part.length > 0)
    .map(part => {
      const asNumber = Number(part);
      return Number.isFinite(asNumber) && String(asNumber) === part
        ? asNumber
        : part;
    });
  if (parts.length === 0) {
    return undefined;
  }
  return parts.length === 1 ? parts[0] : parts;
};

export const getPreviewColumnNames = (
  columns: SubreportPreviewResult['columns'],
): string[] =>
  columns.map(column =>
    typeof column === 'string'
      ? column
      : (column as SubreportPreviewColumn).name,
  );

export const sortSubreports = (subreports: SubreportObject[]) =>
  [...subreports].sort(
    (a, b) => a.position - b.position || (a.id ?? 0) - (b.id ?? 0),
  );

export const toSubreportPayload = (
  subreport: SubreportObject,
): Omit<SubreportObject, 'id' | 'uuid' | 'parent_schedule_id'> => ({
  name: subreport.name,
  sql_query: subreport.sql_query,
  database_id: subreport.database_id,
  param_mapping: subreport.param_mapping,
  position: subreport.position,
  viz_type: subreport.viz_type,
  template: subreport.template,
});

export async function fetchSubreports(parentId: number): Promise<{
  subreports: SubreportObject[];
  contextFields: SubreportContextField[];
}> {
  const { json } = await SupersetClient.get({
    endpoint: subreportEndpoint(parentId),
  });
  const response = json as SubreportListResponse;
  return {
    subreports: sortSubreports(response.result ?? []),
    contextFields: response.context_fields ?? [],
  };
}

export async function createSubreport(
  parentId: number,
  subreport: SubreportObject,
): Promise<SubreportObject> {
  const { json } = await SupersetClient.post({
    endpoint: subreportEndpoint(parentId),
    jsonPayload: toSubreportPayload(subreport),
  });
  const { id, result } = json as { id?: number; result?: SubreportObject };
  return { ...subreport, ...result, id: id ?? result?.id ?? subreport.id };
}

export async function updateSubreport(
  parentId: number,
  subreportId: number,
  changes: Partial<SubreportObject>,
): Promise<void> {
  await SupersetClient.put({
    endpoint: subreportEndpoint(parentId, subreportId),
    jsonPayload: changes,
  });
}

export async function deleteSubreport(
  parentId: number,
  subreportId: number,
): Promise<void> {
  await SupersetClient.delete({
    endpoint: subreportEndpoint(parentId, subreportId),
  });
}

export async function previewSubreport(
  parentId: number,
  payload: SubreportPreviewPayload,
): Promise<SubreportPreviewResult> {
  const { json } = await SupersetClient.post({
    endpoint: `${subreportEndpoint(parentId)}execute_preview`,
    jsonPayload: payload,
  });
  return (json as { result: SubreportPreviewResult }).result;
}

/**
 * Loads databases through the report API's related endpoint, which applies
 * the `DatabaseFilter` so only databases the user may access are listed.
 */
export async function fetchDatabaseOptions(
  filter: string,
  page: number,
  pageSize: number,
  includeId?: number | null,
): Promise<{ data: DatabaseOption[]; totalCount: number }> {
  const query = rison.encode({
    filter,
    page,
    page_size: pageSize,
    ...(includeId ? { include_ids: [includeId] } : {}),
  });
  const { json } = await SupersetClient.get({
    endpoint: `/api/v1/report/related/database?q=${query}`,
  });
  const response = json as RelatedDatabaseResponse;
  return {
    data: response.result.map(item => ({
      value: item.value,
      label: item.text,
    })),
    totalCount: response.count,
  };
}

export const buildParamMapping = (
  parameters: string[],
  mapping: SubreportParamMapping,
): SubreportParamMapping =>
  Object.fromEntries(
    parameters
      .filter(parameter => mapping[parameter])
      .map(parameter => [parameter, mapping[parameter]]),
  );
