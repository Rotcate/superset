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
import { useCallback, useMemo, useState, ChangeEvent } from 'react';
import { t } from '@apache-superset/core/translation';
import { getClientErrorObject } from '@superset-ui/core';
import { Alert } from '@apache-superset/core/components';
import { styled, css } from '@apache-superset/core/theme';
import {
  AsyncSelect,
  Button,
  EmptyWrapperType,
  Input,
  InfoTooltip,
  Select,
  TableView,
} from '@superset-ui/core/components';
import { Radio, RadioChangeEvent } from '@superset-ui/core/components/Radio';
import { EditorHost } from 'src/core/editors';
import {
  SubreportChartType,
  SubreportContextField,
  SubreportObject,
  SubreportPreviewRecord,
  SubreportPreviewResult,
  SubreportPreviewValue,
  SubreportTemplate,
  SubreportVizType,
} from 'src/features/reports/types';
import {
  buildParamMapping,
  createSubreport,
  fetchDatabaseOptions,
  fromFieldReference,
  getPreviewColumnNames,
  getSqlParameters,
  parsePreviewValue,
  previewSubreport,
  toFieldReference,
  updateSubreport,
  toSubreportPayload,
} from './subreportApi';

export interface SubreportFormProps {
  parentId: number;
  subreport: SubreportObject;
  contextFields: SubreportContextField[];
  onCancel: () => void;
  onSaved: () => void;
}

const PREVIEW_ROW_LIMIT = 100;

const StyledForm = styled.div`
  ${({ theme }) => css`
    display: flex;
    flex-direction: column;
    gap: ${theme.sizeUnit * 3}px;

    .control-label {
      font-size: ${theme.fontSizeSM}px;
      color: ${theme.colorTextSecondary};
      margin-bottom: ${theme.sizeUnit}px;
    }
    .required {
      color: ${theme.colorError};
      margin-left: ${theme.sizeUnit / 2}px;
    }
    .ant-select {
      width: 100%;
    }
  `}
`;

