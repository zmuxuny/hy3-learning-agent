"""H0 failure baselines for the known H7 frontend state defects.

These tests execute the current store/component logic with dependency-free Node
stubs.  They deliberately describe the required observable contract and are
strict xfails until the corresponding H7 fix lands.  An unexpected pass is a
signal to remove the marker and keep the regression test permanently enabled.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_STORE = PROJECT_ROOT / "frontend" / "src" / "stores" / "workspace.js"
HOME_VIEW = PROJECT_ROOT / "frontend" / "src" / "components" / "HomeView.vue"
CONTRACT_RESULT_PREFIX = "__H0_CONTRACT_RESULT__="


class HarnessError(RuntimeError):
    """The contract probe itself could not be executed reliably."""


def _run_node(source: str) -> list[str]:
    wrapped_source = (
        "const __h0ContractViolations = [];\n"
        "function h0ContractViolation(message) {\n"
        "  __h0ContractViolations.push(String(message));\n"
        "}\n"
        + source
        + "\nconsole.log("
        + json.dumps(CONTRACT_RESULT_PREFIX)
        + " + JSON.stringify({ violations: __h0ContractViolations }));\n"
    )
    try:
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", wrapped_source],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HarnessError(f"Node contract probe could not run: {error}") from error

    if completed.returncode != 0:
        raise HarnessError(
            f"Node contract probe exited with {completed.returncode}.\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )

    result_lines = [
        line.removeprefix(CONTRACT_RESULT_PREFIX)
        for line in completed.stdout.splitlines()
        if line.startswith(CONTRACT_RESULT_PREFIX)
    ]
    if len(result_lines) != 1:
        raise HarnessError(
            "Node contract probe did not emit exactly one result marker.\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    try:
        payload: Any = json.loads(result_lines[0])
    except json.JSONDecodeError as error:
        raise HarnessError(f"Node contract probe emitted invalid JSON: {error}") from error
    if (
        not isinstance(payload, dict)
        or set(payload) != {"violations"}
        or not isinstance(payload["violations"], list)
        or not all(isinstance(item, str) and item for item in payload["violations"])
    ):
        raise HarnessError(f"Node contract probe emitted an invalid result: {payload!r}")
    return payload["violations"]


def _assert_contract(violations: list[str]) -> None:
    assert not violations, "; ".join(violations)


STORE_BOOTSTRAP = r"""
const __h0StorePath = __STORE_PATH__;
let __h0StoreSource = fs.readFileSync(__h0StorePath, 'utf8');
__h0StoreSource = __h0StoreSource
  .replace(/^import\s+[^;]+;\s*$/gm, '')
  .replace('export const useWorkspaceStore', 'const useWorkspaceStore');

const __h0Ref = Symbol('h0-ref');
const __h0RefValue = (value) => ({ [__h0Ref]: true, value });
const __h0Computed = (getter) => ({
  [__h0Ref]: true,
  get value() { return getter(); },
});
const __h0DefineStore = (_name, setup) => () => {
  const state = setup();
  return new Proxy(state, {
    get(target, property) {
      const value = Reflect.get(target, property);
      return value && value[__h0Ref] ? value.value : value;
    },
    set(target, property, value) {
      const current = Reflect.get(target, property);
      if (current && current[__h0Ref]) {
        current.value = value;
        return true;
      }
      return Reflect.set(target, property, value);
    },
  });
};

globalThis.window = globalThis.window || {
  atob: (value) => Buffer.from(value, 'base64').toString('binary'),
  location: { href: 'http://127.0.0.1/', search: '', pathname: '/', hash: '' },
  history: { replaceState() {} },
  setInterval,
  clearInterval,
};
if (!('navigator' in globalThis)) {
  Object.defineProperty(globalThis, 'navigator', { value: {}, configurable: true });
}

const __h0StoreFactory = new Function(
  'computed',
  'ref',
  'defineStore',
  'api',
  `${__h0StoreSource}\nreturn useWorkspaceStore;`,
);
const useWorkspaceStore = __h0StoreFactory(
  __h0Computed,
  __h0RefValue,
  __h0DefineStore,
  api,
);
"""


def _store_probe(api_source: str, scenario: str) -> list[str]:
    bootstrap = STORE_BOOTSTRAP.replace(
        "__STORE_PATH__", json.dumps(str(WORKSPACE_STORE))
    )
    return _run_node(
        "import fs from 'node:fs';\n"
        + api_source
        + "\n"
        + bootstrap
        + "\n"
        + scenario
    )


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "H7-UI-002: the live answer is anchored to the first Run user message, "
        "so a later steer is rendered after the answer"
    ),
)
def test_h7_ui_002_live_run_anchors_after_the_latest_steer() -> None:
    home_path = json.dumps(str(HOME_VIEW))
    violations = _run_node(
        f"""
