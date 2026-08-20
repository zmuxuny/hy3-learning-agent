import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  isRunBlocking,
  isRunSteerable,
  isRunStreamable,
  runStatusLabel,
} from '../src/runState.js';

test('retry and reconciliation states cannot be mistaken for an idle session', () => {
  for (const status of [
    'queued', 'running', 'waiting_approval', 'retry_wait', 'needs_reconciliation',
  ]) {
    assert.equal(isRunBlocking(status), true, status);
  }
  for (const status of ['completed', 'failed', 'cancelled', undefined]) {
    assert.equal(isRunBlocking(status), false, String(status));
  }
});

test('only executable states retain a live stream or accept steering', () => {
  assert.equal(isRunStreamable('retry_wait'), true);
  assert.equal(isRunSteerable('retry_wait'), true);
  assert.equal(isRunStreamable('waiting_approval'), false);
  assert.equal(isRunSteerable('waiting_approval'), false);
  assert.equal(isRunStreamable('needs_reconciliation'), false);
  assert.equal(isRunSteerable('needs_reconciliation'), false);
});

test('durable exceptional states have explicit user-facing labels', () => {
  assert.equal(runStatusLabel('retry_wait'), '等待重试');
  assert.equal(runStatusLabel('needs_reconciliation'), '需要人工处理');
});
