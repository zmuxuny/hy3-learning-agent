<script setup>
import { CheckCircleIcon, KeyIcon, SparklesIcon } from '@heroicons/vue/24/outline';
import { computed, ref } from 'vue';
import { useShellStore } from '../stores/shell.js';
import { useSettingsStore } from '../stores/settings.js';

const settings = useSettingsStore();
const shell = useShellStore();
const step = ref(1);
const saving = ref(false);
const error = ref('');
const connectionVerified = ref(false);
const form = ref({
  base_url: settings.onboardingStatus?.base_url || 'https://tokenhub.tencentmaas.com/v1',
  model: settings.onboardingStatus?.model || 'hy3',
  api_key: '',
});
const goal = ref('');
const canVerify = computed(() => form.value.base_url.trim() && form.value.model.trim() && form.value.api_key.trim());

async function verifyAndSave() {
  if (!canVerify.value || saving.value) return;
  saving.value = true;
  error.value = '';
  connectionVerified.value = false;
  const payload = {
    base_url: form.value.base_url.trim(),
    model: form.value.model.trim(),
    api_key: form.value.api_key,
  };
  try {
    await settings.verifyModelConnection(payload);
    await settings.updateModelSettings({ ...payload, temperature: 0.9 });
    form.value.api_key = '';
    connectionVerified.value = true;
    step.value = 2;
  } catch (requestError) {
    error.value = requestError.response?.data?.detail?.code === 'model_connection_failed'
      ? '连接验证失败。请检查 TokenHub 密钥、模型名和网络后重试。'
      : '无法保存模型连接，请稍后重试。';
  } finally {
    saving.value = false;
  }
}

function reviewGoal() {
  if (!goal.value.trim()) return;
  error.value = '';
  step.value = 3;
}

async function startLearning() {
  if (!goal.value.trim() || saving.value) return;
  saving.value = true;
  error.value = '';
  const started = await shell.startRun(goal.value.trim(), null);
  if (!started) error.value = shell.error || '首个学习会话创建失败，请重试。';
  saving.value = false;
}
</script>

<template>
  <main class="onboarding-view">
    <section class="onboarding-panel" aria-labelledby="onboarding-title">
      <header>
        <span class="onboarding-mark"><SparklesIcon /></span>
        <p>Learning Agent 首次设置</p>
        <h1 id="onboarding-title">先连接混元，再开始第一段学习</h1>
        <span>密钥只写入本机 <code>.env</code>，连接验证不会保存模型回复。</span>
      </header>

      <ol class="onboarding-steps" aria-label="设置进度">
        <li :class="{ active: step === 1, done: step > 1 }"><b>1</b><span>连接模型</span></li>
        <li :class="{ active: step === 2, done: step > 2 }"><b>2</b><span>写下目标</span></li>
        <li :class="{ active: step === 3 }"><b>3</b><span>开始学习</span></li>
      </ol>

      <form v-if="step === 1" class="onboarding-form" @submit.prevent="verifyAndSave">
        <label>TokenHub Base URL<input v-model="form.base_url" type="url" required autocomplete="url" /></label>
        <label>模型<input v-model="form.model" required autocomplete="off" /></label>
        <label>API Key<input v-model="form.api_key" type="password" required autocomplete="new-password" /></label>
        <button class="primary-button" type="submit" :disabled="!canVerify || saving">
          <KeyIcon />{{ saving ? '正在验证…' : '验证并保存连接' }}
        </button>
      </form>

      <form v-else-if="step === 2" class="onboarding-form" @submit.prevent="reviewGoal">
        <div v-if="connectionVerified" class="onboarding-success"><CheckCircleIcon />混元连接已验证</div>
        <label>你现在最想学会什么？
          <textarea v-model="goal" rows="5" required placeholder="例如：用四周掌握 FastAPI，并完成一个可运行的小项目。"></textarea>
        </label>
        <div class="onboarding-actions">
          <button class="secondary-button" type="button" @click="step = 1">返回连接设置</button>
          <button class="primary-button" type="submit" :disabled="!goal.trim()">继续</button>
        </div>
      </form>

      <section v-else class="onboarding-review">
        <small>首个学习目标</small>
        <p>{{ goal }}</p>
        <span>Agent 会先澄清目标，再建立可追踪的学习计划。你随时可以修改方向。</span>
        <div class="onboarding-actions">
          <button class="secondary-button" type="button" @click="step = 2">修改目标</button>
          <button class="primary-button" type="button" :disabled="saving" @click="startLearning">
            {{ saving ? '正在创建会话…' : '开始第一段学习' }}
          </button>
        </div>
      </section>
      <p v-if="error" class="onboarding-error" role="alert">{{ error }}</p>
    </section>
  </main>
</template>
