<template>
  <div class="solve">
    <aside class="panel">
      <h2>Bug 解决</h2>
      <label>选择流程</label>
      <select v-model="workflowId">
        <option value="" disabled>— 选择已保存流程 —</option>
        <option v-for="w in workflows" :key="w.id" :value="w.id">{{ w.name }}</option>
      </select>

      <label>Ticket JSON（粘贴）</label>
      <textarea v-model="ticketJson" placeholder='{ "repo": "...", "bug_report": { "title": "..." } }' spellcheck="false"></textarea>

      <button class="primary" @click="start" :disabled="!workflowId || running">开始解决问题</button>
      <button v-if="running" class="stop" @click="stop">停止</button>
      <p v-if="error" class="err">{{ error }}</p>

      <template v-if="runId">
        <h3>运行状态</h3>
        <div class="stat">
          <div>run_id: <b>{{ runId }}</b></div>
          <div>状态: <b>{{ runStatus }}</b></div>
          <div>总 token: <b>{{ totalTokens.toLocaleString() }}</b></div>
          <div>总 cost: <b>${{ totalCost.toFixed(4) }}</b></div>
        </div>
      </template>
    </aside>

    <div class="canvas">
      <VueFlow :nodes="nodes" :edges="edges" :default-viewport="{ zoom: 0.8 }" fit-view-on-init @node-click="onNodeClick">
        <Background />
        <Controls />
      </VueFlow>

      <div v-if="selected" class="detail">
        <h3>{{ selected.id }} <span class="status" :class="selected.status">{{ selected.status }}</span></h3>
        <div class="meta">tokens: {{ selected.tokens }} · cost: {{ selected.cost }}</div>
        <details open>
          <summary>输入 prompt</summary>
          <pre>{{ selected.prompt || '(无)' }}</pre>
        </details>
        <details>
          <summary>输出 output</summary>
          <pre>{{ pretty(selected.output) }}</pre>
        </details>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, watch } from 'vue'
import { VueFlow } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import { api } from '../api'

const props = defineProps({ initialWorkflowId: { type: String, default: null } })

const workflows = ref([])
const workflowId = ref(props.initialWorkflowId || '')
const ticketJson = ref('')
const runId = ref(null)
const runStatus = ref('')
const totalTokens = ref(0)
const totalCost = ref(0)
const error = ref('')
const running = ref(false)
const nodes = ref([])
const edges = ref([])
const selected = ref(null)
const runData = ref(null)
let timer = null

const STATUS_COLOR = { done: '#22c55e', running: '#3b82f6', failed: '#ef4444', pending: '#94a3b8', cancelled: '#f59e0b', skipped: '#94a3b8' }

function pretty(obj) { return JSON.stringify(obj, null, 2) }

function buildGraph(graph, nodeStatus) {
  const ns = (graph?.nodes || []).map(n => ({
    id: n.id,
    position: { x: 0, y: 0 },
    data: {
      label: `${n.id} (${n.agent})`,
      status: nodeStatus[n.id] || 'pending',
    },
    style: { background: '#fff', border: `2px solid ${STATUS_COLOR[nodeStatus[n.id]] || '#94a3b8'}`, borderRadius: '6px', padding: '8px' },
  }))
  const es = (graph?.edges || []).map((e, i) => ({ id: `e${i}`, source: e.from, target: e.to }))
  return { nodes: ns, edges: es }
}

function applyLayout(nodesArr, graph) {
  const ids = nodesArr.map(n => n.id)
  const rawEdges = (graph?.edges || []).map(e => [e.from, e.to])
  const indeg = {}
  ids.forEach(id => (indeg[id] = 0))
  const adj = {}
  rawEdges.forEach(([u, v]) => { if (ids.includes(u) && ids.includes(v)) { (adj[u] = adj[u] || []).push(v); indeg[v]++ } })
  const level = {}
  const q = ids.filter(id => indeg[id] === 0)
  q.forEach(id => (level[id] = 0))
  let qi = 0
  while (qi < q.length) { const u = q[qi++]; (adj[u] || []).forEach(v => { level[v] = Math.max(level[v] || 0, (level[u] || 0) + 1); if (--indeg[v] === 0) q.push(v) }) }
  const byLevel = {}
  ids.forEach(id => { const l = level[id] || 0; (byLevel[l] = byLevel[l] || []).push(id) })
  Object.keys(byLevel).forEach(l => { byLevel[l].forEach((id, i) => { const n = nodesArr.find(x => x.id === id); if (n) n.position = { x: i * 220, y: Number(l) * 140 } }) })
  return nodesArr
}

