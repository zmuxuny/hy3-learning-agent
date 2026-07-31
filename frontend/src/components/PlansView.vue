<script setup>
import {
  AdjustmentsHorizontalIcon,
  ArrowTopRightOnSquareIcon,
  BoltIcon,
  CalendarDaysIcon,
  CheckCircleIcon,
  ClockIcon,
  DocumentCheckIcon,
  MapIcon,
  PlayCircleIcon,
  ShieldCheckIcon,
  SparklesIcon,
} from '@heroicons/vue/24/outline';
import { computed } from 'vue';
import { useWorkspaceStore } from '../stores/workspace';
import AgentComposer from './AgentComposer.vue';

const store = useWorkspaceStore();
const taskIds = computed(() => new Set(
  store.currentPlan?.stages.flatMap((stage) => stage.tasks.map((task) => String(task.id))) || [],
));
const planOperations = computed(() => store.operations.filter((operation) => (
  (operation.entity_type === 'plan' && operation.entity_id === String(store.currentPlan?.id))
  || (operation.entity_type === 'task' && taskIds.value.has(operation.entity_id))
)));

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

function inspectPlanWithAgent() {
  store.startRun(
    '请完整检查当前计划的阶段、任务、截止时间、复习安排和最近学习事件，识别风险并给出今天的最小可执行行动。必要时可进行低风险且可撤销的调整。',
    store.currentPlan.id,
  );
}
</script>

<template>
  <section class="view has-composer">
    <header class="view-header compact-header">
      <div>
        <span class="eyebrow">PLANS · EVIDENCE · OPERATIONS</span>
        <h1>学习计划</h1>
        <p>计划是 Agent 可以读取和操作的工作空间。核心任务必须有证据，所有 AI 修改都进入审计记录并可撤销。</p>
      </div>
      <div class="plan-header-actions" v-if="store.currentPlan">
        <button class="secondary-button" @click="store.triggerHeartbeat"><BoltIcon /> 主动检查</button>
        <button class="primary-button" @click="inspectPlanWithAgent"><SparklesIcon /> 交给 Agent</button>
      </div>
    </header>

    <div class="plans-layout">
      <aside class="plan-picker panel">
        <div class="plan-picker-title"><MapIcon /> 全部计划 <span>{{ store.plans.length }}</span></div>
        <button
          v-for="plan in store.plans"
          :key="plan.id"
          :class="{ active: store.currentPlan?.id === plan.id }"
          @click="store.selectPlan(plan.id)"
        >
          <span class="plan-dot"></span>
          <div>
            <strong>{{ plan.title }}</strong>
            <small>{{ statusLabel(plan.status) }} · {{ Math.round(plan.progress * 100) }}%</small>
            <span class="picker-progress"><i :style="{ width: `${Math.round(plan.progress * 100)}%` }"></i></span>
          </div>
        </button>
        <div v-if="!store.plans.length" class="empty-state compact">还没有计划</div>
      </aside>

      <main v-if="store.currentPlan" class="plan-detail">
        <article class="plan-hero panel">
          <div class="plan-hero-main">
            <div class="plan-kicker">
              <span class="live-badge"><i></i> Agent managed</span>
              <span>PLAN {{ store.currentPlan.id }} · VERSION {{ store.currentPlan.version }}</span>
            </div>
            <h2>{{ store.currentPlan.title }}</h2>
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

        <div class="harness-note">
          <SparklesIcon />
          <div><strong>Harness 管理模式</strong><p>任务卡上的操作会发给统一 Agent；Agent 先读取上下文，再通过工具修改计划，不绕过审计层。</p></div>
          <button @click="store.traceOpen = true">查看运行轨迹</button>
        </div>

        <div class="kanban">
          <article v-for="stage in store.currentPlan.stages" :key="stage.id" :class="['stage-column', stage.status]">
            <header>
              <div>
                <small>阶段 {{ stage.position + 1 }}</small>
                <h3>{{ stage.title }}</h3>
              </div>
              <span>{{ stageProgress(stage) }}%</span>
            </header>
            <div class="stage-progress"><i :style="{ width: `${stageProgress(stage)}%` }"></i></div>
            <p v-if="stage.description" class="stage-description">{{ stage.description }}</p>

            <div class="stage-tasks">
              <article v-for="task in stage.tasks" :key="task.id" :class="['task-card', `status-${task.status}`]">
                <header class="task-card-head">
                  <span class="task-id">TASK {{ task.id }}</span>
                  <span :class="['task-status', task.status]">
                    <i></i>{{ statusLabel(task.status) }}
                  </span>
                </header>
                <div class="task-title">
                  <component :is="task.status === 'completed' ? CheckCircleIcon : task.status === 'active' ? PlayCircleIcon : ClockIcon" />
                  <strong>{{ task.title }}</strong>
                </div>
                <p>{{ task.description || '等待 Agent 补充任务说明。' }}</p>

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

                <footer>
                  <a v-if="task.resource_url" :href="task.resource_url" target="_blank" rel="noreferrer">
                    学习资源 <ArrowTopRightOnSquareIcon />
                  </a>
                  <span v-else>暂无外部资源</span>
                  <button @click="askAgentAboutTask(task)"><SparklesIcon /> 让 Agent 检查</button>
                </footer>
              </article>
            </div>
          </article>
        </div>
      </main>

      <main v-else class="panel empty-state">选择一个计划，或让 Agent 创建完整计划。</main>
    </div>
    <AgentComposer />
  </section>
</template>
