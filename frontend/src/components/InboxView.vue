<script setup>
import { ArchiveBoxArrowDownIcon, ArrowUturnLeftIcon, BellIcon, BoltIcon, CheckCircleIcon, CheckIcon, ClockIcon, EnvelopeIcon, ServerStackIcon, XCircleIcon } from '@heroicons/vue/24/outline';
import { computed, ref } from 'vue';
import { useWorkspaceStore } from '../stores/workspace';
import RunTraceButton from './RunTraceButton.vue';

const store = useWorkspaceStore();
const heartbeatMinutes = computed(() => Math.max(1, Math.round((store.schedulerStatus?.interval_seconds || 300) / 60)));
const showArchived = ref(false);
const displayedNotifications = computed(() => (
  showArchived.value ? store.archivedNotifications : store.notifications
));
const readActiveCount = computed(() => store.notifications.filter((item) => item.read_at).length);

function formatTime(value, fallback = '等待首次检查') {
  return value ? new Date(value).toLocaleString() : fallback;
}

function decisionLabel(value) {
  return ({
    waiting_for_first_cycle: '等待首次自动检查',
    quiet_no_intervention_needed: '上次判断：无需打扰',
    heartbeat_already_running: '上次检查仍在运行',
    due_review: '发现到期复习并已交给 Agent',
    task_due_within_24h: '发现临近截止任务并已交给 Agent',
    progress_checkin_due: '发现进度需要跟进并已交给 Agent',
    cycle_error: '上轮候选扫描失败，将在下一轮重试',
  })[value] || '后台会按真实状态决定是否介入';
}

async function requestBrowserPermission() {
  await store.enableBrowserNotifications();
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

    <section class="proactive-status panel">
      <div class="proactive-status-icon"><BoltIcon /></div>
      <div class="proactive-status-copy">
        <header>
          <div><strong>主动检查已{{ store.schedulerStatus?.enabled ? '开启' : '关闭' }}</strong><span>全局心跳 · 不是每个任务各跑一个定时器</span></div>
          <em :class="{ active: store.schedulerStatus?.active }">{{ store.schedulerStatus?.active ? '检查中' : `${heartbeatMinutes} 分钟一轮` }}</em>
        </header>
        <p>每轮先读取到期复习、临近截止任务和学习活动；有证据需要介入时才启动 Hy3。超过 {{ store.schedulerStatus?.progress_checkin_hours || 24 }} 小时没有学习证据时，它也可以主动询问进度。站内收件箱无需配置邮箱。</p>
        <div class="proactive-status-facts">
          <span><ClockIcon /><small>下次检查</small><strong>{{ formatTime(store.schedulerStatus?.next_cycle_at) }}</strong></span>
          <span><CheckCircleIcon /><small>最近判断</small><strong>{{ decisionLabel(store.schedulerStatus?.last_decision) }}</strong></span>
        </div>
      </div>
      <button class="secondary-button" :disabled="store.schedulerStatus?.active" @click="store.triggerHeartbeat"><BoltIcon /> 立即检查</button>
    </section>

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
          邮箱只是可选的离站渠道，不影响站内主动提醒。若需要邮件，必须有一个真实发件服务：可以复用个人邮箱的应用专用密码，也可以接入事务邮件 SMTP 服务；项目无法在没有任何发件凭据时替代邮件服务商发信。
        </p>
        <p v-else>
          发件账号 {{ store.emailConfiguration.smtp_username }}，收件文件夹 {{ store.emailConfiguration.imap_folder }}；邮件回复会回到原来的连续对话。
        </p>
        <small v-if="store.emailConfiguration?.smtp_missing?.length">SMTP 缺少：{{ store.emailConfiguration.smtp_missing.join('、') }}</small>
        <small v-if="store.emailConfiguration?.imap_missing?.length">IMAP 缺少：{{ store.emailConfiguration.imap_missing.join('、') }}</small>
        <small v-for="warning in store.emailConfiguration?.warnings || []" :key="warning" class="email-warning">{{ warning }}</small>
        <div class="email-test-actions" v-if="store.emailConfiguration?.smtp_configured || store.emailConfiguration?.imap_configured">
          <button class="secondary-button" @click="store.openView('settings')"><ServerStackIcon /> 配置</button>
          <button v-if="store.emailConfiguration.smtp_configured" class="secondary-button" @click="testEmail('smtp', true)"><EnvelopeIcon /> 发送测试邮件</button>
          <button v-if="store.emailConfiguration.imap_configured" class="secondary-button" @click="testEmail('imap')"><ServerStackIcon /> 测试回复邮箱</button>
        </div>
        <div v-if="store.emailTestResult" :class="['email-test-result', { failed: !store.emailTestResult.ok }]">
          <component :is="store.emailTestResult.ok ? CheckCircleIcon : XCircleIcon" />
          {{ store.emailTestResult.ok ? '邮箱连接测试通过' : store.emailTestResult.error }}
        </div>
      </div>
    </section>

    <div class="inbox-toolbar">
      <div class="inbox-tabs" aria-label="收件箱筛选">
        <button :class="{ active: !showArchived }" @click="showArchived = false">收件箱 <span>{{ store.notifications.length }}</span></button>
        <button :class="{ active: showArchived }" @click="showArchived = true">已归档 <span>{{ store.archivedNotifications.length }}</span></button>
      </div>
      <button v-if="!showArchived && readActiveCount" class="quiet-button" @click="store.archiveReadNotifications">
        <ArchiveBoxArrowDownIcon />归档全部已读
      </button>
      <small>归档不会删除消息，可随时恢复。</small>
    </div>

    <div class="inbox-list">
      <article v-for="item in displayedNotifications" :key="item.id" :class="['panel', 'inbox-card', { unread: !item.read_at }]">
        <div class="inbox-icon"><component :is="item.channel === 'email' ? EnvelopeIcon : BellIcon" /></div>
        <div><header><strong>{{ item.title }}</strong><span>{{ item.channel }} · {{ item.status }}</span></header><p>{{ item.body }}</p><small>{{ new Date(item.created_at).toLocaleString() }}</small></div>
        <div class="inbox-actions">
          <button v-if="!item.read_at" title="标记已读" @click="store.markNotificationRead(item.id)"><CheckIcon /></button>
          <button :title="showArchived ? '恢复消息' : '归档消息'" @click="store.setNotificationArchived(item.id, !showArchived)">
            <component :is="showArchived ? ArrowUturnLeftIcon : ArchiveBoxArrowDownIcon" />
          </button>
        </div>
      </article>
      <div v-if="!displayedNotifications.length" class="panel empty-state">
        {{ showArchived ? '还没有归档消息。' : 'Agent 暂时没有需要主动告诉你的事情。' }}
      </div>
    </div>
  </section>
</template>
