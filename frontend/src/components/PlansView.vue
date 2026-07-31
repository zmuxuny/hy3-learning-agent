<script setup>
import {
  AdjustmentsHorizontalIcon,
  ArrowLeftIcon,
  ArrowTopRightOnSquareIcon,
  BoltIcon,
  CalendarDaysIcon,
  CheckCircleIcon,
  ChevronRightIcon,
  ClockIcon,
  DocumentCheckIcon,
  MapIcon,
  PlayCircleIcon,
  PlusIcon,
  ShieldCheckIcon,
  SparklesIcon,
  PaperClipIcon,
} from '@heroicons/vue/24/outline';
import { computed } from 'vue';
import { useWorkspaceStore } from '../stores/workspace';
import AgentComposer from './AgentComposer.vue';
import RunTraceButton from './RunTraceButton.vue';

const store = useWorkspaceStore();
const taskIds = computed(() => new Set(
  store.currentPlan?.stages.flatMap((stage) => stage.tasks.map((task) => String(task.id))) || [],
));
const planOperations = computed(() => store.operations.filter((operation) => (
  (operation.entity_type === 'plan' && operation.entity_id === String(store.currentPlan?.id))
  || (operation.entity_type === 'task' && taskIds.value.has(operation.entity_id))
)));

function tasksFor(plan) {
  return plan.stages.flatMap((stage) => stage.tasks);
}

function completedTasks(plan) {
  return tasksFor(plan).filter((task) => task.status === 'completed').length;
}

function operationCount(plan) {
  const ids = new Set(tasksFor(plan).map((task) => String(task.id)));
  return store.operations.filter((operation) => (
    (operation.entity_type === 'plan' && operation.entity_id === String(plan.id))
    || (operation.entity_type === 'task' && ids.has(operation.entity_id))
  )).length;
}

function statusLabel(status) {
  return { pending: '待开始', active: '进行中', completed: '已完成', blocked: '受阻', skipped: '已跳过' }[status] || status;
}

function formatDate(value, includeTime = false) {
  if (!value) return '未安排';
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    month: 'numeric',
    day: 'numeric',
    ...(includeTime ? { hour: '2-digit', minute: '2-digit' } : {}),
  }).format(new Date(value));
}

function formatUpdated(value) {
  if (!value) return '尚未更新';
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value));
}

function stageProgress(stage) {
  if (!stage.tasks.length) return 0;
  return Math.round(stage.tasks.filter((task) => task.status === 'completed').length / stage.tasks.length * 100);
}

function wasTouchedByAgent(task) {
  return store.operations.some((operation) => operation.entity_type === 'task' && operation.entity_id === String(task.id));
}

function askAgentAboutTask(task) {
  store.startRun(
    `请检查任务 ${task.id}「${task.title}」的当前状态、证据要求和截止时间。先读取计划，再告诉我今天如何推进；如需调整，只执行低风险且可撤销的修改。`,
    store.currentPlan.id,
  );
}

function submitTaskToAgent(task) {
  store.startRun(
    `我要提交任务 ${task.id}「${task.title}」的学习成果。请先读取任务要求，询问我需要提交的文字、文件路径、代码或链接；收到后使用 submission_create 保存证据，必要时读取文件或运行代码，再用 submission_check 给出验收结果。`,
    store.currentPlan.id,
  );
}

function inspectPlanWithAgent() {
  store.startRun(
    '请完整检查当前计划的阶段、任务、截止时间、复习安排和最近学习事件，识别风险并给出今天的最小可执行行动。必要时可进行低风险且可撤销的调整。',
    store.currentPlan.id,
  );
}

function checkCurrentPlan() {
  store.startRun(
    '请主动检查当前计划现在是否有逾期、阻塞、待复习或需要提醒的事项。先读取真实状态；如果没有需要干预的内容，直接说明无需动作。',
    store.currentPlan.id,
  );
}

