import { computed, ref } from 'vue';
import { defineStore } from 'pinia';
import api from '../api/client.js';
import { createLoadFence, errorMessage, removeById, replaceById, sameId } from './helpers.js';

export const usePlanStore = defineStore('plan', () => {
  const plans = ref([]);
  const archivedPlans = ref([]);
  const currentPlan = ref(null);
  const planResources = ref([]);
  const competencyGraph = ref(null);
  const evidenceObservations = ref([]);
  const focusPlanId = ref(null);
  const loading = ref({});
  const errors = ref({});
  const activeFence = createLoadFence();
  const archiveFence = createLoadFence();
  const detailFence = createLoadFence();
  const resourceFence = createLoadFence();
  const competencyFence = createLoadFence();
  const evidenceFence = createLoadFence();
  const archivedTombstones = new Map();

  const activePlans = computed(() => plans.value.filter((plan) => plan.status === 'active'));
  const allPlans = computed(() => [...plans.value, ...archivedPlans.value]);
  const focusedPlan = computed(() => plans.value.find(
    (plan) => sameId(plan.id, focusPlanId.value) && plan.status !== 'archived',
  ) || null);

  function reconcileFocus() {
    if (focusPlanId.value != null && !focusedPlan.value) focusPlanId.value = null;
    return focusPlanId.value;
  }

  function setFocus(planId) {
    const plan = plans.value.find((item) => sameId(item.id, planId) && item.status !== 'archived');
    focusPlanId.value = plan?.id ?? null;
    return focusPlanId.value;
  }

  function clearDetail() {
    detailFence.next();
    resourceFence.next();
    competencyFence.next();
    evidenceFence.next();
    currentPlan.value = null;
    planResources.value = [];
    competencyGraph.value = null;
    evidenceObservations.value = [];
  }

  async function loadCollection(key, path, target, fence) {
    const generation = fence.next();
    loading.value = { ...loading.value, [key]: true };
    try {
      const response = await api.get(path);
      if (fence.current(generation)) {
        if (key === 'active') {
          target.value = response.data.filter((item) => !archivedTombstones.has(String(item.id)));
        } else if (key === 'archived') {
          target.value = [
            ...response.data.filter((item) => !archivedTombstones.has(String(item.id))),
            ...archivedTombstones.values(),
          ];
        } else {
          target.value = response.data;
        }
        const next = { ...errors.value };
        delete next[key];
        errors.value = next;
        reconcileFocus();
      }
      return response.data;
    } catch (error) {
      if (fence.current(generation)) errors.value = { ...errors.value, [key]: errorMessage(error) };
      throw error;
    } finally {
      if (fence.current(generation)) loading.value = { ...loading.value, [key]: false };
    }
  }

  const loadActivePlans = () => loadCollection('active', '/plans', plans, activeFence);
  const loadArchivedPlans = () => loadCollection('archived', '/plans?archived=true', archivedPlans, archiveFence);

  async function loadPlan(planId) {
    const detailGeneration = detailFence.next();
    const resourceGeneration = resourceFence.next();
    const competencyGeneration = competencyFence.next();
    const evidenceGeneration = evidenceFence.next();
    loading.value = { ...loading.value, detail: true, resources: true };
    const detailRequest = api.get(`/plans/${planId}`)
      .then((response) => {
        if (detailFence.current(detailGeneration)) {
          currentPlan.value = response.data;
          const next = { ...errors.value };
          delete next.detail;
          errors.value = next;
        }
        return response.data;
      })
      .catch((error) => {
        if (detailFence.current(detailGeneration)) errors.value = { ...errors.value, detail: errorMessage(error) };
        throw error;
      })
      .finally(() => {
        if (detailFence.current(detailGeneration)) loading.value = { ...loading.value, detail: false };
      });
    api.get(`/plans/${planId}/resources`)
      .then((response) => {
        if (resourceFence.current(resourceGeneration)) {
          planResources.value = response.data;
          const next = { ...errors.value };
          delete next.resources;
          errors.value = next;
        }
      })
      .catch((error) => {
        if (resourceFence.current(resourceGeneration)) errors.value = { ...errors.value, resources: errorMessage(error) };
      })
      .finally(() => {
        if (resourceFence.current(resourceGeneration)) loading.value = { ...loading.value, resources: false };
      });
    api.get(`/plans/${planId}/competencies`)
      .then((response) => {
        if (competencyFence.current(competencyGeneration)) competencyGraph.value = response.data;
      })
      .catch((error) => {
        if (competencyFence.current(competencyGeneration)) errors.value = { ...errors.value, competencies: errorMessage(error) };
      });
    api.get(`/plans/${planId}/evidence-observations`)
      .then((response) => {
        if (evidenceFence.current(evidenceGeneration)) evidenceObservations.value = response.data?.observations || [];
      })
      .catch((error) => {
        if (evidenceFence.current(evidenceGeneration)) errors.value = { ...errors.value, evidence: errorMessage(error) };
      });
    return detailRequest;
  }

  async function setArchived(planId, archived) {
    const response = await api.patch(`/plans/${planId}/archive`, { archived });
    const plan = response.data;
    // Any collection request issued before this committed mutation is stale,
    // even if its response arrives after the archive request.
    activeFence.next();
    archiveFence.next();
    if (archived) {
      archivedTombstones.set(String(planId), plan);
      plans.value = removeById(plans.value, planId);
      archivedPlans.value = replaceById(archivedPlans.value, plan);
    } else {
      archivedTombstones.delete(String(planId));
      archivedPlans.value = removeById(archivedPlans.value, planId);
      plans.value = replaceById(plans.value, plan);
    }
    const detailCleared = archived && sameId(currentPlan.value?.id, planId);
    const focusCleared = archived && sameId(focusPlanId.value, planId);
    if (detailCleared) clearDetail();
    if (focusCleared) focusPlanId.value = null;
    reconcileFocus();
    void Promise.allSettled([loadActivePlans(), loadArchivedPlans()]);
    return { plan, archived, detailCleared, focusCleared };
  }

  return {
    plans,
    archivedPlans,
    currentPlan,
    planResources,
    competencyGraph,
    evidenceObservations,
    focusPlanId,
    loading,
    errors,
    activePlans,
    allPlans,
    focusedPlan,
    reconcileFocus,
    setFocus,
    clearDetail,
    loadActivePlans,
    loadArchivedPlans,
    loadPlan,
    setArchived,
  };
});
