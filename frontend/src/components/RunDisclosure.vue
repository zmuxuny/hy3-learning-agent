<script setup>
import {
  ChevronRightIcon,
  CommandLineIcon,
  CircleStackIcon,
  ExclamationCircleIcon,
  UserGroupIcon,
} from '@heroicons/vue/24/outline';
import { computed, ref, watch } from 'vue';
import { useWorkspaceStore } from '../stores/workspace';
import AgentMessage from './AgentMessage.vue';

const props = defineProps({
  run: { type: Object, default: null },
  events: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
});
const emit = defineEmits(['expand']);
const store = useWorkspaceStore();

const expanded = ref(false);
const childExpanded = ref(new Set());
const childDetails = ref({});
const childLoading = ref(new Set());
const childErrors = ref({});
const childEventExpanded = ref(new Set());
const eventExpanded = ref(new Set());

const TOOL_LABELS = {
  profile_get: '读取学习画像',
  plan_list: '查看学习计划',
  plan_get: '读取计划详情',
  plan_create: '创建学习计划',
  task_patch: '更新学习任务',
  plan_patch: '调整学习计划',
  stage_create: '添加计划阶段',
  task_create: '添加学习任务',
  review_schedule: '安排复习',
  quiz_create: '生成考核',
  quiz_get: '读取考核标准',
  quiz_grade: '评估学习结果',
  memory_search: '检索相关记忆',
  memory_propose: '整理长期记忆',
  memory_maintain: '维护分层记忆',
  web_search: '搜索学习资料',
  web_open: '阅读网页资料',
  file_list: '查看工作区文件',
  file_read: '读取文件',
  file_write: '写入文件',
  code_execute: '运行代码',
  calendar_list: '查看日程',
  calendar_create: '创建日程',
  calendar_patch: '调整日程',
  notification_send: '发送学习提醒',
  study_state_get: '判断当前学习位置',
  learning_event_list: '读取近期学习记录',
  resource_list: '查看已核验资源',
  resource_save: '保存学习资源',
  submission_create: '提交学习证据',
  submission_get: '读取提交证据',
  submission_list: '查看近期提交',
  submission_check: '验收学习成果',
  planning_intake_get: '读取计划需求',
  planning_intake_update: '整理计划需求',
  planning_delegate: '委派规划调查',
  plan_proposal_create: '生成计划提案',
  subagent_spawn: '启动子 Agent',
  subagent_status: '检查子 Agent 状态',
  subagent_join: '汇总子 Agent 结论',
  subagent_cancel: '停止子 Agent',
};

const normalizedEvents = computed(() => props.events.map((event) => ({
  ...event,
  type: event.type || event.event_type,
})));
const active = computed(() => ['queued', 'running'].includes(props.run?.status));
const terminalEvent = computed(() => [...normalizedEvents.value].reverse().find((event) => (
  ['run.completed', 'run.failed', 'run.cancelled'].includes(event.type)
)));
const visibleEvents = computed(() => {
  const rows = [];
  for (const event of normalizedEvents.value) {
    if (['run.started', 'run.resumed', 'assistant.delta', 'assistant.message', 'run.completed'].includes(event.type)) continue;
    if (event.type === 'tool.started') {
      const completed = normalizedEvents.value.find((item) => (
        item.type === 'tool.completed'
        && item.payload?.tool_call_id === event.payload?.tool_call_id
      ));
      rows.push(completed || event);
      continue;
    }
    if (event.type === 'tool.completed' && rows.some((item) => (
      item.payload?.tool_call_id === event.payload?.tool_call_id
    ))) continue;
    if (event.type === 'subagent.completed') continue;
    rows.push(event);
  }
  return rows;
});
const subagents = computed(() => normalizedEvents.value
  .filter((event) => event.type === 'subagent.started')
  .map((event) => {
    const childId = event.payload?.child_run_id;
    const completed = [...normalizedEvents.value].reverse().find((item) => (
      item.type === 'subagent.completed' && item.payload?.child_run_id === childId
    ));
    return {
      id: childId,
      role: event.payload?.role || '子 Agent',
      objective: event.payload?.objective || '',
      status: childStatus(completed?.payload?.status, active.value),
    };
  }));

