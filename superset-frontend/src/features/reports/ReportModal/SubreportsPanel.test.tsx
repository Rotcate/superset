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
import fetchMock from 'fetch-mock';
import {
  render,
  screen,
  userEvent,
  waitFor,
  within,
  selectOption,
} from 'spec/helpers/testing-library';
import SubreportsPanel from './SubreportsPanel';

jest.mock('src/core/editors', () => ({
  EditorHost: ({
    id,
    value,
    onChange,
  }: {
    id: string;
    value: string;
    onChange: (value: string) => void;
  }) => (
    <textarea
      data-test="subreport-sql-editor"
      id={id}
      aria-label="SQL editor"
      value={value}
      onChange={event => onChange(event.target.value)}
    />
  ),
}));

const PARENT_ID = 42;
const LIST_ENDPOINT = `glob:*/api/v1/report/${PARENT_ID}/subreport/`;
const ITEM_ENDPOINT = `glob:*/api/v1/report/${PARENT_ID}/subreport/*`;
const PREVIEW_ENDPOINT = `glob:*/api/v1/report/${PARENT_ID}/subreport/execute_preview`;
const DATABASE_ENDPOINT = 'glob:*/api/v1/report/related/database*';

const contextFields = [
  {
    name: 'customer_id',
    source: 'native_filter',
    label: 'Customer',
    reference: '$F{customer_id}',
  },
  { name: 'region', source: 'chart_data', reference: '$F{region}' },
];

const savedSubreports = [
  {
    id: 2,
    name: 'Second',
    sql_query: 'SELECT 2',
    database_id: 1,
    param_mapping: {},
    position: 1,
    viz_type: 'chart',
    template: { chart_type: 'bar', x_column: 'a', y_columns: ['b'] },
  },
  {
    id: 1,
    name: 'First',
    sql_query: 'SELECT * FROM orders WHERE customer_id = :customer_id',
    database_id: 1,
    param_mapping: { customer_id: '$F{customer_id}' },
    position: 0,
    viz_type: 'table',
    template: {},
  },
];

const mockList = (result = savedSubreports) =>
  fetchMock.get(
    LIST_ENDPOINT,
    { result, context_fields: contextFields },
    { name: 'list' },
  );

const requestBody = (name: string, index = 0) =>
  JSON.parse(
    fetchMock.callHistory.calls(name)[index].options.body as string,
  ) as Record<string, unknown>;

beforeEach(() => {
  fetchMock.get(
    DATABASE_ENDPOINT,
    { result: [{ value: 1, text: 'examples' }], count: 1 },
    { name: 'databases' },
  );
});

afterEach(() => {
  fetchMock.clearHistory().removeRoutes();
});

test('asks to save the report first when the parent is not saved', () => {
  render(<SubreportsPanel />, { useRedux: true });
  expect(screen.getByText('Save the report first')).toBeInTheDocument();
  expect(
    fetchMock.callHistory
      .calls()
      .filter(call => call.url.includes('/subreport')),
  ).toHaveLength(0);
  expect(
    screen.queryByRole('button', { name: 'Add subreport' }),
  ).not.toBeInTheDocument();
});

test('lists saved subreports ordered by position', async () => {
  mockList();
  render(<SubreportsPanel parentId={PARENT_ID} />, { useRedux: true });

  const list = await screen.findByTestId('subreports-list');
  await waitFor(() =>
    expect(within(list).getAllByRole('listitem')).toHaveLength(2),
  );
  const items = within(list).getAllByRole('listitem');
  expect(items[0]).toHaveTextContent('First');
  expect(items[0]).toHaveTextContent('Table');
  expect(items[1]).toHaveTextContent('Second');
  expect(items[1]).toHaveTextContent('Chart');
  expect(screen.getByRole('button', { name: 'Move First up' })).toBeDisabled();
  expect(
    screen.getByRole('button', { name: 'Move Second down' }),
  ).toBeDisabled();
});

test('shows an empty state and load errors', async () => {
  mockList([]);
  const { unmount } = render(<SubreportsPanel parentId={PARENT_ID} />, {
    useRedux: true,
  });
  expect(await screen.findByTestId('subreports-empty')).toBeInTheDocument();
  unmount();

  fetchMock.removeRoutes({ names: ['list'] });
  fetchMock.get(LIST_ENDPOINT, {
    status: 403,
    body: { message: 'Forbidden' },
  });
  render(<SubreportsPanel parentId={PARENT_ID} />, { useRedux: true });
  expect(await screen.findByText('Subreport error')).toBeInTheDocument();
});

