export function errorMessage(error) {
  return error?.response?.data?.detail || error?.message || '请求失败';
}

export function sameId(left, right) {
  return left != null && right != null && String(left) === String(right);
}

export function replaceById(items, entity) {
  const next = items.filter((item) => !sameId(item.id, entity.id));
  next.push(entity);
  return next;
}

export function removeById(items, entityId) {
  return items.filter((item) => !sameId(item.id, entityId));
}

export function createLoadFence() {
  let generation = 0;
  return {
    next() {
      generation += 1;
      return generation;
    },
    current(candidate) {
      return candidate === generation;
    },
  };
}
