<script setup>
import { reactive, ref, watch } from 'vue';
import {
  BellIcon,
  CheckCircleIcon,
  ChatBubbleLeftRightIcon,
  EnvelopeIcon,
  ServerStackIcon,
  SparklesIcon,
  TrashIcon,
  XCircleIcon,
} from '@heroicons/vue/24/outline';
import { useSettingsStore } from '../stores/settings.js';

const settings = useSettingsStore();
const saving = ref('');
const feedback = ref('');
const feedbackError = ref(false);

const model = reactive({ base_url: '', model: '', api_key: '', temperature: 0.9 });
const email = reactive({
  smtp_host: '', smtp_port: 587, smtp_username: '', smtp_password: '',
  smtp_from: '', smtp_to: '', smtp_use_tls: true, smtp_use_ssl: false,
  enable_email_reply_polling: false,
  imap_host: '', imap_port: 993, imap_username: '', imap_password: '', imap_folder: 'INBOX',
});
const policy = reactive({ quiet_start: '23:00', quiet_end: '08:00', daily_limit: 3, cooldown_minutes: 180, paused: false });
const followup = ref('steer');

function syncAppSettings(app) {
  if (app) {
    model.base_url = app.base_url || '';
    model.model = app.model || '';
    model.temperature = app.temperature ?? 0.9;
  }
}

function syncEmailConfiguration(emailConfig) {
  if (emailConfig) {
    email.smtp_host = emailConfig.smtp_host || '';
    email.smtp_port = emailConfig.smtp_port || 587;
    // The API intentionally returns only masked identities. Keep editable
    // identity/secret fields blank so saving another field cannot persist the
    // mask as a real credential.
    email.smtp_use_tls = emailConfig.smtp_use_tls ?? true;
    email.smtp_use_ssl = emailConfig.smtp_use_ssl ?? false;
    email.imap_host = emailConfig.imap_host || '';
    email.imap_port = emailConfig.imap_port || 993;
    email.imap_folder = emailConfig.imap_folder || 'INBOX';
    email.enable_email_reply_polling = emailConfig.reply_polling_enabled ?? false;
  }
}

function syncProfile(profile) {
  if (profile) {
    policy.quiet_start = profile.quiet_hours?.start || '23:00';
    policy.quiet_end = profile.quiet_hours?.end || '08:00';
    policy.daily_limit = profile.daily_notification_limit ?? 3;
  }
}

function syncScheduler(status) {
  if (status) {
    policy.cooldown_minutes = settings.appSettings?.notification_cooldown_minutes ?? 180;
    policy.paused = Boolean(status.paused);
  }
}

watch(() => settings.appSettings, syncAppSettings, { immediate: true });
watch(() => settings.emailConfiguration, syncEmailConfiguration, { immediate: true });
watch(() => settings.profile, syncProfile, { immediate: true });
watch(() => settings.schedulerStatus, syncScheduler, { immediate: true });
watch(() => settings.followUpBehavior, (value) => { followup.value = value || 'steer'; }, { immediate: true });

async function run(label, action) {
  saving.value = label;
  feedback.value = '';
  feedbackError.value = false;
  try {
    await action();
    feedback.value = '设置已保存；邮箱、模型和提醒冷却时间在重启服务后生效，其余交互与通知偏好即时生效。';
  } catch (requestError) {
    feedbackError.value = true;
    feedback.value = requestError.response?.data?.detail || requestError.message;
  } finally {
    saving.value = '';
  }
}

function saveModel() {
  return run('model', async () => {
    const payload = {
      base_url: model.base_url.trim(),
      model: model.model.trim(),
      temperature: Number(model.temperature),
    };
    if (model.api_key.trim()) payload.api_key = model.api_key.trim();
    await settings.updateModelSettings(payload);
    model.api_key = '';
  });
}

function saveFollowup() {
  return run('followup', async () => {
    await settings.setFollowUpBehavior(followup.value);
  });
}

function saveEmail() {
  return run('email', async () => {
    const payload = {
      smtp_host: email.smtp_host,
      smtp_port: Number(email.smtp_port),
      smtp_use_tls: email.smtp_use_tls,
      smtp_use_ssl: email.smtp_use_ssl,
      enable_email_reply_polling: email.enable_email_reply_polling,
      imap_host: email.imap_host,
      imap_port: Number(email.imap_port),
      imap_folder: email.imap_folder,
    };
    for (const field of ['smtp_username', 'smtp_password', 'smtp_from', 'smtp_to', 'imap_username', 'imap_password']) {
      if (String(email[field] || '').trim()) payload[field] = String(email[field]).trim();
    }
    await settings.updateEmailSettings(payload);
    email.smtp_password = '';
    email.imap_password = '';
  });
}