function childStatus(status, parentActive) {
  if (status === 'completed') return '已完成';
  if (status === 'failed') return '失败';
  if (status === 'cancelled') return '已停止';
  return parentActive ? '处理中' : '待同步';
}

const durationLabel = computed(() => {
  const startValue = props.run?.started_at || props.run?.created_at;
  if (!startValue) return '';
  const endValue = props.run?.completed_at || terminalEvent.value?.created_at || new Date().toISOString();
  const seconds = Math.max(0, Math.round((new Date(endValue) - new Date(startValue)) / 1000));
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
});
const statusLabel = computed(() => {
  if (props.run?.status === 'waiting_approval') return '等待确认';
  if (active.value) return '处理中';
  if (props.run?.status === 'failed') return '处理失败';
  if (props.run?.status === 'cancelled') return '已停止';
  return '已处理';
});

watch(() => props.run?.id, () => {
  expanded.value = ['queued', 'running', 'waiting_approval', 'failed'].includes(props.run?.status);
  if (expanded.value) queueMicrotask(() => emit('expand'));
}, { immediate: true });

function toggle() {
  expanded.value = !expanded.value;
  if (expanded.value) emit('expand');
}

async function toggleChild(childId) {
  if (!childId) return;
  const next = new Set(childExpanded.value);
  next.has(childId) ? next.delete(childId) : next.add(childId);
  childExpanded.value = next;
  if (next.has(childId) && !childDetails.value[childId]) {
    const loading = new Set(childLoading.value);
    loading.add(childId);
    childLoading.value = loading;
    childErrors.value = { ...childErrors.value, [childId]: '' };
    try {
      const events = await store.fetchChildRunEvents(childId);
      childDetails.value = { ...childDetails.value, [childId]: events };
    } catch (error) {
      childErrors.value = {
        ...childErrors.value,
        [childId]: error.response?.data?.detail || error.message || '工作记录读取失败',
      };
    } finally {
      const finished = new Set(childLoading.value);
      finished.delete(childId);
      childLoading.value = finished;
    }
  }
}

async function retryChild(childId) {
  childDetails.value = { ...childDetails.value, [childId]: null };
  childErrors.value = { ...childErrors.value, [childId]: '' };
  const collapsed = new Set(childExpanded.value);
  collapsed.delete(childId);
  childExpanded.value = collapsed;
  await toggleChild(childId);
}

function childEvents(childId) {
  const events = (childDetails.value[childId] || []).map((event) => ({
    ...event,
    type: event.type || event.event_type,
  }));
  const rows = [];
  for (const event of events) {
    if (['run.started', 'run.completed', 'run.failed', 'run.cancelled', 'assistant.delta'].includes(event.type)) continue;
    if (event.type === 'tool.started') {
      const completed = events.find((item) => (
        item.type === 'tool.completed'
        && item.payload?.tool_call_id === event.payload?.tool_call_id
      ));
      rows.push(completed || event);
      continue;
    }
    if (event.type === 'tool.completed' && rows.some((item) => (
      item.payload?.tool_call_id === event.payload?.tool_call_id
    ))) continue;
    rows.push(event);
  }
  return rows;
}

function childReport(childId) {
  const finalEvent = [...(childDetails.value[childId] || [])].reverse().find((event) => (
    ['run.completed', 'run.failed', 'run.cancelled'].includes(event.event_type || event.type)
  ));
  const report = finalEvent?.summary || '';
  const legacyFailure = report.match(/^Specialist failed:\s*(.+)$/i);
  if (legacyFailure?.[1] === 'OperationalError') {
    return '这次子 Agent 调查因旧版本的数据库并发写入冲突而中断；已完成的搜索和网页读取记录仍保留在上方。';
  }
  return legacyFailure ? `子 Agent 调查失败：${legacyFailure[1]}` : report;
}

function childEventKey(childId, event) {
  return `${childId}:${eventKey(event)}`;
}

function toggleChildEvent(childId, event) {
  if (!eventExpandable(event)) return;
  const key = childEventKey(childId, event);
  const next = new Set(childEventExpanded.value);
  next.has(key) ? next.delete(key) : next.add(key);
  childEventExpanded.value = next;
}

function toolLabel(name) {
  return TOOL_LABELS[name] || name || '工具操作';
}