async function loadWorkflows() {
  workflows.value = await api.listWorkflows()
  if (!workflowId.value && props.initialWorkflowId) workflowId.value = props.initialWorkflowId
}

async function start() {
  error.value = ''
  let ticket = {}
  if (ticketJson.value.trim()) {
    try { ticket = JSON.parse(ticketJson.value) } catch (e) { error.value = `ticket JSON 解析失败: ${e.message}`; return }
  }
  try {
    const res = await api.startRun({ workflow_id: workflowId.value, ticket })
    runId.value = res.run_id
    running.value = true
    runStatus.value = 'started'
    await poll()
  } catch (e) { error.value = e.message }
}

async function stop() {
  try {
    await api.stopRun(runId.value)
    running.value = false
    clearInterval(timer)
    runStatus.value = 'cancelled'
  } catch (e) { error.value = e.message }
}

async function poll() {
  clearInterval(timer)
  timer = setInterval(async () => {
    try {
      const run = await api.getRun(runId.value)
      runData.value = run
      runStatus.value = run.status
      totalTokens.value = run.total_tokens
      totalCost.value = run.total_cost
      const status = {}
      Object.entries(run.nodes || {}).forEach(([id, s]) => { status[id] = s.status })
      const graph = await api.getWorkflow(workflowId.value)
      const { nodes: ns, edges: es } = buildGraph(graph.graph, status)
      nodes.value = applyLayout(ns, graph.graph)
      edges.value = es
      if (run.status === 'success' || run.status === 'failed') { running.value = false; clearInterval(timer) }
    } catch (e) { /* 轮询中忽略 */ }
  }, 2000)
}

function onNodeClick({ node }) {
  const s = (runData.value?.nodes || {})[node.id]
  if (s) selected.value = { id: node.id, ...s }
}

watch(workflowId, async (id) => {
  if (id) {
    try {
      const w = await api.getWorkflow(id)
      const { nodes: ns, edges: es } = buildGraph(w.graph, {})
      nodes.value = applyLayout(ns, w.graph)
      edges.value = es
    } catch (e) { /* ignore */ }
  }
})

onMounted(loadWorkflows)
onUnmounted(() => clearInterval(timer))
</script>

<style scoped>
.solve { display: flex; height: 100%; }
.panel { width: 360px; padding: 16px; background: #fff; border-right: 1px solid #e2e8f0; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; }
.panel h2 { margin: 0 0 8px; font-size: 16px; }
.panel h3 { margin: 16px 0 8px; font-size: 14px; color: #475569; }
.panel label { font-size: 13px; color: #475569; }
.panel select, .panel textarea { padding: 8px; border: 1px solid #cbd5e1; border-radius: 6px; font-family: ui-monospace, monospace; font-size: 12px; }
.panel textarea { height: 180px; resize: vertical; }
.panel button.primary { padding: 10px; background: #3b82f6; color: #fff; border: none; border-radius: 6px; cursor: pointer; }
.panel button.primary:disabled { opacity: 0.5; cursor: not-allowed; }
.panel button.stop { padding: 10px; background: #ef4444; color: #fff; border: none; border-radius: 6px; cursor: pointer; }
.err { color: #dc2626; font-size: 12px; margin: 0; white-space: pre-wrap; }
.stat { font-size: 13px; display: flex; flex-direction: column; gap: 4px; }
.canvas { flex: 1; position: relative; background: #f8fafc; }
.detail { position: absolute; right: 16px; top: 16px; width: 420px; max-height: calc(100% - 32px); overflow-y: auto; background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); }
.detail h3 { margin: 0 0 8px; font-size: 14px; }
.status { font-size: 12px; padding: 2px 8px; border-radius: 10px; color: #fff; }
.status.done { background: #22c55e; }
.status.running { background: #3b82f6; }
.status.failed { background: #ef4444; }
.status.pending { background: #94a3b8; }
.status.cancelled { background: #f59e0b; }
.meta { font-size: 12px; color: #64748b; margin-bottom: 8px; }
.detail summary { cursor: pointer; font-size: 13px; font-weight: 600; margin: 8px 0; }
.detail pre { font-size: 11px; background: #f8fafc; padding: 8px; border-radius: 4px; overflow-x: auto; white-space: pre-wrap; max-height: 200px; overflow-y: auto; }
</style>
