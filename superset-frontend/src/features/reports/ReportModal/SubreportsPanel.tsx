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
import { useCallback, useEffect, useState } from 'react';
import { t } from '@apache-superset/core/translation';
import { getClientErrorObject } from '@superset-ui/core';
import { Alert } from '@apache-superset/core/components';
import { styled, css } from '@apache-superset/core/theme';
import { Button, Loading, Popconfirm } from '@superset-ui/core/components';
import { Typography } from '@superset-ui/core/components/Typography';
import {
  SubreportContextField,
  SubreportObject,
  SubreportVizType,
} from 'src/features/reports/types';
import SubreportForm from './SubreportForm';
import {
  deleteSubreport,
  fetchSubreports,
  updateSubreport,
} from './subreportApi';

export interface SubreportsPanelProps {
  /** Saved parent report id; subreports can only be managed once it exists. */
  parentId?: number;
}

const StyledPanel = styled.div`
  ${({ theme }) => css`
    padding: ${theme.sizeUnit * 4}px;
    display: flex;
    flex-direction: column;
    gap: ${theme.sizeUnit * 3}px;
  `}
`;

const StyledList = styled.ol`
  ${({ theme }) => css`
    list-style: none;
    margin: 0;
    padding: 0;

    li {
      display: flex;
      align-items: center;
      gap: ${theme.sizeUnit * 2}px;
      padding: ${theme.sizeUnit * 2}px 0;
      border-bottom: 1px solid ${theme.colorSplit};
    }
    .subreport-name {
      flex: 1 1 auto;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .subreport-type {
      color: ${theme.colorTextSecondary};
    }
  `}
`;

const newSubreport = (position: number): SubreportObject => ({
  name: '',
  sql_query: '',
  database_id: null,
  param_mapping: {},
  position,
  viz_type: SubreportVizType.Table,
  template: {},
});

export default function SubreportsPanel({ parentId }: SubreportsPanelProps) {
  const [subreports, setSubreports] = useState<SubreportObject[]>([]);
  const [contextFields, setContextFields] = useState<SubreportContextField[]>(
    [],
  );
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string>();
  const [editing, setEditing] = useState<SubreportObject | null>(null);
  const [isMutating, setIsMutating] = useState(false);

  const load = useCallback(async () => {
    if (parentId === undefined) {
      return;
    }
    setIsLoading(true);
    try {
      const result = await fetchSubreports(parentId);
      setSubreports(result.subreports);
      setContextFields(result.contextFields);
      setError(undefined);
    } catch (e) {
      const { error: message } = await getClientErrorObject(e);
      setError(message || t('Failed to load subreports'));
    }
    setIsLoading(false);
  }, [parentId]);

  useEffect(() => {
    setEditing(null);
    load();
  }, [load]);

  if (parentId === undefined) {
    return (
      <StyledPanel data-test="subreports-panel">
        <Alert
          type="info"
          message={t('Save the report first')}
          description={t(
            'Subreports can be added after the report has been saved. Save this report, then reopen it to add subreports.',
          )}
        />
      </StyledPanel>
    );
  }

  const runMutation = async (
    mutation: () => Promise<void>,
    fallbackMessage: string,
  ) => {
    setIsMutating(true);
    setError(undefined);
    try {
      await mutation();
    } catch (e) {
      const { error: message } = await getClientErrorObject(e);
      setError(message || fallbackMessage);
    }
    await load();
    setIsMutating(false);
  };

  const onMove = (index: number, offset: -1 | 1) => {
    const reordered = [...subreports];
    const [moved] = reordered.splice(index, 1);
    reordered.splice(index + offset, 0, moved);
    const changed = reordered
      .map((subreport, position) => ({ subreport, position }))
      .filter(
        ({ subreport, position }) =>
          subreport.id !== undefined && subreport.position !== position,
      );
    setSubreports(
      reordered.map((subreport, position) => ({ ...subreport, position })),
    );
    return runMutation(async () => {
      // Sequential updates keep positions consistent if one request fails.
      for (const { subreport, position } of changed) {
        // eslint-disable-next-line no-await-in-loop
        await updateSubreport(parentId, subreport.id as number, { position });
      }
    }, t('Failed to reorder subreports'));
  };

  const onDelete = (subreport: SubreportObject) =>
    runMutation(
      () => deleteSubreport(parentId, subreport.id as number),
      t('Failed to delete subreport'),
    );

  if (editing) {
    return (
      <StyledPanel data-test="subreports-panel">
        <Typography.Title level={5}>
          {editing.id === undefined ? t('Add subreport') : t('Edit subreport')}
        </Typography.Title>
        <SubreportForm
          parentId={parentId}
          subreport={editing}
          contextFields={contextFields}
          onCancel={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            load();
          }}
        />
      </StyledPanel>
    );
  }

  const nextPosition = subreports.length
    ? Math.max(...subreports.map(subreport => subreport.position)) + 1
    : 0;

  return (
    <StyledPanel data-test="subreports-panel">
      <div>
        {t(
          'Subreports run parameterized SQL queries using values from this report and render them, in order, in the notification with a CSV export.',
        )}
      </div>
      {error && (
        <Alert
          type="error"
          message={t('Subreport error')}
          description={error}
        />
      )}
      {isLoading && !subreports.length ? (
        <Loading position="inline" />
      ) : (
        <>
          {!subreports.length && !error && (
            <div data-test="subreports-empty">
              {t('This report has no subreports.')}
            </div>
          )}
          <StyledList data-test="subreports-list">
            {subreports.map((subreport, index) => (
              <li key={subreport.id ?? index}>
                <span className="subreport-name">{subreport.name}</span>
                <span className="subreport-type">
                  {subreport.viz_type === SubreportVizType.Chart
                    ? t('Chart')
                    : t('Table')}
                </span>
                <Button
                  buttonSize="xsmall"
                  buttonStyle="secondary"
                  aria-label={t('Move %s up', subreport.name)}
                  disabled={index === 0 || isMutating}
                  onClick={() => onMove(index, -1)}
                >
                  {t('Up')}
                </Button>
                <Button
                  buttonSize="xsmall"
                  buttonStyle="secondary"
                  aria-label={t('Move %s down', subreport.name)}
                  disabled={index === subreports.length - 1 || isMutating}
                  onClick={() => onMove(index, 1)}
                >
                  {t('Down')}
                </Button>
                <Button
                  buttonSize="xsmall"
                  buttonStyle="secondary"
                  aria-label={t('Edit %s', subreport.name)}
                  disabled={isMutating}
                  onClick={() => setEditing(subreport)}
                >
                  {t('Edit')}
                </Button>
                <Popconfirm
                  title={t('Delete subreport %s?', subreport.name)}
                  okText={t('Delete')}
                  cancelText={t('Cancel')}
                  onConfirm={() => onDelete(subreport)}
                >
                  <Button
                    buttonSize="xsmall"
                    buttonStyle="danger"
                    aria-label={t('Delete %s', subreport.name)}
                    disabled={isMutating}
                  >
                    {t('Delete')}
                  </Button>
                </Popconfirm>
              </li>
            ))}
          </StyledList>
        </>
      )}
      <div>
        <Button
          buttonSize="small"
          buttonStyle="primary"
          disabled={isMutating}
          onClick={() => setEditing(newSubreport(nextPosition))}
        >
          {t('Add subreport')}
        </Button>
      </div>
    </StyledPanel>
  );
}