function deleteEmailCredentials() {
  return run('email-delete', () => settings.deleteEmailCredentials());
}

function savePolicy() {
  return run('policy', async () => {
    await settings.updateNotificationPolicy({
      quiet_hours: { start: policy.quiet_start, end: policy.quiet_end },
      daily_notification_limit: Number(policy.daily_limit),
      cooldown_minutes: Number(policy.cooldown_minutes),
    });
    await settings.setProactivePaused(policy.paused);
    await Promise.allSettled([
      settings.loadProfile(),
      settings.loadSchedulerStatus(),
      settings.loadAppSettings(),
    ]);
  });
}
</script>

<template>
  <section class="view">
    <header class="view-header compact-header">
      <div><span class="eyebrow">SETTINGS</span><h1>设置</h1><p>模型、邮件与主动通知策略。凭据只保存在本地 `.env`，API 永不回传密码。</p></div>
    </header>

    <div v-if="feedback" :class="['settings-feedback', { failed: feedbackError }]">
      <component :is="feedbackError ? XCircleIcon : CheckCircleIcon" />
      {{ feedback }}
    </div>

    <section class="settings-section panel">
      <header>
        <div class="settings-icon"><SparklesIcon /></div>
        <div><small>MODEL</small><h2>模型连接</h2></div>
      </header>
      <div class="settings-form">
        <label class="settings-field"><span>API 地址</span><input v-model="model.base_url" placeholder="https://tokenhub.tencentmaas.com/v1" /></label>
        <label class="settings-field"><span>模型名称</span><input v-model="model.model" placeholder="hy3" /></label>
        <label class="settings-field"><span>API Key（留空则不修改）</span><input v-model="model.api_key" type="password" placeholder="已配置" /></label>
        <label class="settings-field"><span>温度</span><input v-model.number="model.temperature" type="number" min="0" max="2" step="0.1" /></label>
        <div class="settings-actions">
          <button class="primary-button" :disabled="saving === 'model'" @click="saveModel">
            {{ saving === 'model' ? '保存中…' : '保存模型设置' }}
          </button>
        </div>
      </div>
      <p class="settings-note">
        数据库文件：{{ settings.appSettings?.database_file || '未知' }}；当前数据量：计划 {{ settings.appSettings?.data_counts?.plans ?? '-' }} ·
        会话 {{ settings.appSettings?.data_counts?.sessions ?? '-' }} · 站内消息 {{ settings.appSettings?.data_counts?.notifications ?? '-' }} ·
        确认记忆 {{ settings.appSettings?.data_counts?.memories ?? '-' }}。清空数据请先停止服务，再运行 <code>./scripts/reset-data.sh</code>。
      </p>
    </section>

    <section class="settings-section panel">
      <header>
        <div class="settings-icon"><ChatBubbleLeftRightIcon /></div>
        <div><small>INTERACTION</small><h2>对话交互</h2></div>
      </header>
      <div class="settings-form">
        <label class="settings-field settings-select-field">
          <span>运行中发送消息的默认行为</span>
          <select v-model="followup">
            <option value="steer">转向当前运行（消息立即进入当前轮，不停止）</option>
            <option value="queue">排队到下一轮（完成/失败后续跑，主动停止时保留）</option>
          </select>
          <small>运行中按 Enter 使用此默认行为，按 Tab 直接排队；也可以点击输入框旁的按钮临时选择。</small>
        </label>
        <div class="settings-actions">
          <button class="primary-button" :disabled="saving === 'followup'" @click="saveFollowup">
            {{ saving === 'followup' ? '保存中…' : '保存交互偏好' }}
          </button>
        </div>
      </div>
    </section>

    <section class="settings-section panel">
      <header>
        <div class="settings-icon"><EnvelopeIcon /></div>
        <div><small>EMAIL</small><h2>邮件与回复</h2></div>
        <em :class="{ ready: settings.emailConfiguration?.smtp_configured && settings.emailConfiguration?.imap_configured }">
          {{ settings.emailConfiguration?.smtp_configured && settings.emailConfiguration?.imap_configured ? '已配置' : '等待配置' }}
        </em>
      </header>
      <div class="settings-form settings-grid">
        <label class="settings-field"><span>SMTP 主机</span><input v-model="email.smtp_host" placeholder="smtp.qq.com" /></label>
        <label class="settings-field"><span>SMTP 端口</span><input v-model.number="email.smtp_port" type="number" /></label>
        <label class="settings-field"><span>SMTP 账号（Agent 邮箱）</span><input v-model="email.smtp_username" :placeholder="settings.emailConfiguration?.smtp_username || '留空则不修改'" /></label>
        <label class="settings-field"><span>SMTP 授权码</span><input v-model="email.smtp_password" type="password" placeholder="留空则不修改" /></label>
        <label class="settings-field"><span>发件人地址</span><input v-model="email.smtp_from" :placeholder="settings.emailConfiguration?.smtp_from || '留空则不修改'" /></label>
        <label class="settings-field"><span>收件人地址（你的邮箱）</span><input v-model="email.smtp_to" :placeholder="settings.emailConfiguration?.smtp_to || '留空则不修改'" /></label>
        <label class="settings-field settings-check"><input v-model="email.smtp_use_tls" type="checkbox" /> STARTTLS（587）</label>
        <label class="settings-field settings-check"><input v-model="email.smtp_use_ssl" type="checkbox" /> SSL（465）</label>
        <label class="settings-field settings-check"><input v-model="email.enable_email_reply_polling" type="checkbox" /> 启用邮件回复轮询</label>
        <label class="settings-field"><span>IMAP 主机</span><input v-model="email.imap_host" placeholder="imap.qq.com" /></label>
        <label class="settings-field"><span>IMAP 端口</span><input v-model.number="email.imap_port" type="number" /></label>
        <label class="settings-field"><span>IMAP 账号</span><input v-model="email.imap_username" :placeholder="settings.emailConfiguration?.imap_username || '留空则不修改'" /></label>
        <label class="settings-field"><span>IMAP 授权码</span><input v-model="email.imap_password" type="password" placeholder="留空则不修改" /></label>
        <label class="settings-field"><span>IMAP 文件夹</span><input v-model="email.imap_folder" /></label>
      </div>
      <div class="settings-actions">
        <button class="primary-button" :disabled="saving === 'email'" @click="saveEmail">
          {{ saving === 'email' ? '保存中…' : '保存邮箱设置' }}
        </button>
        <button class="secondary-button" @click="settings.testEmail('smtp', true)">发送测试邮件</button>
        <button class="secondary-button" @click="settings.testEmail('imap')">测试回复邮箱</button>
        <button class="secondary-button danger" :disabled="saving === 'email-delete'" @click="deleteEmailCredentials"><TrashIcon /> 删除凭据</button>
      </div>
      <p class="settings-note">建议使用独立 Agent 邮箱 A 向你的日常邮箱 B 发送；`.env` 以 0600 权限保存。</p>
    </section>

    <section class="settings-section panel">
      <header>
        <div class="settings-icon"><BellIcon /></div>
        <div><small>NOTIFICATIONS</small><h2>主动通知策略</h2></div>
      </header>
      <div class="settings-form settings-grid">
        <label class="settings-field settings-check"><input v-model="policy.paused" type="checkbox" /> 暂停后台主动检查（仍可手动检查）</label>
        <label class="settings-field"><span>免打扰开始</span><input v-model="policy.quiet_start" type="time" /></label>
        <label class="settings-field"><span>免打扰结束</span><input v-model="policy.quiet_end" type="time" /></label>
        <label class="settings-field"><span>每日站内通知上限</span><input v-model.number="policy.daily_limit" type="number" min="0" max="20" /></label>
        <label class="settings-field"><span>同类提醒冷却（分钟）</span><input v-model.number="policy.cooldown_minutes" type="number" min="0" max="1440" /></label>
      </div>
      <div class="settings-actions">
        <button class="primary-button" :disabled="saving === 'policy'" @click="savePolicy">
          {{ saving === 'policy' ? '保存中…' : '保存通知策略' }}
        </button>
      </div>
      <p class="settings-note">冷却时间写入 `.env`，重启后生效；免打扰与每日上限即时生效。</p>
    </section>
  </section>
</template>
