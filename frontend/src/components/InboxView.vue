<script setup>
import { BellIcon, CheckCircleIcon, CheckIcon, EnvelopeIcon, ServerStackIcon, XCircleIcon } from '@heroicons/vue/24/outline';
import { useWorkspaceStore } from '../stores/workspace';
import RunTraceButton from './RunTraceButton.vue';

const store = useWorkspaceStore();

async function requestBrowserPermission() {
  if ('Notification' in window) await Notification.requestPermission();
}

function testEmail(channel, sendMessage = false) {
  store.testEmail(channel, sendMessage);
}
</script>

<template>
  <section class="view">
    <header class="view-header compact-header">
      <div><span class="eyebrow">PERSONAL INBOX</span><h1>收件箱</h1><p>学习提醒、抽查和调整建议统一进入这里；重要消息可以同步到邮箱。</p></div>
      <div class="page-header-actions">
        <RunTraceButton />
        <button class="secondary-button" @click="requestBrowserPermission"><BellIcon /> 启用浏览器通知</button>
      </div>
    </header>

    <section class="email-setup panel">
      <div class="email-setup-icon"><ServerStackIcon /></div>
      <div class="email-setup-copy">
        <header>
          <div><strong>邮箱通信</strong><span>SMTP 发送 · IMAP 回复</span></div>
          <em :class="{ ready: store.emailConfiguration?.smtp_configured && store.emailConfiguration?.imap_configured }">
            {{ store.emailConfiguration?.smtp_configured && store.emailConfiguration?.imap_configured ? '已配置' : '等待配置' }}
          </em>
        </header>
        <p v-if="!store.emailConfiguration?.smtp_configured || !store.emailConfiguration?.imap_configured">
          在项目根目录 <code>.env</code> 填写 SMTP/IMAP 主机、邮箱账号和应用专用密码，然后重启服务。密码只保存在本机，不会进入页面或 Git。
        </p>
        <p v-else>
          发件账号 {{ store.emailConfiguration.smtp_username }}，收件文件夹 {{ store.emailConfiguration.imap_folder }}；邮件回复会回到原来的连续对话。
        </p>
        <small v-if="store.emailConfiguration?.smtp_missing?.length">SMTP 缺少：{{ store.emailConfiguration.smtp_missing.join('、') }}</small>
        <small v-if="store.emailConfiguration?.imap_missing?.length">IMAP 缺少：{{ store.emailConfiguration.imap_missing.join('、') }}</small>
        <div class="email-test-actions" v-if="store.emailConfiguration?.smtp_configured || store.emailConfiguration?.imap_configured">
          <button v-if="store.emailConfiguration.smtp_configured" class="secondary-button" @click="testEmail('smtp', true)"><EnvelopeIcon /> 发送测试邮件</button>
          <button v-if="store.emailConfiguration.imap_configured" class="secondary-button" @click="testEmail('imap')"><ServerStackIcon /> 测试回复邮箱</button>
        </div>
        <div v-if="store.emailTestResult" :class="['email-test-result', { failed: !store.emailTestResult.ok }]">
          <component :is="store.emailTestResult.ok ? CheckCircleIcon : XCircleIcon" />
          {{ store.emailTestResult.ok ? '邮箱连接测试通过' : store.emailTestResult.error }}
        </div>
      </div>
    </section>

    <div class="inbox-list">
      <article v-for="item in store.notifications" :key="item.id" :class="['panel', 'inbox-card', { unread: !item.read_at }]">
        <div class="inbox-icon"><component :is="item.channel === 'email' ? EnvelopeIcon : BellIcon" /></div>
        <div><header><strong>{{ item.title }}</strong><span>{{ item.channel }} · {{ item.status }}</span></header><p>{{ item.body }}</p><small>{{ new Date(item.created_at).toLocaleString() }}</small></div>
        <button v-if="!item.read_at" title="标记已读" @click="store.markNotificationRead(item.id)"><CheckIcon /></button>
      </article>
      <div v-if="!store.notifications.length" class="panel empty-state">Agent 暂时没有需要主动告诉你的事情。</div>
    </div>
  </section>
</template>