function createPlanWithAgent() {
  store.startNewConversation();
  store.startRun('请通过对话引导我创建一份新的学习计划。先确认目标、当前基础、期限、每周时间、偏好、期望产出、已有资源和不希望采用的方式，再生成完整计划。');
}
</script>

<template>
  <section v-if="store.planScreen === 'list'" class="view plan-index-view">
    <header class="view-header plan-index-header">
      <div>
        <span class="eyebrow">PLANS · PROGRESS · EVIDENCE</span>
        <h1>学习计划</h1>
        <p>查看所有学习目标和执行状态。进入一个计划后，对话会明确专注于该计划。</p>
      </div>
      <div class="page-header-actions">
        <RunTraceButton />
        <button class="primary-button" @click="createPlanWithAgent"><PlusIcon /> 用 Agent 创建</button>
      </div>
    </header>

    <div class="plan-index-content">
      <div class="plan-index-toolbar">
        <div><strong>全部计划</strong><span>{{ store.plans.length }}</span></div>
        <small>按最近更新排序</small>
      </div>

      <div v-if="store.plans.length" class="plan-list">
        <button v-for="plan in store.plans" :key="plan.id" class="plan-list-card" @click="store.selectPlan(plan.id)">
          <div class="plan-list-main">
            <header>
              <span :class="['plan-list-status', plan.status]"><i></i>{{ statusLabel(plan.status) }}</span>
              <small>更新于 {{ formatUpdated(plan.updated_at) }}</small>
            </header>
            <h2>{{ plan.title }}</h2>
            <p>{{ plan.goal || plan.description || '等待 Agent 补充计划目标。' }}</p>
            <div class="plan-list-progress">
              <span><i :style="{ width: `${Math.round(plan.progress * 100)}%` }"></i></span>
              <strong>{{ Math.round(plan.progress * 100) }}%</strong>
            </div>
            <footer>
              <span><CalendarDaysIcon />{{ formatDate(plan.deadline) }}</span>
              <span><MapIcon />{{ plan.stages.length }} 阶段 · {{ tasksFor(plan).length }} 任务</span>
              <span><CheckCircleIcon />{{ completedTasks(plan) }} 项已完成</span>
              <span><SparklesIcon />{{ operationCount(plan) }} 次 Agent 操作</span>
            </footer>
          </div>
          <div class="plan-list-enter">
            <span>进入计划</span>
            <ChevronRightIcon />
          </div>
        </button>
      </div>

      <div v-else class="plan-list-empty panel">
        <MapIcon />
        <h2>还没有学习计划</h2>
        <p>让 Agent 先了解你的目标和约束，再创建第一份完整计划。</p>
        <button class="primary-button" @click="createPlanWithAgent"><PlusIcon /> 用 Agent 创建</button>
      </div>
    </div>
  </section>

  <section v-else class="view has-composer">
    <header v-if="store.currentPlan" class="plan-detail-toolbar">
      <button class="back-button icon-back" aria-label="返回所有计划" @click="store.openPlanList"><ArrowLeftIcon /></button>
      <h1>{{ store.currentPlan.title }}</h1>
      <RunTraceButton />
    </header>

    <div class="plan-detail-layout">
      <section class="plan-workspace">
        <main v-if="store.currentPlan" class="plan-detail">
          <article class="plan-hero">
            <div class="plan-hero-main">
              <p>{{ store.currentPlan.goal }}</p>
              <div class="plan-meta-grid">
                <div><CalendarDaysIcon /><span><small>最终期限</small><strong>{{ formatDate(store.currentPlan.deadline, true) }}</strong></span></div>
                <div><ClockIcon /><span><small>每周投入</small><strong>{{ store.currentPlan.weekly_minutes }} 分钟</strong></span></div>
                <div><MapIcon /><span><small>计划结构</small><strong>{{ store.currentPlan.stages.length }} 阶段 · {{ taskIds.size }} 任务</strong></span></div>
                <div><AdjustmentsHorizontalIcon /><span><small>Agent 操作</small><strong>{{ planOperations.length }} 条可审计记录</strong></span></div>
              </div>
            </div>
            <div class="plan-progress-block">
              <div class="plan-score" :style="{ '--progress': `${Math.round(store.currentPlan.progress * 360)}deg` }">
                <span><strong>{{ Math.round(store.currentPlan.progress * 100) }}%</strong><small>整体进度</small></span>
              </div>
              <div class="plan-output"><DocumentCheckIcon /><span><small>期望产出</small><strong>{{ store.currentPlan.expected_outcome || '等待补充' }}</strong></span></div>
            </div>
          </article>

          <div class="plan-action-row">
            <span><i></i>计划焦点 · 版本 {{ store.currentPlan.version }}</span>
            <div>
              <button class="secondary-button" @click="checkCurrentPlan"><BoltIcon /> 检查提醒</button>
              <button class="primary-button" @click="inspectPlanWithAgent"><SparklesIcon /> 交给 Agent</button>
            </div>
          </div>

          <div class="plan-timeline">
            <article v-for="stage in store.currentPlan.stages" :key="stage.id" :class="['timeline-stage', stage.status]">
              <div class="timeline-rail"><span>{{ stage.position + 1 }}</span><i></i></div>
              <section class="timeline-stage-body">
                <header class="timeline-stage-header">
                  <div>
                    <small>阶段 {{ stage.position + 1 }} · {{ stage.tasks.length }} 个任务</small>
                    <h3>{{ stage.title }}</h3>
                    <p v-if="stage.description">{{ stage.description }}</p>
                  </div>
                  <div class="stage-completion">
                    <strong>{{ stageProgress(stage) }}%</strong>
                    <span><i :style="{ width: `${stageProgress(stage)}%` }"></i></span>
                  </div>
                </header>

                <div class="timeline-tasks">
                  <article v-for="task in stage.tasks" :key="task.id" :class="['timeline-task', `status-${task.status}`]">
                    <div class="timeline-task-state">
                      <component :is="task.status === 'completed' ? CheckCircleIcon : task.status === 'active' ? PlayCircleIcon : ClockIcon" />
                    </div>
                    <div class="timeline-task-main">
                      <header>
                        <div><small>TASK {{ task.id }}</small><strong>{{ task.title }}</strong></div>
                        <span :class="['task-status', task.status]"><i></i>{{ statusLabel(task.status) }}</span>
                      </header>
                      <p>{{ task.description || '等待 Agent 补充任务说明。' }}</p>
                      <div class="timeline-task-meta">
                        <div class="task-facts">
                          <span><ClockIcon />{{ task.estimated_minutes }} 分钟</span>
                          <span><CalendarDaysIcon />截止 {{ formatDate(task.due_at, true) }}</span>
                          <span v-if="task.review_due_at" class="review-fact"><BoltIcon />复习 {{ formatDate(task.review_due_at, true) }}</span>
                        </div>
                        <div class="task-tags">
                          <span>{{ task.kind }}</span>
                          <span v-if="task.is_core || task.evidence_required" class="evidence-tag"><ShieldCheckIcon /> 核心 · 需证据</span>
                          <span v-if="wasTouchedByAgent(task)" class="agent-tag"><SparklesIcon /> Agent 已调整</span>
                        </div>
                      </div>
                      <footer>
                        <a v-if="task.resource_url" :href="task.resource_url" target="_blank" rel="noreferrer">学习资源 <ArrowTopRightOnSquareIcon /></a>
                        <span v-else>暂无外部资源</span>
                        <div class="task-actions">
                          <button @click="submitTaskToAgent(task)"><PaperClipIcon /> 提交成果</button>
                          <button @click="askAgentAboutTask(task)"><SparklesIcon /> 检查任务</button>
                        </div>
                      </footer>
                    </div>
                  </article>
                </div>
              </section>
            </article>
          </div>
        </main>

        <main v-else class="panel empty-state">该计划已不存在，返回计划列表重新选择。</main>
        <AgentComposer />
      </section>
    </div>
  </section>
</template>