import fs from 'node:fs';
const source = fs.readFileSync({home_path}, 'utf8');
const script = source.match(/<script setup>([\\s\\S]*?)<\\/script>/)?.[1];
if (!script) throw new Error('HomeView script setup was not found');
const executable = script.replace(/^import\\s+[^;]+;\\s*$/gm, '');
const original = {{ id: 'original', run_id: 'run-1', role: 'user', content: '原始要求' }};
const steer = {{
  id: 'steer',
  run_id: 'run-1',
  role: 'user',
  content: '中途转向',
  message_metadata: {{ ui_kind: 'steer' }},
}};
const store = {{
  conversationMessages: [original, steer],
  currentRun: {{ id: 'run-1', status: 'running' }},
  runEvents: [],
  highlightedMessageId: null,
}};
const computed = (getter) => ({{ get value() {{ return getter(); }} }});
const ref = (value) => ({{ value }});
const nextTick = async () => {{}};
const watch = () => {{}};
const useWorkspaceStore = () => store;
const inspect = new Function(
  'computed', 'nextTick', 'ref', 'watch', 'useWorkspaceStore',
  `${{executable}}\\nreturn {{ currentRunUser, currentRunAssistant }};`,
)(computed, nextTick, ref, watch, useWorkspaceStore);
if (inspect.currentRunUser.value?.id !== steer.id) {{
  h0ContractViolation(
    `live answer anchor ${{inspect.currentRunUser.value?.id}} precedes latest steer ${{steer.id}}`,
  );
}}
"""
    )
    _assert_contract(violations)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "H7-UI-003: archiving a plan does not unconditionally remove that plan "
        "from both current detail and focus scope"
    ),
)
def test_h7_ui_003_archiving_a_plan_clears_current_and_focus_scopes() -> None:
    violations = _store_probe(
        r"""
const api = {
  async patch() { return { data: {} }; },
  async get(path) {
    if (path === '/plans') return { data: [] };
    if (path === '/plans?archived=true') {
      return { data: [{ id: 7, title: '归档目标', status: 'archived' }] };
    }
    throw new Error(`unexpected GET ${path}`);
  },
};
""",
        r"""
for (const detailWasLoaded of [true, false]) {
  const store = useWorkspaceStore();
  store.plans = [{ id: 7, title: '归档目标', status: 'active' }];
  store.archivedPlans = [];
  store.currentPlan = detailWasLoaded
    ? { id: 7, title: '归档目标', status: 'active', stages: [] }
    : null;
  store.focusPlanId = 7;
  store.activeSessionId = 'session-7';
  store.conversationMessages = [{ id: 1, role: 'user', content: '继续计划' }];

  const archived = await store.setPlanArchived(7, true);
  if (!archived) throw new Error('archive request unexpectedly failed');
  if (store.currentPlan && String(store.currentPlan.id) === '7') {
    h0ContractViolation(
      `archived plan detail survived with detailWasLoaded=${detailWasLoaded}`,
    );
  }
  if (store.focusPlanId != null && String(store.focusPlanId) === '7') {
    h0ContractViolation(
      `archived focus survived with detailWasLoaded=${detailWasLoaded}`,
    );
  }
  if (store.focusedPlan && String(store.focusedPlan.id) === '7') {
    h0ContractViolation(
      `computed focusedPlan still exposes archive with detailWasLoaded=${detailWasLoaded}`,
    );
  }
}
""",
    )
    _assert_contract(violations)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "H7-UI-004: an active-Run reminder reply loses its exact stable "
        "reply_to_intervention_id or consumes that target at the wrong boundary"
    ),
)
def test_h7_ui_004_queued_reminder_reply_carries_its_durable_target() -> None:
    violations = _store_probe(
        r"""