function resultError(result) {
  if (!result?.error) return '';
  const value = typeof result.error === 'string' ? result.error : result.error.message || JSON.stringify(result.error);
  return value.length > 140 ? `${value.slice(0, 140)}…` : value;
}

function toolSubject(event) {
  const name = event.payload?.name;
  const data = event.payload?.result?.data || {};
  if (name === 'web_search' && data.query) return `“${String(data.query).slice(0, 46)}${String(data.query).length > 46 ? '…' : ''}”`;
  if (name === 'web_open' && data.url) {
    try {
      return new URL(data.url).hostname.replace(/^www\./, '');
    } catch {
      return String(data.url).slice(0, 46);
    }
  }
  return '';
}

function eventLabel(event) {
  const result = event.payload?.result;
  if (event.type === 'context.built') return '读取了计划、近期进度与相关记忆';
  if (event.type === 'tool.completed' || event.type === 'tool.started') {
    const name = toolLabel(event.payload?.name);
    if (result?.ok === false) return `${name}未完成：${resultError(result) || '调用失败'}`;
    const subject = toolSubject(event);
    return `${name}${subject ? ` ${subject}` : ''}${event.type === 'tool.started' ? '…' : ''}`;
  }
  if (event.type === 'assistant.status') return event.summary || '继续处理';
  if (event.type === 'assistant.reasoning') return event.summary || event.payload?.text || '正在分析下一步';
  if (event.type === 'steer.received') return '已接收你的补充要求，并调整当前处理方向';
  if (event.type === 'approval.required') return `等待确认 ${event.payload?.tool_name || '操作'}`;
  if (event.type === 'approval.resolved') return event.payload?.approved ? '操作已获批准' : '操作已被拒绝，正在调整方案';
  if (event.type === 'run.retrying') return event.summary || '暂时失败，正在重试';
  if (event.type === 'run.budget_exceeded') return event.summary || '已达到本次运行预算';
  if (event.type === 'run.failed') return event.summary || '运行没有正常完成';
  if (event.type === 'run.cancelled') return '运行已停止';
  return event.summary || event.type;
}

function eventKey(event) {
  return String(event.sequence || `${event.type}-${event.created_at}`);
}

function eventExpandable(event) {
  return Boolean(
    event.type === 'context.built'
    || event.type?.startsWith('tool.')
    || event.type === 'run.budget_exceeded'
    || event.type?.startsWith('approval.'),
  );
}

function toggleEvent(event) {
  if (!eventExpandable(event)) return;
  const key = eventKey(event);
  const next = new Set(eventExpanded.value);
  next.has(key) ? next.delete(key) : next.add(key);
  eventExpanded.value = next;
}

function detailSections(event) {
  const sections = [];
  if (event.payload?.arguments && Object.keys(event.payload.arguments).length) {
    sections.push({ label: '输入', value: event.payload.arguments });
  }
  if (event.payload?.result) {
    sections.push({ label: '结果', value: event.payload.result });
  } else if (event.type === 'context.built') {
    sections.push({ label: '上下文快照', value: event.payload });
  } else if (event.payload && Object.keys(event.payload).length) {
    sections.push({ label: '详情', value: event.payload });
  }
  return sections;
}

function prettyJson(value) {
  const output = JSON.stringify(value, null, 2);
  return output.length > 7000 ? `${output.slice(0, 7000)}\n… 已截断` : output;
}

function eventIcon(event) {
  if (event.type === 'context.built') return CircleStackIcon;
  if (event.type?.startsWith('tool.')) return CommandLineIcon;
  if (event.type?.startsWith('subagent.')) return UserGroupIcon;
  if (['run.failed', 'run.budget_exceeded'].includes(event.type) || event.payload?.result?.ok === false) return ExclamationCircleIcon;
  return null;
}
</script>

