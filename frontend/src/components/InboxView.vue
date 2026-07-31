<script setup>
import { BellIcon, CheckIcon, EnvelopeIcon } from '@heroicons/vue/24/outline';
import { useWorkspaceStore } from '../stores/workspace';

const store = useWorkspaceStore();

async function requestBrowserPermission() {
  if ('Notification' in window) await Notification.requestPermission();
}
</script>

<template>
  <section class="view">
    <header class="view-header compact-header">
      <div><span class="eyebrow">PROACTIVE INBOX</span><h1>主动消息</h1><p>提醒、抽查和调整建议统一进入这里；浏览器与邮件是可选投递渠道。</p></div>
      <button class="secondary-button" @click="requestBrowserPermission"><BellIcon /> 启用浏览器通知</button>
    </header>

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
