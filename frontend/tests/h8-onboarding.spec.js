import { flushPromises, mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import api from '../src/api/client.js';
import OnboardingView from '../src/components/OnboardingView.vue';
import router from '../src/router.js';
import { useShellStore } from '../src/stores/shell.js';
import { useSettingsStore } from '../src/stores/settings.js';

function coreResponse(path, onboarding) {
  const responses = {
    '/profile': { owner_id: 'local' },
    '/settings/onboarding': onboarding,
    '/plans': [],
    '/agent/sessions': [],
    '/agent/runs': [],
    '/agent/queue': [],
  };
  if (path in responses) return Promise.resolve({ data: responses[path] });
  return Promise.reject(new Error(`optional unavailable: ${path}`));
}

describe('H8 first-run onboarding', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('redirects only a fresh zero-session installation without a key', async () => {
    vi.spyOn(api, 'get').mockImplementation((path) => coreResponse(path, {
      api_key_configured: false,
      session_count: 0,
      requires_onboarding: true,
      model: 'hy3',
      base_url: 'https://tokenhub.tencentmaas.com/v1',
    }));
    const replace = vi.spyOn(router, 'replace').mockResolvedValue();

    await useShellStore().bootstrap();

    expect(replace).toHaveBeenCalledWith({ name: 'onboarding' });
    expect(useSettingsStore().onboardingStatus.requires_onboarding).toBe(true);
  });

  it('keeps failed credentials on step one and never creates a Run', async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const shell = useShellStore();
    const start = vi.spyOn(shell, 'startRun').mockResolvedValue(true);
    vi.spyOn(api, 'post').mockRejectedValue({
      response: { data: { detail: { code: 'model_connection_failed' } } },
    });
    const put = vi.spyOn(api, 'put');
    const wrapper = mount(OnboardingView, { global: { plugins: [pinia] } });

    const inputs = wrapper.findAll('input');
    await inputs[0].setValue('https://tokenhub.tencentmaas.com/v1');
    await inputs[1].setValue('hy3');
    await inputs[2].setValue('bad-key');
    await wrapper.find('form').trigger('submit');
    await flushPromises();

    expect(wrapper.text()).toContain('连接验证失败');
    expect(wrapper.find('input[type="password"]').exists()).toBe(true);
    expect(put).not.toHaveBeenCalled();
    expect(start).not.toHaveBeenCalled();
  });

  it('verifies, saves, then creates exactly one first learning Run from the stated goal', async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const shell = useShellStore();
    const start = vi.spyOn(shell, 'startRun').mockResolvedValue(true);
    vi.spyOn(api, 'post').mockResolvedValue({ data: { ok: true } });
    vi.spyOn(api, 'put').mockResolvedValue({
      data: {
        restart_required: false,
        api_key_configured: true,
        model: 'hy3',
        base_url: 'https://tokenhub.tencentmaas.com/v1',
        temperature: 0.9,
      },
    });
    const wrapper = mount(OnboardingView, { global: { plugins: [pinia] } });

    const inputs = wrapper.findAll('input');
    await inputs[2].setValue('synthetic-key');
    await wrapper.find('form').trigger('submit');
    await flushPromises();
    expect(wrapper.text()).toContain('混元连接已验证');

    await wrapper.find('textarea').setValue('四周掌握 FastAPI 并完成项目');
    await wrapper.find('form').trigger('submit');
    await wrapper.get('button.primary-button').trigger('click');
    await flushPromises();

    expect(api.post).toHaveBeenCalledWith('/settings/model/test', {
      base_url: 'https://tokenhub.tencentmaas.com/v1',
      model: 'hy3',
      api_key: 'synthetic-key',
    });
    expect(api.put).toHaveBeenCalledWith('/settings/model', {
      base_url: 'https://tokenhub.tencentmaas.com/v1',
      model: 'hy3',
      api_key: 'synthetic-key',
      temperature: 0.9,
    });
    expect(start).toHaveBeenCalledOnce();
    expect(start).toHaveBeenCalledWith('四周掌握 FastAPI 并完成项目', null);
  });
});