const stableInterventionId = 'intervention-stable-91';
let postedQueueBody = null;
let durableQueue = [];
let consumedInterventionId = null;
let rejectQueueWrite = false;
const api = {
  async post(path, body) {
    if (path === '/agent/queue') {
      if (rejectQueueWrite) throw new Error('fixture queue persistence failed');
      postedQueueBody = structuredClone(body);
      const messageMetadata = body.reply_to_intervention_id == null
        ? {}
        : { reply_to_intervention_id: body.reply_to_intervention_id };
      const queued = {
        id: 'queue-1',
        session_id: body.session_id,
        plan_id: body.plan_id,
        trigger: 'user_message',
        objective: body.objective,
        user_content: null,
        message_metadata: messageMetadata,
        position: 0,
      };
      durableQueue = [queued];
      return { data: structuredClone(queued) };
    }
    if (path === '/agent/queue/queue-1/send') {
      if (durableQueue.length !== 1) {
        throw new Error('queue consumption fixture did not find exactly one durable item');
      }
      consumedInterventionId = durableQueue[0].message_metadata.reply_to_intervention_id;
      durableQueue = [];
      return {
        data: {
          id: 'run-from-queue',
          session_id: 'session-1',
          plan_id: 7,
          status: 'queued',
          objective: '我卡在练习环境',
        },
      };
    }
    throw new Error(`unexpected POST ${path}`);
  },
  async get(path) {
    if (path.startsWith('/agent/queue')) return { data: structuredClone(durableQueue) };
    if (path === '/agent/sessions') {
      return { data: [{ id: 'session-1', plan_id: 7, title: '提醒对话' }] };
    }
    if (path === '/agent/sessions?archived=true') return { data: [] };
    throw new Error(`unexpected GET ${path}`);
  },
};

class FakeEventSource {
  addEventListener() {}
  close() {}
}
globalThis.EventSource = FakeEventSource;
""",
        r"""
const store = useWorkspaceStore();
store.activeSessionId = 'session-1';
store.focusPlanId = 7;
store.currentRun = { id: 'run-1', session_id: 'session-1', status: 'running' };
store.replyTargetNotification = {
  id: 91,
  intervention_id: stableInterventionId,
  title: '今天的学习提醒',
};

const queued = await store.enqueueMessage('我卡在练习环境');
if (!queued || !postedQueueBody) {
  throw new Error('queue fixture did not observe a successful enqueue request');
}
const expectedQueueBody = {
  objective: '我卡在练习环境',
  session_id: 'session-1',
  plan_id: 7,
  reply_to_intervention_id: stableInterventionId,
};
const bodyKeys = Object.keys(postedQueueBody).sort();
const expectedKeys = Object.keys(expectedQueueBody).sort();
const exactRequestSchema = (
  JSON.stringify(bodyKeys) === JSON.stringify(expectedKeys)
  && expectedKeys.every((key) => postedQueueBody[key] === expectedQueueBody[key])
);
if (!exactRequestSchema) {
  h0ContractViolation(
    `queue request schema/value mismatch: ${JSON.stringify(postedQueueBody)}`,
  );
}

if (queued.message_metadata?.reply_to_intervention_id !== stableInterventionId) {
  h0ContractViolation(
    `queue response lost durable intervention target: ${JSON.stringify(queued.message_metadata)}`,
  );
}
if (store.queuedMessages.length !== 1
    || store.queuedMessages[0].message_metadata?.reply_to_intervention_id !== stableInterventionId) {
  h0ContractViolation(
    `reloaded queue lost durable intervention target: ${JSON.stringify(store.queuedMessages)}`,
  );
}
if (store.replyTargetNotification !== null) {
  h0ContractViolation('successful durable enqueue did not consume the composer reply target');
}

const sent = await store.sendQueuedMessage('queue-1');
if (!sent) throw new Error('queue consumption fixture could not send the durable item');
if (consumedInterventionId !== stableInterventionId) {
  h0ContractViolation(
    `queue dispatch consumed intervention target ${String(consumedInterventionId)}`,
  );
}
if (store.queuedMessages.length !== 0) {
  h0ContractViolation('successfully dispatched durable queue item remained visible');
}

const retryStore = useWorkspaceStore();
retryStore.activeSessionId = 'session-1';
retryStore.focusPlanId = 7;
retryStore.replyTargetNotification = {
  id: 92,
  intervention_id: 'intervention-stable-92',
  title: '重试提醒',
};
rejectQueueWrite = true;
let persistenceFailed = false;
try {
  const retryResult = await retryStore.enqueueMessage('这条回复需要重试');
  persistenceFailed = !retryResult;
} catch {
  persistenceFailed = true;
}
if (!persistenceFailed) {
  throw new Error('queue failure fixture unexpectedly persisted the retry reply');
}
if (retryStore.replyTargetNotification?.intervention_id !== 'intervention-stable-92') {
  h0ContractViolation('failed durable enqueue consumed the reply target before persistence');
}
""",
    )
    _assert_contract(violations)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "H7-UI-005: EventSource errors do not reconcile current Run, events, "
        "and messages from durable HTTP state"
    ),
)
def test_h7_ui_005_sse_disconnect_reconciles_with_durable_run_state() -> None:
    violations = _store_probe(
        r"""
