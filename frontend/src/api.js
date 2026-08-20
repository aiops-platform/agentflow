// 后端 API 封装（dev 经 vite proxy 转发到 localhost:8000）
const BASE = ''

async function req(path, options = {}) {
  const resp = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({ detail: resp.statusText }))
    throw new Error(detail.detail || resp.statusText)
  }
  return resp.json()
}

export const api = {
  // workflow CRUD
  saveWorkflow: (name, yaml) => req('/workflows', { method: 'POST', body: JSON.stringify({ name, yaml }) }),
  listWorkflows: () => req('/workflows'),
  getWorkflow: (id) => req(`/workflows/${id}`),
  deleteWorkflow: (id) => req(`/workflows/${id}`, { method: 'DELETE' }),
  // run
  startRun: (payload) => req('/run', { method: 'POST', body: JSON.stringify(payload) }),
  getRun: (runId) => req(`/runs/${runId}`),
  stopRun: (runId) => req(`/runs/${runId}/stop`, { method: 'POST' }),
}
