async function request(method, path, body) {
  const response = await fetch(`/api/v1${path}`, {
    method,
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const error = new Error(data?.detail || `Request failed with ${response.status}`);
    error.response = { data, status: response.status };
    throw error;
  }
  return { data };
}

const api = {
  get: (path) => request('GET', path),
  post: (path, body) => request('POST', path, body),
  patch: (path, body) => request('PATCH', path, body),
  delete: (path) => request('DELETE', path),
  upload: async (path, file) => {
    const body = new FormData();
    body.append('file', file);
    const response = await fetch(`/api/v1${path}`, { method: 'POST', body });
    const data = await response.json().catch(() => null);
    if (!response.ok) throw new Error(data?.detail || `Upload failed with ${response.status}`);
    return { data };
  },
};

export default api;