test('creates a subreport with a parameter mapping', async () => {
  mockList([]);
  fetchMock.post(LIST_ENDPOINT, { id: 10, result: {} }, { name: 'create' });
  render(<SubreportsPanel parentId={PARENT_ID} />, { useRedux: true });

  await userEvent.click(
    await screen.findByRole('button', { name: 'Add subreport' }),
  );
  await userEvent.type(
    screen.getByRole('textbox', { name: 'Subreport name' }),
    'Orders',
  );
  await selectOption('examples', 'Subreport database');
  await userEvent.type(
    screen.getByTestId('subreport-sql-editor'),
    'SELECT * FROM orders WHERE customer_id = :customer_id',
  );
  expect(await screen.findByText(':customer_id')).toBeInTheDocument();
  await userEvent.click(
    screen.getByRole('combobox', { name: 'Parent field for customer_id' }),
  );
  await userEvent.click(
    await screen.findByTitle('Customer (dashboard filter)'),
  );
  await userEvent.click(screen.getByRole('button', { name: 'Save subreport' }));

  await waitFor(() =>
    expect(fetchMock.callHistory.calls('create')).toHaveLength(1),
  );
  expect(requestBody('create')).toEqual({
    name: 'Orders',
    sql_query: 'SELECT * FROM orders WHERE customer_id = :customer_id',
    database_id: 1,
    param_mapping: { customer_id: '$F{customer_id}' },
    position: 0,
    viz_type: 'table',
    template: {},
  });
  expect(await screen.findByTestId('subreports-list')).toBeInTheDocument();
  expect(fetchMock.callHistory.calls('list').length).toBeGreaterThan(1);
});

test('does not save when a SQL parameter is unmapped', async () => {
  mockList([]);
  fetchMock.post(LIST_ENDPOINT, { id: 10 }, { name: 'create' });
  render(<SubreportsPanel parentId={PARENT_ID} />, { useRedux: true });

  await userEvent.click(
    await screen.findByRole('button', { name: 'Add subreport' }),
  );
  await userEvent.type(
    screen.getByRole('textbox', { name: 'Subreport name' }),
    'Orders',
  );
  await selectOption('examples', 'Subreport database');
  await userEvent.type(
    screen.getByTestId('subreport-sql-editor'),
    'SELECT * FROM orders WHERE region = :region',
  );
  await userEvent.click(screen.getByRole('button', { name: 'Save subreport' }));

  expect(
    await screen.findByText(
      'Map every SQL parameter to a parent field: region',
    ),
  ).toBeInTheDocument();
  expect(fetchMock.callHistory.calls('create')).toHaveLength(0);
});

test('edits a subreport as a chart', async () => {
  mockList();
  fetchMock.put(ITEM_ENDPOINT, { id: 2, result: {} }, { name: 'update' });
  render(<SubreportsPanel parentId={PARENT_ID} />, { useRedux: true });

  await userEvent.click(
    await screen.findByRole('button', { name: 'Edit Second' }),
  );
  const nameInput = screen.getByRole('textbox', { name: 'Subreport name' });
  await userEvent.clear(nameInput);
  await userEvent.type(nameInput, 'Revenue');
  expect(screen.getByTestId('subreport-chart-options')).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: 'Save subreport' }));

  await waitFor(() =>
    expect(fetchMock.callHistory.calls('update')).toHaveLength(1),
  );
  expect(fetchMock.callHistory.calls('update')[0].url).toMatch(
    /\/api\/v1\/report\/42\/subreport\/2$/,
  );
  expect(requestBody('update')).toMatchObject({
    name: 'Revenue',
    viz_type: 'chart',
    template: { chart_type: 'bar', x_column: 'a', y_columns: ['b'] },
  });
});

test('requires chart axes for chart subreports', async () => {
  mockList();
  fetchMock.put(ITEM_ENDPOINT, {}, { name: 'update' });
  render(<SubreportsPanel parentId={PARENT_ID} />, { useRedux: true });

  await userEvent.click(
    await screen.findByRole('button', { name: 'Edit First' }),
  );
  await userEvent.click(screen.getByRole('radio', { name: 'Chart' }));
  await userEvent.click(screen.getByRole('button', { name: 'Save subreport' }));

  expect(
    await screen.findByText(
      'Charts require an X axis column and at least one Y column.',
    ),
  ).toBeInTheDocument();
  expect(fetchMock.callHistory.calls('update')).toHaveLength(0);
});

test('shows backend validation errors on save', async () => {
  mockList();
  fetchMock.put(
    ITEM_ENDPOINT,
    {
      status: 422,
      body: { message: { sql_query: ['Only SELECT statements are allowed'] } },
    },
    { name: 'update' },
  );
  render(<SubreportsPanel parentId={PARENT_ID} />, { useRedux: true });

  await userEvent.click(
    await screen.findByRole('button', { name: 'Edit First' }),
  );
  await userEvent.click(screen.getByRole('button', { name: 'Save subreport' }));

  expect(
    await screen.findByText('Only SELECT statements are allowed'),
  ).toBeInTheDocument();
  expect(screen.getByTestId('subreport-form')).toBeInTheDocument();
});