const StyledRow = styled.div`
  ${({ theme }) => css`
    display: flex;
    align-items: center;
    gap: ${theme.sizeUnit * 2}px;

    > :first-child {
      flex: 0 0 30%;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    > :last-child {
      flex: 1 1 auto;
    }
  `}
`;

const StyledActions = styled.div`
  ${({ theme }) => css`
    display: flex;
    justify-content: flex-end;
    gap: ${theme.sizeUnit * 2}px;
  `}
`;

const StyledPreview = styled.div`
  ${({ theme }) => css`
    max-height: ${theme.sizeUnit * 75}px;
    overflow: auto;
  `}
`;

const formatCell = (value: unknown): string => {
  if (value === null || value === undefined) {
    return 'NULL';
  }
  if (typeof value === 'object') {
    return JSON.stringify(value);
  }
  return String(value);
};

const fieldLabel = (field: SubreportContextField) => {
  const label = field.label && field.label !== field.name ? field.label : '';
  const source =
    field.source === 'native_filter'
      ? t('dashboard filter')
      : field.source === 'chart_data'
        ? t('chart data')
        : field.source;
  return [label || field.name, source ? `(${source})` : '']
    .filter(Boolean)
    .join(' ');
};

export default function SubreportForm({
  parentId,
  subreport,
  contextFields,
  onCancel,
  onSaved,
}: SubreportFormProps) {
  const [draft, setDraft] = useState<SubreportObject>(subreport);
  const [isSaving, setIsSaving] = useState(false);
  const [saveError, setSaveError] = useState<string>();
  const [previewInputs, setPreviewInputs] = useState<Record<string, string>>(
    {},
  );
  const [isPreviewing, setIsPreviewing] = useState(false);
  const [previewError, setPreviewError] = useState<string>();
  const [previewResult, setPreviewResult] = useState<SubreportPreviewResult>();

  const update = (changes: Partial<SubreportObject>) =>
    setDraft(current => ({ ...current, ...changes }));
  const updateTemplate = (changes: Partial<SubreportTemplate>) =>
    setDraft(current => ({
      ...current,
      template: { ...current.template, ...changes },
    }));

  const parameters = useMemo(
    () => getSqlParameters(draft.sql_query),
    [draft.sql_query],
  );
  const paramMapping = useMemo(
    () => buildParamMapping(parameters, draft.param_mapping),
    [parameters, draft.param_mapping],
  );
  const unmappedParameters = parameters.filter(
    parameter => !paramMapping[parameter],
  );
  const mappedFields = useMemo(
    () =>
      [...new Set(Object.values(paramMapping).map(fromFieldReference))].filter(
        (field): field is string => !!field,
      ),
    [paramMapping],
  );
  const fieldOptions = useMemo(
    () =>
      contextFields.map(field => ({
        value: field.reference || toFieldReference(field.name),
        label: fieldLabel(field),
      })),
    [contextFields],
  );
  const previewColumns = useMemo(
    () => (previewResult ? getPreviewColumnNames(previewResult.columns) : []),
    [previewResult],
  );
  const columnOptions = previewColumns.map(column => ({
    value: column,
    label: column,
  }));
  const isChart = draft.viz_type === SubreportVizType.Chart;

  const loadDatabases = useCallback(
    (filter: string, page: number, pageSize: number) =>
      fetchDatabaseOptions(filter, page, pageSize, subreport.database_id),
    [subreport.database_id],
  );

  const getValidationError = (): string | undefined => {
    if (!draft.name.trim()) {
      return t('Name is required.');
    }
    if (!draft.database_id) {
      return t('Database is required.');
    }
    if (!draft.sql_query.trim()) {
      return t('SQL query is required.');
    }
    if (unmappedParameters.length) {
      return t(
        'Map every SQL parameter to a parent field: %s',
        unmappedParameters.join(', '),
      );
    }
    if (
      isChart &&
      (!draft.template.x_column || !draft.template.y_columns?.length)
    ) {
      return t('Charts require an X axis column and at least one Y column.');
    }
    return undefined;
  };

  const buildTemplate = (): SubreportTemplate => {
    const { title, chart_type, x_column, y_columns, columns, max_rows } =
      draft.template;
    const base: SubreportTemplate = {
      ...(title ? { title } : {}),
      ...(max_rows ? { max_rows } : {}),
    };
    if (isChart) {
      return {
        ...base,
        chart_type: chart_type || SubreportChartType.Bar,
        x_column,
        y_columns,
      };
    }
    return { ...base, ...(columns?.length ? { columns } : {}) };
  };

  const onSave = async () => {
    const validationError = getValidationError();
    if (validationError) {
      setSaveError(validationError);
      return;
    }
    const payload: SubreportObject = {
      ...draft,
      name: draft.name.trim(),
      param_mapping: paramMapping,
      template: buildTemplate(),
    };
    setIsSaving(true);
    setSaveError(undefined);
    try {
      if (draft.id === undefined) {
        await createSubreport(parentId, payload);
      } else {
        await updateSubreport(parentId, draft.id, toSubreportPayload(payload));
      }
      setIsSaving(false);
      onSaved();
    } catch (error) {
      const { error: message } = await getClientErrorObject(error);
      setSaveError(message || t('Failed to save subreport'));
      setIsSaving(false);
    }
  };

  const onPreview = async () => {
    if (!draft.database_id || !draft.sql_query.trim()) {
      setPreviewError(t('Select a database and enter a SQL query to preview.'));
      return;
    }
    if (unmappedParameters.length) {
      setPreviewError(
        t(
          'Map every SQL parameter to a parent field: %s',
          unmappedParameters.join(', '),
        ),
      );
      return;
    }
    const values: Record<string, SubreportPreviewValue> = {};
    mappedFields.forEach(field => {
      const value = parsePreviewValue(previewInputs[field] ?? '');
      if (value !== undefined) {
        values[field] = value;
      }
    });
    setIsPreviewing(true);
    setPreviewError(undefined);
    setPreviewResult(undefined);
    try {
      const result = await previewSubreport(parentId, {
        database_id: draft.database_id,
        sql_query: draft.sql_query,
        param_mapping: paramMapping,
        values,
        row_limit: PREVIEW_ROW_LIMIT,
      });
      setPreviewResult(result);
    } catch (error) {
      const { error: message } = await getClientErrorObject(error);
      setPreviewError(message || t('Failed to preview subreport'));
    }
    setIsPreviewing(false);
  };

  // Column names may contain dots, which the table treats as nested paths, so
  // rows are re-keyed by column index.
  const tableColumns = useMemo(
    () =>
      previewColumns.map((column, index) => ({
        id: `column_${index}`,
        accessor: `column_${index}`,
        Header: column,
      })),
    [previewColumns],
  );
  const tableData = useMemo(
    () =>
      (previewResult?.data ?? []).map((row: SubreportPreviewRecord) =>
        Object.fromEntries(
          previewColumns.map((column, index) => [
            `column_${index}`,
            formatCell(row[column]),
          ]),
        ),
      ),
    [previewResult, previewColumns],
  );

  return (
    <StyledForm data-test="subreport-form">
      <div>
        <div className="control-label">
          {t('Name')}
          <span className="required">*</span>
        </div>
        <Input
          name="subreport-name"
          aria-label={t('Subreport name')}
          value={draft.name}
          onChange={(event: ChangeEvent<HTMLInputElement>) =>
            update({ name: event.target.value })
          }
        />
      </div>
      <div>
        <div className="control-label">
          {t('Database')}
          <span className="required">*</span>
        </div>
        <AsyncSelect
          ariaLabel={t('Subreport database')}
          placeholder={t('Select database')}
          lazyLoading={false}
          value={draft.database_id ?? undefined}
          options={loadDatabases}
          onChange={value =>
            update({
              database_id:
                typeof value === 'number'
                  ? value
                  : ((value as { value?: number } | null)?.value ?? null),
            })
          }
        />
      </div>
      <div>
        <div className="control-label">
          {t('SQL query')}
          <InfoTooltip
            tooltip={t(
              'A single read-only SELECT. Use named parameters such as :customer_id and map them to parent fields below.',
            )}
          />
          <span className="required">*</span>
        </div>
        <EditorHost
          id={`subreport-sql-editor-${draft.id ?? 'new'}`}
          language="sql"
          value={draft.sql_query}
          onChange={(value: string) => update({ sql_query: value })}
          height="160px"
          width="100%"
        />
      </div>
      <div data-test="subreport-param-mapping">
        <div className="control-label">{t('Parameter mapping')}</div>
        {parameters.length === 0 && (
          <div>{t('The SQL query has no :parameters.')}</div>
        )}
        {parameters.length > 0 && contextFields.length === 0 && (
          <Alert
            type="warning"
            message={t(
              'The parent report has no available fields to map parameters to. Configure dashboard native filters or use a chart with data columns.',
            )}
          />
        )}
        {parameters.map(parameter => (
          <StyledRow key={parameter}>
            <code>:{parameter}</code>
            <Select
              ariaLabel={t('Parent field for %s', parameter)}
              placeholder={t('Select parent field')}
              options={fieldOptions}
              value={paramMapping[parameter]}
              onChange={value =>
                update({
                  param_mapping: {
                    ...draft.param_mapping,
                    [parameter]: String(value ?? ''),
                  },
                })
              }
            />
          </StyledRow>
        ))}
      </div>
      <div>
        <div className="control-label">{t('Visualization')}</div>
        <Radio.GroupWrapper
          value={draft.viz_type}
          onChange={(event: RadioChangeEvent) =>
            update({ viz_type: event.target.value })
          }
          options={[
            { label: t('Table'), value: SubreportVizType.Table },
            { label: t('Chart'), value: SubreportVizType.Chart },
          ]}
        />
      </div>
      <div>
        <div className="control-label">{t('Title')}</div>
        <Input
          aria-label={t('Subreport title')}
          value={draft.template.title ?? ''}
          onChange={(event: ChangeEvent<HTMLInputElement>) =>
            updateTemplate({ title: event.target.value })
          }
        />
      </div>
      {isChart && (
        <div data-test="subreport-chart-options">
          <StyledRow>
            <span>{t('Chart type')}</span>
            <Select
              ariaLabel={t('Chart type')}
              options={[
                { value: SubreportChartType.Bar, label: t('Bar') },
                { value: SubreportChartType.Line, label: t('Line') },
              ]}
              value={draft.template.chart_type || SubreportChartType.Bar}
              onChange={value =>
                updateTemplate({ chart_type: value as SubreportChartType })
              }
            />
          </StyledRow>
          <StyledRow>
            <span>{t('X axis')}</span>
            <Select
              ariaLabel={t('X axis column')}
              allowNewOptions
              options={columnOptions}
              value={draft.template.x_column}
              placeholder={t('Column name')}
              onChange={value =>
                updateTemplate({
                  x_column: value ? String(value) : undefined,
                })
              }
            />
          </StyledRow>
          <StyledRow>
            <span>{t('Y axis')}</span>
            <Select
              ariaLabel={t('Y axis columns')}
              mode="multiple"
              allowNewOptions
              options={columnOptions}
              value={draft.template.y_columns ?? []}
              placeholder={t('Column names')}
              onChange={value =>
                updateTemplate({
                  y_columns: (Array.isArray(value) ? value : [value])
                    .filter(item => item !== null && item !== undefined)
                    .map(String),
                })
              }
            />
          </StyledRow>
        </div>
      )}
      <div data-test="subreport-preview">
        <div className="control-label">
          {t('Preview parameters')}
          <InfoTooltip
            tooltip={t(
              'Example parent values used only for this preview. Separate multiple values with commas.',
            )}
          />
        </div>
        {mappedFields.map(field => (
          <StyledRow key={field}>
            <code>{toFieldReference(field)}</code>
            <Input
              aria-label={t('Preview value for %s', field)}
              value={previewInputs[field] ?? ''}
              onChange={(event: ChangeEvent<HTMLInputElement>) =>
                setPreviewInputs(current => ({
                  ...current,
                  [field]: event.target.value,
                }))
              }
            />
          </StyledRow>
        ))}
        <Button
          buttonSize="small"
          buttonStyle="secondary"
          onClick={onPreview}
          loading={isPreviewing}
        >
          {t('Preview')}
        </Button>
      </div>
      {previewError && (
        <Alert
          type="error"
          message={t('Preview failed')}
          description={previewError}
        />
      )}
      {previewResult && (
        <StyledPreview data-test="subreport-preview-result">
          <div>
            {previewResult.truncated
              ? t('Showing the first %s rows', previewResult.row_count)
              : t('%s rows', previewResult.row_count)}
          </div>
          <TableView
            columns={tableColumns}
            data={tableData}
            emptyWrapperType={EmptyWrapperType.Small}
            noDataText={t('No rows returned')}
            withPagination={false}
            small
          />
        </StyledPreview>
      )}
      {saveError && (
        <Alert
          type="error"
          message={t('Failed to save subreport')}
          description={saveError}
        />
      )}
      <StyledActions>
        <Button buttonSize="small" buttonStyle="secondary" onClick={onCancel}>
          {t('Cancel')}
        </Button>
        <Button
          buttonSize="small"
          buttonStyle="primary"
          onClick={onSave}
          loading={isSaving}
        >
          {t('Save subreport')}
        </Button>
      </StyledActions>
    </StyledForm>
  );
}