const apiCalls = [];
const unexpectedApiCalls = [];
let durableTerminal = false;
const completedRun = {
  id: 'run-1',
  session_id: 'session-1',
  plan_id: null,
  status: 'completed',
  objective: '解释事务边界',
};
const session = {
  id: 'session-1',
  plan_id: null,
  title: '事务学习',
  last_run_id: 'run-1',
  last_run_status: 'completed',
};
const finalMessages = [
  { id: 1, session_id: 'session-1', run_id: 'run-1', role: 'user', content: '解释事务边界', message_metadata: {} },
  { id: 2, session_id: 'session-1', run_id: 'run-1', role: 'assistant', content: '最终持久化回答', message_metadata: {} },
];
const terminalEvents = [{
  sequence: 9,
  event_type: 'run.completed',
  summary: '最终持久化回答',
  payload: {},
  created_at: '2026-08-18T00:00:00Z',
}];

const api = {
  async post(path) {
    if (path === '/agent/runs') {
      return { data: { ...completedRun, status: 'running' } };
    }
    unexpectedApiCalls.push(`POST ${path}`);
    throw new Error(`unexpected POST ${path}`);
  },
  async get(path) {
    apiCalls.push(path);
    if (path === '/agent/runs/run-1') {
      return { data: durableTerminal ? completedRun : { ...completedRun, status: 'running' } };
    }
    if (path.startsWith('/agent/runs/run-1/events')) return { data: durableTerminal ? terminalEvents : [] };
    if (path === '/agent/runs') return { data: durableTerminal ? [completedRun] : [] };
    if (path === '/agent/sessions') return { data: [session] };
    if (path === '/agent/sessions?archived=true') return { data: [] };
    if (path === '/agent/sessions/session-1/messages') return { data: durableTerminal ? finalMessages : finalMessages.slice(0, 1) };
    if (path === '/agent/sessions/session-1/planning') return { data: { intake: null, proposal: null } };
    if (path.startsWith('/agent/queue')) return { data: [] };
    if (path === '/profile') return { data: { id: 'local', level: 1, xp: 0 } };
    if (path === '/plans' || path === '/plans?archived=true') return { data: [] };
    if (path === '/dashboard') return { data: { activity: [], achievements: [] } };
    if (path === '/memories' || path === '/notifications' || path === '/notifications?archived=true' || path === '/operations') return { data: [] };
    if (path === '/settings/email') return { data: {} };
    if (path === '/settings/proactive') return { data: { enabled: true, active: false } };
    if (path === '/settings') return { data: {} };
    if (path === '/settings/followup') return { data: { follow_up_behavior: 'steer' } };
    unexpectedApiCalls.push(`GET ${path}`);
    throw new Error(`unexpected GET ${path}`);
  },
};

class FakeEventSource {
  static instances = [];
  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this.listeners = new Map();
    FakeEventSource.instances.push(this);
  }
  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }
  close() { this.readyState = 2; }
  failClosed() {
    this.readyState = 2;
    return this.onerror ? this.onerror({ type: 'error' }) : undefined;
  }
}
globalThis.EventSource = FakeEventSource;

class ControlledClock {
  constructor() {
    this.now = 0;
    this.nextId = 1;
    this.tasks = [];
    this.errors = [];
  }
  setTimeout(callback, milliseconds = 0, ...args) {
    if (typeof callback !== 'function') throw new Error('timer callback must be callable');
    const delay = Number(milliseconds);
    if (!Number.isFinite(delay)) throw new Error(`invalid timer delay ${milliseconds}`);
    const task = {
      id: this.nextId++,
      dueAt: this.now + Math.max(0, delay),
      callback,
      args,
    };
    this.tasks.push(task);
    return task.id;
  }
  clearTimeout(id) {
    this.tasks = this.tasks.filter((task) => task.id !== id);
  }
  async runNext(maximumVirtualTime) {
    this.tasks.sort((left, right) => left.dueAt - right.dueAt || left.id - right.id);
    const task = this.tasks[0];
    if (!task || task.dueAt > maximumVirtualTime) return false;
    this.tasks.shift();
    this.now = task.dueAt;
    const result = task.callback(...task.args);
    if (result && typeof result.then === 'function') {
      result.catch((error) => this.errors.push(error));
    }
    for (let turn = 0; turn < 6; turn += 1) await Promise.resolve();
    return true;
  }
}
const clock = new ControlledClock();
globalThis.setTimeout = clock.setTimeout.bind(clock);
globalThis.clearTimeout = clock.clearTimeout.bind(clock);
Date.now = () => clock.now;
""",
        r"""