test('deletes a subreport after confirmation', async () => {
  mockList();
  fetchMock.delete(ITEM_ENDPOINT, {}, { name: 'delete' });
  render(<SubreportsPanel parentId={PARENT_ID} />, { useRedux: true });

  await userEvent.click(
    await screen.findByRole('button', { name: 'Delete First' }),
  );
  expect(fetchMock.callHistory.calls('delete')).toHaveLength(0);
  await userEvent.click(await screen.findByRole('button', { name: 'Delete' }));

  await waitFor(() =>
    expect(fetchMock.callHistory.calls('delete')).toHaveLength(1),
  );
  expect(fetchMock.callHistory.calls('delete')[0].url).toMatch(
    /\/api\/v1\/report\/42\/subreport\/1$/,
  );
});

test('reorders subreports by updating positions', async () => {
  mockList();
  fetchMock.put(ITEM_ENDPOINT, {}, { name: 'update' });
  render(<SubreportsPanel parentId={PARENT_ID} />, { useRedux: true });

  await userEvent.click(
    await screen.findByRole('button', { name: 'Move First down' }),
  );

  await waitFor(() =>
    expect(fetchMock.callHistory.calls('update')).toHaveLength(2),
  );
  const updates = fetchMock.callHistory
    .calls('update')
    .map(call => [
      call.url.split('/').pop(),
      JSON.parse(call.options.body as string),
    ]);
  expect(updates).toEqual([
    ['2', { position: 0 }],
    ['1', { position: 1 }],
  ]);
});

test('previews a subreport with explicit parameter values', async () => {
  mockList();
  fetchMock.post(
    PREVIEW_ENDPOINT,
    {
      result: {
        columns: ['customer_id', { name: 'total', type: 'float64' }],
        data: [
          { customer_id: 7, total: 10.5 },
          { customer_id: 8, total: null },
        ],
        row_count: 2,
      },
    },
    { name: 'preview' },
  );
  render(<SubreportsPanel parentId={PARENT_ID} />, { useRedux: true });

  await userEvent.click(
    await screen.findByRole('button', { name: 'Edit First' }),
  );
  await userEvent.type(
    screen.getByRole('textbox', { name: 'Preview value for customer_id' }),
    '7, 8',
  );
  await userEvent.click(screen.getByRole('button', { name: 'Preview' }));

  const result = await screen.findByTestId('subreport-preview-result');
  expect(within(result).getByText('2 rows')).toBeInTheDocument();
  expect(within(result).getByText('10.5')).toBeInTheDocument();
  expect(within(result).getByText('NULL')).toBeInTheDocument();
  expect(requestBody('preview')).toEqual({
    database_id: 1,
    sql_query: 'SELECT * FROM orders WHERE customer_id = :customer_id',
    param_mapping: { customer_id: '$F{customer_id}' },
    values: { customer_id: [7, 8] },
    row_limit: 100,
  });
});

test('shows preview errors from the backend', async () => {
  mockList();
  fetchMock.post(
    PREVIEW_ENDPOINT,
    {
      status: 400,
      body: { message: 'Multiple values for customer_id require IN (...)' },
    },
    { name: 'preview' },
  );
  render(<SubreportsPanel parentId={PARENT_ID} />, { useRedux: true });

  await userEvent.click(
    await screen.findByRole('button', { name: 'Edit First' }),
  );
  await userEvent.click(screen.getByRole('button', { name: 'Preview' }));

  expect(await screen.findByText('Preview failed')).toBeInTheDocument();
  expect(
    screen.getByText('Multiple values for customer_id require IN (...)'),
  ).toBeInTheDocument();
});

test('blocks preview when a parameter is unmapped', async () => {
  mockList();
  fetchMock.post(PREVIEW_ENDPOINT, {}, { name: 'preview' });
  render(<SubreportsPanel parentId={PARENT_ID} />, { useRedux: true });

  await userEvent.click(
    await screen.findByRole('button', { name: 'Edit Second' }),
  );
  const editor = screen.getByTestId('subreport-sql-editor');
  await userEvent.clear(editor);
  await userEvent.type(editor, 'SELECT * FROM t WHERE r = :region');
  await userEvent.click(screen.getByRole('button', { name: 'Preview' }));

  expect(
    await screen.findByText(
      'Map every SQL parameter to a parent field: region',
    ),
  ).toBeInTheDocument();
  expect(fetchMock.callHistory.calls('preview')).toHaveLength(0);
});
