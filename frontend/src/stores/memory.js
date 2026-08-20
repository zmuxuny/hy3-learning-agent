import { computed, ref } from 'vue';
import { defineStore } from 'pinia';
import api from '../api/client.js';
import { createLoadFence, errorMessage, replaceById } from './helpers.js';

export const useMemoryStore = defineStore('memory', () => {
  const memories = ref([]);
  const loading = ref(false);
  const error = ref('');
  const fence = createLoadFence();
  const pendingMemories = computed(() => memories.value.filter((memory) => memory.status === 'proposed'));

  async function loadMemories() {
    const generation = fence.next();
    loading.value = true;
    try {
      const response = await api.get('/memories');
      if (fence.current(generation)) {
        memories.value = response.data;
        error.value = '';
      }
      return response.data;
    } catch (requestError) {
      if (fence.current(generation)) error.value = errorMessage(requestError);
      throw requestError;
    } finally {
      if (fence.current(generation)) loading.value = false;
    }
  }

  async function confirmMemory(memoryId) {
    const response = await api.post(`/memories/${memoryId}/confirm`);
    memories.value = replaceById(memories.value, response.data);
    return response.data;
  }

  async function proposeMemoryCorrection(memoryId, content) {
    const original = memories.value.find((memory) => memory.id === memoryId);
    if (!original || !content.trim()) return null;
    const response = await api.post('/memories/proposals', {
      scope: original.scope,
      scope_id: original.scope_id,
      layer: original.layer,
      content: content.trim(),
      confidence: 1,
      supersedes_id: original.id,
    });
    memories.value = replaceById(memories.value, response.data);
    return response.data;
  }

  async function archiveMemory(memoryId) {
    const response = await api.delete(`/memories/${memoryId}`);
    memories.value = replaceById(memories.value, response.data);
    return response.data;
  }

  async function restoreMemory(memoryId) {
    const response = await api.post(`/memories/${memoryId}/restore`);
    memories.value = replaceById(memories.value, response.data);
    return response.data;
  }

  return {
    memories,
    loading,
    error,
    pendingMemories,
    loadMemories,
    confirmMemory,
    proposeMemoryCorrection,
    archiveMemory,
    restoreMemory,
  };
});
