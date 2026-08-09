<script setup>
import {
  ArrowRightIcon,
  ArrowsPointingOutIcon,
  CalendarDaysIcon,
  CheckIcon,
  ChevronDownIcon,
  ClipboardIcon,
  LightBulbIcon,
} from '@heroicons/vue/24/outline';
import { computed, ref } from 'vue';
import { useWorkspaceStore } from '../stores/workspace';

const store = useWorkspaceStore();
const props = defineProps({
  plan: { type: Object, required: true },
});
const expanded = ref(false);
const copied = ref(false);
const plan = computed(() => [...store.plans, ...store.archivedPlans].find(
  (item) => Number(item.id) === Number(props.plan.id),
) || props.plan);
const stages = computed(() => plan.value?.stages || []);
const taskCount = computed(() => stages.value.reduce(
  (count, stage) => count + (stage.tasks?.length || 0), 0,
));

async function copyPlan() {
  const lines = [
    `# ${plan.value.title}`,
    '',
    plan.value.goal || '',
    '',
    ...stages.value.flatMap((stage, index) => [
      `## ${index + 1}. ${stage.title}`,
      stage.description || '',
      ...(stage.tasks || []).map((task) => `- [${task.status === 'completed' ? 'x' : ' '}] ${task.title}`),
      '',
    ]),
  ];
  await navigator.clipboard?.writeText(lines.join('\n').trim());
  copied.value = true;
  window.setTimeout(() => { copied.value = false; }, 1400);
}
</script>

<template>
  <section v-if="plan" :class="['artifact-preview', 'plan-artifact', { expanded }]">
    <header class="artifact-toolbar">
      <span class="artifact-kind"><LightBulbIcon /> 学习计划</span>
      <div class="artifact-actions">
        <button :title="copied ? '已复制' : '复制计划'" @click="copyPlan">
          <CheckIcon v-if="copied" /><ClipboardIcon v-else />
        </button>
        <button title="打开完整计划" @click="store.selectPlan(plan.id)"><ArrowsPointingOutIcon /></button>
      </div>
    </header>

    <div class="artifact-viewport">
      <div class="artifact-document">
        <h2>{{ plan.title }}</h2>
        <p class="artifact-lead">{{ plan.goal || '围绕当前目标持续推进、提交证据并接受阶段检查。' }}</p>
        <div class="artifact-facts">
          <span>{{ stages.length }} 个阶段</span>
          <span>{{ taskCount }} 个任务</span>
          <span>{{ Math.round((plan.progress || 0) * 100) }}% 完成</span>
          <span v-if="plan.deadline"><CalendarDaysIcon /> {{ new Date(plan.deadline).toLocaleDateString('zh-CN') }} 截止</span>
        </div>
        <div class="artifact-stage-list">
          <article v-for="(stage, index) in stages" :key="stage.id || `${index}-${stage.title}`">
            <small>阶段 {{ index + 1 }}</small>
            <h3>{{ stage.title }}</h3>
            <p>{{ stage.description || stage.objectives?.join(' · ') }}</p>
            <ul v-if="stage.tasks?.length">
              <li v-for="task in stage.tasks.slice(0, expanded ? stage.tasks.length : 3)" :key="task.id || task.title">
                <i :class="{ done: task.status === 'completed' }"><CheckIcon v-if="task.status === 'completed'" /></i>
                <span>{{ task.title }}</span>
              </li>
            </ul>
          </article>
        </div>
      </div>
    </div>

    <button class="artifact-expand" :title="expanded ? '收起预览' : '展开预览'" @click="expanded = !expanded">
      <ChevronDownIcon />
    </button>
    <footer class="artifact-footer">
      <button @click="store.selectPlan(plan.id)">打开计划</button>
      <button class="artifact-primary-action" @click="store.continueInPlan(plan.id)">在计划中继续 <ArrowRightIcon /></button>
    </footer>
  </section>
</template>