window.setTimeout = globalThis.setTimeout;
window.clearTimeout = globalThis.clearTimeout;
const store = useWorkspaceStore();
const started = await store.startRun('解释事务边界');
if (!started) throw new Error('run did not start');
const source = FakeEventSource.instances.at(-1);
if (!source) throw new Error('run did not create an EventSource');

durableTerminal = true;
let eventSourceError = null;
const errorWork = source.failClosed();
if (errorWork && typeof errorWork.then === 'function') {
  errorWork.catch((error) => { eventSourceError = error; });
}

const hasReconciledDurableState = () => (
  store.currentRun?.status === 'completed'
  && store.conversationMessages.some(
    (message) => message.role === 'assistant' && message.content === '最终持久化回答',
  )
  && store.runEvents.some((event) => event.sequence === 9)
  && apiCalls.includes('/agent/runs/run-1')
  && apiCalls.some((path) => path.startsWith('/agent/runs/run-1/events'))
  && apiCalls.includes('/agent/sessions/session-1/messages')
);
const maximumVirtualTime = 30000;
const maximumClockSteps = 64;
for (let step = 0; step < maximumClockSteps && !hasReconciledDurableState(); step += 1) {
  for (let turn = 0; turn < 6; turn += 1) await Promise.resolve();
  if (eventSourceError) throw eventSourceError;
  if (clock.errors.length) throw clock.errors[0];
  if (hasReconciledDurableState()) break;
  if (!await clock.runNext(maximumVirtualTime)) break;
}
if (eventSourceError) throw eventSourceError;
if (clock.errors.length) throw clock.errors[0];
if (unexpectedApiCalls.length) {
  throw new Error(`SSE fixture missed API calls: ${unexpectedApiCalls.join(',')}`);
}

if (!hasReconciledDurableState()) {
  h0ContractViolation(
    `SSE close left stale UI after ${clock.now}ms virtual time: status=${store.currentRun?.status}, finalVisible=${store.conversationMessages.some((message) => message.role === 'assistant' && message.content === '最终持久化回答')}, terminalEventVisible=${store.runEvents.some((event) => event.sequence === 9)}, calls=${apiCalls.join(',')}`,
  );
}
""",
    )
    _assert_contract(violations)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "H7-UI-006: one degradable settings/statistics request rejects the "
        "single startup Promise.all before core workspace state is assigned"
    ),
)
def test_h7_ui_006_optional_boot_failure_preserves_core_workspace() -> None:
    violations = _store_probe(
        r"""
let failedPath = '/settings/email';
const unexpectedApiCalls = [];
const api = {
  async get(path) {
    if (path === failedPath) throw new Error(`degradable endpoint unavailable: ${path}`);
    if (path === '/profile') return { data: { id: 'local', level: 2, xp: 40 } };
    if (path === '/plans') return { data: [{ id: 7, title: '核心计划', status: 'active' }] };
    if (path === '/plans?archived=true') return { data: [] };
    if (path === '/dashboard') return { data: { activity: [], achievements: [] } };
    if (path === '/memories' || path === '/notifications' || path === '/notifications?archived=true' || path === '/operations' || path === '/agent/runs' || path === '/agent/sessions?archived=true' || path === '/agent/queue') return { data: [] };
    if (path === '/agent/sessions') return { data: [{ id: 'session-1', title: '核心会话', plan_id: null }] };
    if (path === '/settings/email') return { data: {} };
    if (path === '/settings/proactive') return { data: { enabled: true } };
    if (path === '/settings') return { data: { model: 'hy3' } };
    if (path === '/settings/followup') return { data: { follow_up_behavior: 'steer' } };
    unexpectedApiCalls.push(path);
    throw new Error(`unexpected GET ${path}`);
  },
};
""",
        r"""
for (const optionalFailure of ['/settings/email', '/dashboard']) {
  failedPath = optionalFailure;
  const store = useWorkspaceStore();
  await store.loadWorkspace();
  if (unexpectedApiCalls.length) {
    throw new Error(`boot fixture missed GETs: ${unexpectedApiCalls.join(',')}`);
  }
  const coreAvailable = (
    store.profile?.id === 'local'
    && store.plans.some((plan) => plan.id === 7)
    && store.sessions.some((session) => session.id === 'session-1')
  );
  if (!coreAvailable) {
    h0ContractViolation(`core workspace was cleared by ${optionalFailure}`);
  }
}
""",
    )
    _assert_contract(violations)