<template>
  <section v-if="run" :class="['run-disclosure', { expanded, active, failed: run.status === 'failed' }]">
    <button class="run-disclosure-toggle" type="button" :aria-expanded="expanded" @click="toggle">
      <span>{{ statusLabel }}<template v-if="durationLabel"> {{ durationLabel }}</template></span>
      <ChevronRightIcon />
      <i aria-hidden="true"></i>
    </button>

    <div v-if="expanded" class="run-disclosure-body">
      <p v-if="loading" class="run-loading">正在恢复这次运行的操作记录…</p>
      <template v-else>
        <div v-if="subagents.length" class="agent-chip-row">
          <button
            v-for="agent in subagents"
            :key="agent.id"
            :class="['agent-chip', { open: childExpanded.has(agent.id) }]"
            type="button"
            :aria-expanded="childExpanded.has(agent.id)"
            :aria-controls="`child-run-${agent.id}`"
            @click="toggleChild(agent.id)"
          >
            <UserGroupIcon />
            <span>{{ agent.role }}</span>
            <small>{{ agent.status }}</small>
            <ChevronRightIcon class="agent-chip-chevron" />
          </button>
          <div
            v-for="agent in subagents.filter((item) => childExpanded.has(item.id))"
            :key="`detail-${agent.id}`"
            :id="`child-run-${agent.id}`"
            class="agent-chip-detail"
          >
            <div class="child-run-heading">
              <small>调查任务</small>
              <p>{{ agent.objective }}</p>
            </div>
            <p v-if="childLoading.has(agent.id)" class="child-run-state">正在读取调查过程…</p>
            <p v-else-if="childErrors[agent.id]" class="child-run-state error">
              无法读取调查过程：{{ childErrors[agent.id] }}
              <button type="button" @click="retryChild(agent.id)">重试</button>
            </p>
            <template v-else>
              <div v-if="childEvents(agent.id).length" class="child-worklog">
                <div
                  v-for="event in childEvents(agent.id)"
                  :key="childEventKey(agent.id, event)"
                  :class="['run-action-item', { open: childEventExpanded.has(childEventKey(agent.id, event)), error: event.payload?.result?.ok === false || event.type === 'run.failed' }]"
                >
                  <button
                    :class="['run-action-line', { expandable: eventExpandable(event) }]"
                    type="button"
                    :aria-expanded="eventExpandable(event) ? childEventExpanded.has(childEventKey(agent.id, event)) : undefined"
                    @click="toggleChildEvent(agent.id, event)"
                  >
                    <component :is="eventIcon(event)" v-if="eventIcon(event)" />
                    <i v-else class="action-icon-spacer" aria-hidden="true"></i>
                    <span>{{ eventLabel(event) }}</span>
                    <small v-if="event.payload?.result?.budget_exceeded">已达调查上限</small>
                    <ChevronRightIcon v-if="eventExpandable(event)" class="action-chevron" />
                  </button>
                  <div v-if="childEventExpanded.has(childEventKey(agent.id, event))" class="run-action-detail">
                    <section v-for="section in detailSections(event)" :key="section.label">
                      <small>{{ section.label }}</small>
                      <pre>{{ prettyJson(section.value) }}</pre>
                    </section>
                  </div>
                </div>
              </div>
              <section v-if="childReport(agent.id)" class="child-report">
                <small>{{ agent.status === '失败' ? '失败说明' : '调查结论' }}</small>
                <AgentMessage :content="childReport(agent.id)" />
              </section>
              <p v-else class="child-run-state">这次调查没有生成最终结论，可查看上面的工具记录。</p>
            </template>
          </div>
        </div>
        <div
          v-for="event in visibleEvents"
          :key="eventKey(event)"
          :class="['run-action-item', { open: eventExpanded.has(eventKey(event)), error: event.payload?.result?.ok === false || event.type === 'run.failed' }]"
        >
          <button
            :class="['run-action-line', { expandable: eventExpandable(event) }]"
            type="button"
            :aria-expanded="eventExpandable(event) ? eventExpanded.has(eventKey(event)) : undefined"
            @click="toggleEvent(event)"
          >
            <component :is="eventIcon(event)" v-if="eventIcon(event)" />
            <i v-else class="action-icon-spacer" aria-hidden="true"></i>
            <span>{{ eventLabel(event) }}</span>
            <small v-if="event.payload?.result?.replayed">已从检查点复用</small>
            <ChevronRightIcon v-if="eventExpandable(event)" class="action-chevron" />
          </button>
          <div v-if="eventExpanded.has(eventKey(event))" class="run-action-detail">
            <section v-for="section in detailSections(event)" :key="section.label">
              <small>{{ section.label }}</small>
              <pre>{{ prettyJson(section.value) }}</pre>
            </section>
          </div>
        </div>
      </template>
    </div>
  </section>
</template>
