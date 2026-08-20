<script setup>
import {
  AdjustmentsHorizontalIcon,
  ArchiveBoxArrowDownIcon,
  ArrowLeftIcon,
  ArrowUturnLeftIcon,
  ArrowTopRightOnSquareIcon,
  BoltIcon,
  BookOpenIcon,
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
import { useRoute, useRouter } from 'vue-router';
import { usePlanStore } from '../stores/plan.js';
import { useRunStore } from '../stores/run.js';
import { useShellStore } from '../stores/shell.js';
import AgentComposer from './AgentComposer.vue';
import RunTraceButton from './RunTraceButton.vue';

const route = useRoute();
const router = useRouter();
const planStore = usePlanStore();
const runStore = useRunStore();
const shell = useShellStore();
const showArchivedPlans = computed({
  get: () => route.name === 'archives',
  set: (archived) => router.push({ name: archived ? 'archives' : 'plans' }),
});
const displayedPlans = computed(() => (showArchivedPlans.value ? planStore.archivedPlans : planStore.plans));
const taskIds = computed(() => new Set(
  planStore.currentPlan?.stages.flatMap((stage) => stage.tasks.map((task) => String(task.id))) || [],
));
const planOperations = computed(() => runStore.operations.filter((operation) => (
  (operation.entity_type === 'plan' && operation.entity_id === String(planStore.currentPlan?.id))
  || (operation.entity_type === 'task' && taskIds.value.has(operation.entity_id))
)));
const currentTask = computed(() => {
  const tasks = planStore.currentPlan?.stages.flatMap((stage) => stage.tasks) || [];
  return tasks.find((task) => task.status === 'active')
    || tasks.find((task) => task.status === 'blocked')
    || tasks.find((task) => task.status === 'pending')
    || null;
});
const competencyById = computed(() => new Map(
  (planStore.competencyGraph?.competencies || []).map((item) => [Number(item.id), item]),
));
const competencyRows = computed(() => (planStore.competencyGraph?.competencies || []).map((item) => {
  const links = (planStore.competencyGraph?.task_links || []).filter(
    (link) => Number(link.competency_id) === Number(item.id),
  );
  const evidence = planStore.evidenceObservations.filter((observation) => (
    observation.fact_kind === 'observation'
    && observation.competency_refs?.some((reference) => Number(reference.competency_id) === Number(item.id))
  ));
  return { ...item, links, evidence };
}));
const evidenceTimeline = computed(() => planStore.evidenceObservations.slice(0, 12));

function taskCompetencyLinks(taskId) {
  return (planStore.competencyGraph?.task_links || []).filter(
    (link) => Number(link.task_id) === Number(taskId),
  );
}

function competencyTitle(competencyId) {
  return competencyById.value.get(Number(competencyId))?.title || `技能 ${competencyId}`;
}

function relationLabel(relation) {
  return relation === 'assesses' ? '证明' : relation === 'teaches' ? '训练' : relation;
}

function evidenceStageLabel(stage) {
  return {
    unknown: '尚不足以判断',
    exposed: '已接触',
    practicing: '练习中',
    demonstrated: '已证明',
    retained: '已保持',
  }[stage] || stage;
}

function evidenceSourceLabel(sourceType) {
  return {
    submission_checked: '成果验收',
    quiz_graded: '考核评分',
    task_evidence: '任务证据',
    self_report: '学习者自述',
    email_reply: '邮件回复',
  }[sourceType] || sourceType;
}

function taskTitle(taskId) {
  return planStore.currentPlan?.stages
    .flatMap((stage) => stage.tasks)
    .find((task) => Number(task.id) === Number(taskId))?.title || '计划级证据';
}

function tasksFor(plan) {
  return plan.stages.flatMap((stage) => stage.tasks);
}

function completedTasks(plan) {
  return tasksFor(plan).filter((task) => task.status === 'completed').length;
}

function operationCount(plan) {
  const ids = new Set(tasksFor(plan).map((task) => String(task.id)));
  return runStore.operations.filter((operation) => (
    (operation.entity_type === 'plan' && operation.entity_id === String(plan.id))
    || (operation.entity_type === 'task' && ids.has(operation.entity_id))
  )).length;
}

function statusLabel(status) {
  return { pending: '待开始', active: '进行中', paused: '已暂停', completed: '已完成', archived: '已归档', blocked: '受阻', skipped: '已跳过' }[status] || status;
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
  return runStore.operations.some((operation) => operation.entity_type === 'task' && operation.entity_id === String(task.id));
}

function askAgentAboutTask(task) {
  shell.startRun(
    `请检查任务 ${task.id}「${task.title}」的当前状态、证据要求和截止时间。先读取计划，再告诉我今天如何推进；如需调整，只执行低风险且可撤销的修改。`,
    planStore.currentPlan.id,
  );
}

function submitTaskToAgent(task) {
  shell.startRun(
    `我要提交任务 ${task.id}「${task.title}」的学习成果。请先读取任务要求，询问我需要提交的文字、文件路径、代码或链接；收到后使用 submission_create 保存证据，必要时读取文件或运行代码，再用 submission_check 给出验收结果。`,
    planStore.currentPlan.id,
  );
}

function teachNextStep() {
  shell.startRun(
    '请作为我的学习导师带我完成当前计划的下一步。先读取完整计划、最近提交与学习事件、到期复习和已保存资源，准确判断我进行到哪里；只选择一个最合适的当前任务，解释为什么现在做它，然后讲清必要概念并给我一个小练习。等我回答或提交证据后再继续，不要一次性倾倒整门课程。',
    planStore.currentPlan.id,
  );
}

function findPlanResources() {
  shell.startRun(
    '请为当前计划补充真正可学习的具体资源，而不是只列 API 文档。先读取计划和当前进度，再分别搜索：一门结构化课程或课程主页、一个中文或低门槛教程、一个带练习的实验/项目资源，以及必要时的一份权威参考。可以考虑 Coursera、edX、Hugging Face Learn、Kaggle Learn、CS DIY、Stanford 课程（如主题匹配时的 CS336）、freeCodeCamp、菜鸟教程等，但要按我的目标筛选。逐个 web_open 核验后，用 resource_save 保存类型、难度、语言、内容摘要和推荐理由；不要保存搜索结果页。',
    planStore.currentPlan.id,
  );
}

function resourceTypeLabel(value) {
  return { course: '课程', tutorial: '教程', lab: '实验', documentation: '参考文档', video: '视频', book: '书籍', repository: '项目仓库', curriculum: '学习路径' }[value] || '学习资源';
}

function checkCurrentPlan() {
  shell.startRun(
    '请主动检查当前计划现在是否有逾期、阻塞、待复习或需要提醒的事项。先读取真实状态；如果没有需要干预的内容，直接说明无需动作。',
    planStore.currentPlan.id,
  );
}

function createPlanWithAgent() {
  shell.startNewConversation();
  shell.startRun('我想制定一份新的学习计划。请先了解我真正想达到什么结果；把已确认的信息和最关键的待确认问题记录成需求卡片，不要现在就创建正式计划。信息充分后，你可以把资源调研、课程结构和考核审查分给规划子 Agent，最后给我一份可讨论、可确认的计划提案。');
}
</script>

<template>
  <section v-if="route.name !== 'plan'" class="view plan-index-view">
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
        <div class="plan-filter-tabs">
          <button :class="{ active: !showArchivedPlans }" @click="showArchivedPlans = false">当前计划 <span>{{ planStore.plans.length }}</span></button>
          <button :class="{ active: showArchivedPlans }" @click="showArchivedPlans = true">已归档 <span>{{ planStore.archivedPlans.length }}</span></button>
        </div>
        <small>按最近更新排序</small>
      </div>

      <div v-if="displayedPlans.length" class="plan-list">
        <article v-for="plan in displayedPlans" :key="plan.id" :class="['plan-list-card', { archived: plan.status === 'archived' }]">
          <button class="plan-list-open" @click="shell.selectPlan(plan.id)">
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
            <div class="plan-list-enter"><span>进入计划</span><ChevronRightIcon /></div>
          </button>
          <button
            class="plan-lifecycle-button"
            :title="plan.status === 'archived' ? '恢复计划' : '归档计划'"
            @click="shell.setPlanArchived(plan.id, plan.status !== 'archived')"
          >
            <component :is="plan.status === 'archived' ? ArrowUturnLeftIcon : ArchiveBoxArrowDownIcon" />
            {{ plan.status === 'archived' ? '恢复' : '归档' }}
          </button>
        </article>
      </div>

      <div v-else class="plan-list-empty panel">
        <MapIcon />
        <h2>{{ showArchivedPlans ? '还没有归档计划' : '还没有学习计划' }}</h2>
        <button v-if="!showArchivedPlans" class="primary-button" @click="createPlanWithAgent"><PlusIcon /> 用 Agent 创建</button>
      </div>
    </div>
  </section>

  <section v-else class="view has-composer">
    <header v-if="planStore.currentPlan" class="plan-detail-toolbar">
      <button class="back-button icon-back" aria-label="返回所有计划" @click="router.push({ name: 'plans' })"><ArrowLeftIcon /></button>
      <h1>{{ planStore.currentPlan.title }}</h1>
      <div class="plan-detail-actions">
        <button
          class="secondary-button"
          @click="shell.setPlanArchived(planStore.currentPlan.id, planStore.currentPlan.status !== 'archived')"
        >
          <component :is="planStore.currentPlan.status === 'archived' ? ArrowUturnLeftIcon : ArchiveBoxArrowDownIcon" />
          {{ planStore.currentPlan.status === 'archived' ? '恢复计划' : '归档计划' }}
        </button>
        <RunTraceButton />
      </div>
    </header>

    <div class="plan-detail-layout">
      <section class="plan-workspace">
        <main v-if="planStore.currentPlan" class="plan-detail">
          <article class="plan-hero">
            <div class="plan-hero-main">
              <p>{{ planStore.currentPlan.goal }}</p>
              <div class="plan-meta-grid">
                <div><CalendarDaysIcon /><span><small>最终期限</small><strong>{{ formatDate(planStore.currentPlan.deadline, true) }}</strong></span></div>
                <div><ClockIcon /><span><small>每周投入</small><strong>{{ planStore.currentPlan.weekly_minutes }} 分钟</strong></span></div>
                <div><MapIcon /><span><small>计划结构</small><strong>{{ planStore.currentPlan.stages.length }} 阶段 · {{ taskIds.size }} 任务</strong></span></div>
                <div><AdjustmentsHorizontalIcon /><span><small>Agent 操作</small><strong>{{ planOperations.length }} 条可审计记录</strong></span></div>
              </div>
            </div>
            <div class="plan-progress-block">
              <div class="plan-score" :style="{ '--progress': `${Math.round(planStore.currentPlan.progress * 360)}deg` }">
                <span><strong>{{ Math.round(planStore.currentPlan.progress * 100) }}%</strong><small>整体进度</small></span>
              </div>
              <div class="plan-output"><DocumentCheckIcon /><span><small>期望产出</small><strong>{{ planStore.currentPlan.expected_outcome || '等待补充' }}</strong></span></div>
            </div>
          </article>

          <div class="plan-action-row">
            <span><i></i><strong>当前一步</strong>{{ currentTask?.title || '计划已完成' }} · 版本 {{ planStore.currentPlan.version }}</span>
            <div>
              <button class="secondary-button" @click="checkCurrentPlan"><BoltIcon /> 检查提醒</button>
              <button class="primary-button" @click="teachNextStep"><SparklesIcon /> 教我下一步</button>
            </div>
          </div>

          <section class="plan-resources-section">
            <header>
              <div>
                <small>CURATED LEARNING SOURCES</small>
                <h2>精选学习资源</h2>
                <p>按当前阶段挑选的课程、教程和动手练习；旧版搜索结果会标为未核验存档。</p>
              </div>
              <button class="secondary-button" @click="findPlanResources"><BookOpenIcon /> 补充资源</button>
            </header>
            <div v-if="planStore.planResources.length" class="resource-list">
              <a
                v-for="resource in planStore.planResources"
                :key="resource.id"
                :href="resource.url"
                target="_blank"
                rel="noreferrer"
                :class="['resource-row', { legacy: !resource.verified_at }]"
              >
                <span class="resource-mark"><BookOpenIcon /></span>
                <span class="resource-copy">
                  <span class="resource-meta">
                    <em>{{ resource.provider || '公开网络' }}</em>
                    <i>{{ resourceTypeLabel(resource.resource_type) }}</i>
                    <i v-if="!resource.verified_at">未核验存档</i>
                    <i v-if="resource.difficulty && resource.difficulty !== 'mixed'">{{ resource.difficulty }}</i>
                    <i v-if="resource.language">{{ resource.language }}</i>
                  </span>
                  <strong>{{ resource.title }}</strong>
                  <small>{{ resource.why_recommended || resource.summary || '已保存到当前计划。' }}</small>
                </span>
                <ArrowTopRightOnSquareIcon class="resource-open" />
              </a>
            </div>
            <div v-else class="resource-empty">
              <span>还没有经过筛选的课程资源。</span>
              <button @click="findPlanResources">让 Agent 搜索并核验</button>
            </div>
          </section>

          <section class="learning-map-section" aria-labelledby="learning-map-title">
            <header>
              <div>
                <small>SKILLS · EVIDENCE · SOURCES</small>
                <h2 id="learning-map-title">学习依据</h2>
                <p>只展示计划已明确映射的技能和不可变证据，不把完成进度推断成掌握度。</p>
              </div>
              <span class="readonly-badge">只读 · 图版本 {{ planStore.competencyGraph?.revision ?? 0 }}</span>
            </header>

            <div v-if="competencyRows.length" class="competency-map-list">
              <article v-for="competency in competencyRows" :key="competency.id" class="competency-map-row">
                <div>
                  <small>{{ competency.scope === 'global' ? '跨计划技能' : '当前计划技能' }}</small>
                  <strong>{{ competency.title }}</strong>
                  <p>{{ competency.description || '该技能由任务映射和证据事实共同解释。' }}</p>
                </div>
                <div class="competency-map-relations">
                  <span
                    v-for="link in competency.links"
                    :key="link.id"
                    :class="[`relation-${link.relation}`]"
                  >{{ relationLabel(link.relation) }} · {{ taskTitle(link.task_id) }}</span>
                  <span v-if="!competency.links.length">尚未映射任务</span>
                </div>
                <div class="competency-evidence-count">
                  <strong>{{ competency.evidence.length }}</strong>
                  <small>条关联证据</small>
                </div>
              </article>
            </div>
            <p v-else class="learning-map-empty">当前计划尚未建立技能映射；Agent 不会根据相似标题自动合并或猜测。</p>

            <div class="evidence-ledger">
              <div class="evidence-ledger-heading">
                <h3>最近 Evidence</h3>
                <span>{{ planStore.evidenceObservations.length }} 条事实</span>
              </div>
              <article v-for="observation in evidenceTimeline" :key="observation.id" class="evidence-row">
                <time :datetime="observation.occurred_at">{{ formatUpdated(observation.occurred_at) }}</time>
                <div>
                  <strong>{{ taskTitle(observation.task_id) }}</strong>
                  <p>{{ evidenceSourceLabel(observation.source_type) }} · {{ observation.outcome }}</p>
                  <span v-if="observation.competency_refs?.length" class="evidence-competencies">
                    {{ observation.competency_refs.map((reference) => competencyTitle(reference.competency_id)).join(' · ') }}
                  </span>
                </div>
                <div class="evidence-stage">
                  <strong>{{ evidenceStageLabel(observation.eligibility_stage) }}</strong>
                  <small>{{ observation.counts_as_success ? '计入成功证据' : observation.eligibility_reason }}</small>
                </div>
              </article>
              <p v-if="!evidenceTimeline.length" class="learning-map-empty">完成学习并提交成果后，来源、时间和适用技能会显示在这里。</p>
            </div>
          </section>

          <div class="plan-timeline">
            <article v-for="stage in planStore.currentPlan.stages" :key="stage.id" :class="['timeline-stage', stage.status]">
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
                  <article v-for="task in stage.tasks" :key="task.id" :class="['timeline-task', `status-${task.status}`, { 'is-current': task.id === currentTask?.id }]">
                    <div class="timeline-task-state">
                      <component :is="task.status === 'completed' ? CheckCircleIcon : task.status === 'active' ? PlayCircleIcon : ClockIcon" />
                    </div>
                    <div class="timeline-task-main">
                      <header>
                        <div><small>TASK {{ task.id }}</small><strong>{{ task.title }}</strong></div>
                        <span :class="['task-status', task.status]"><i></i>{{ task.id === currentTask?.id && task.status === 'pending' ? '当前建议' : statusLabel(task.status) }}</span>
                      </header>
                      <p>{{ task.description || '等待 Agent 补充任务说明。' }}</p>
                      <div v-if="taskCompetencyLinks(task.id).length" class="task-learning-relations">
                        <span
                          v-for="link in taskCompetencyLinks(task.id)"
                          :key="link.id"
                          :class="[`relation-${link.relation}`]"
                        >{{ relationLabel(link.relation) }} · {{ competencyTitle(link.competency_id) }}</span>
                      </div>
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
