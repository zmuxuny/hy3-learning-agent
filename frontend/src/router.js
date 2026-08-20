import { createRouter, createWebHistory } from 'vue-router';
import HomeView from './components/HomeView.vue';
import InboxView from './components/InboxView.vue';
import MemoryView from './components/MemoryView.vue';
import PlansView from './components/PlansView.vue';
import SettingsView from './components/SettingsView.vue';

export const routes = [
  { path: '/', name: 'home', component: HomeView },
  { path: '/sessions/:sessionId', name: 'session', component: HomeView, props: true },
  { path: '/plans', name: 'plans', component: PlansView },
  { path: '/plans/:planId', name: 'plan', component: PlansView, props: true },
  { path: '/inbox', name: 'inbox', component: InboxView },
  { path: '/inbox/:interventionId', name: 'inbox-intervention', component: HomeView, props: true },
  { path: '/memory', name: 'memory', component: MemoryView },
  { path: '/settings', name: 'settings', component: SettingsView },
  { path: '/archives', name: 'archives', component: PlansView },
];

const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
});

export default router;
