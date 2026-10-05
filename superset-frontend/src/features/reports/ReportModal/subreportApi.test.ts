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
import {
  buildParamMapping,
  fromFieldReference,
  getPreviewColumnNames,
  getSqlParameters,
  parsePreviewValue,
  sortSubreports,
  subreportEndpoint,
  toFieldReference,
} from './subreportApi';
import { SubreportObject, SubreportVizType } from '../types';

const subreport = (id: number, position: number): SubreportObject => ({
  id,
  name: `Subreport ${id}`,
  sql_query: 'SELECT 1',
  database_id: 1,
  param_mapping: {},
  position,
  viz_type: SubreportVizType.Table,
  template: {},
});

test('builds subreport endpoints', () => {
  expect(subreportEndpoint(3)).toBe('/api/v1/report/3/subreport/');
  expect(subreportEndpoint(3, 7)).toBe('/api/v1/report/3/subreport/7');
});

test('converts field references', () => {
  expect(toFieldReference('customer_id')).toBe('$F{customer_id}');
  expect(fromFieldReference('$F{customer_id}')).toBe('customer_id');
  expect(fromFieldReference('customer_id')).toBeUndefined();
});

test('extracts named parameters ignoring strings, comments and casts', () => {
  const sql = `
    SELECT a::text, ':not_param' AS s, "col:quoted"
    FROM orders -- :commented
    /* :block */
    WHERE customer_id = :customer_id AND region IN (:region)
      AND other = :customer_id`;
  expect(getSqlParameters(sql)).toEqual(['customer_id', 'region']);
});

test('parses preview values into scalars and lists', () => {
  expect(parsePreviewValue('')).toBeUndefined();
  expect(parsePreviewValue(' 42 ')).toBe(42);
  expect(parsePreviewValue('01234')).toBe('01234');
  expect(parsePreviewValue('acme')).toBe('acme');
  expect(parsePreviewValue('1, 2,acme,')).toEqual([1, 2, 'acme']);
});

test('reads preview column names from strings or column objects', () => {
  expect(getPreviewColumnNames(['a', { name: 'b', type: 'int64' }])).toEqual([
    'a',
    'b',
  ]);
});

test('sorts subreports by position then id', () => {
  expect(
    sortSubreports([subreport(3, 1), subreport(2, 0), subreport(1, 1)]).map(
      ({ id }) => id,
    ),
  ).toEqual([2, 1, 3]);
});

test('drops mappings for parameters no longer in the SQL', () => {
  expect(
    buildParamMapping(['a'], { a: '$F{x}', stale: '$F{y}', b: '' }),
  ).toEqual({ a: '$F{x}' });
});
