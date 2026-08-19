import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';

import api from '../src/api/client.js';

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

test('GET requests use the API prefix without a request body', async () => {
  const calls = [];
  globalThis.fetch = async (...args) => {
    calls.push(args);
    return {
      ok: true,
      status: 200,
      json: async () => ({ status: 'ok' }),
    };
  };

  const response = await api.get('/health');

  assert.deepEqual(response, { data: { status: 'ok' } });
  assert.deepEqual(calls, [[
    '/api/v1/health',
    { method: 'GET', headers: {}, body: undefined },
  ]]);
});

test('JSON requests serialize their body and set the content type', async () => {
  const calls = [];
  globalThis.fetch = async (...args) => {
    calls.push(args);
    return {
      ok: true,
      status: 201,
      json: async () => ({ id: 'run-1' }),
    };
  };

  const response = await api.post('/agent/runs', { prompt: 'review' });

  assert.deepEqual(response, { data: { id: 'run-1' } });
  assert.deepEqual(calls, [[
    '/api/v1/agent/runs',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt: 'review' }),
    },
  ]]);
});

test('API errors preserve the response payload and status', async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 409,
    json: async () => ({ detail: 'idempotency_conflict' }),
  });

  await assert.rejects(
    api.patch('/plans/plan-1', { title: 'changed' }),
    (error) => {
      assert.equal(error.message, 'idempotency_conflict');
      assert.deepEqual(error.response, {
        data: { detail: 'idempotency_conflict' },
        status: 409,
      });
      return true;
    },
  );
});

test('non-JSON errors fall back to the HTTP status', async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 503,
    json: async () => {
      throw new SyntaxError('not JSON');
    },
  });

  await assert.rejects(
    api.delete('/notifications/notification-1'),
    (error) => {
      assert.equal(error.message, 'Request failed with 503');
      assert.deepEqual(error.response, { data: null, status: 503 });
      return true;
    },
  );
});
