import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';

import { createPinia, setActivePinia } from 'pinia';

import api from '../src/api/client.js';
import { useWorkspaceStore } from '../src/stores/workspace.js';

const originalGet = api.get;
const originalPost = api.post;

afterEach(() => {
  api.get = originalGet;
  api.post = originalPost;
});

test('SMTP diagnostic retries retain one client action id until enqueue is confirmed', async () => {
  setActivePinia(createPinia());
  const store = useWorkspaceStore();
  const requests = [];
  let postAttempt = 0;

  api.post = async (path, payload) => {
    requests.push({ path, payload: { ...payload } });
    postAttempt += 1;
    if (postAttempt === 1) throw new Error('response lost after request');
    return { data: { ok: true, channel: 'smtp', status: 'queued' } };
  };
  api.get = async () => ({ data: { smtp_configured: true } });

  assert.equal(await store.testEmail('smtp', true), false);
  assert.equal(await store.testEmail('smtp', true), true);
  assert.equal(requests[0].path, '/settings/email/test');
  assert.equal(requests[0].payload.action_id, requests[1].payload.action_id);
  assert.match(requests[0].payload.action_id, /^[0-9a-f-]{36}$/i);

  assert.equal(await store.testEmail('smtp', true), true);
  assert.notEqual(requests[2].payload.action_id, requests[1].payload.action_id);

  assert.equal(await store.testEmail('smtp', false), true);
  assert.deepEqual(requests[3].payload, {
    channel: 'smtp',
    send_message: false,
  });
});

test('a failed settings refresh does not invalidate a confirmed SMTP enqueue', async () => {
  setActivePinia(createPinia());
  const store = useWorkspaceStore();
  const requests = [];

  api.post = async (_path, payload) => {
    requests.push({ ...payload });
    return { data: { ok: true, channel: 'smtp', status: 'queued' } };
  };
  api.get = async () => {
    throw new Error('configuration refresh unavailable');
  };

  assert.equal(await store.testEmail('smtp', true), true);
  assert.deepEqual(store.emailTestResult, {
    ok: true,
    channel: 'smtp',
    status: 'queued',
  });

  assert.equal(await store.testEmail('smtp', true), true);
  assert.notEqual(requests[0].action_id, requests[1].action_id);
});
