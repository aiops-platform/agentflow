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

    <div class="right">
      <div class="flow">
        <VueFlow :nodes="nodes" :edges="edges" :default-viewport="{ zoom: 0.7 }" fit-view-on-init @node-click="onNodeClick">
          <Background />
          <Controls />
        </VueFlow>
      </div>

      <div class="output">
        <div v-if="selected" class="node-detail">
          <div class="head">
            <h3>{{ selected.id }}</h3>
            <span class="badge" :class="selected.status">{{ selected.status }}</span>
            <span class="meta">tokens {{ (selected.tokens || 0).toLocaleString() }} · cost ${{ (selected.cost || 0).toFixed(4) }}</span>
          </div>
          <div class="tabs">
            <button :class="{ active: tab === 'output' }" @click="tab = 'output'">输出 output</button>
            <button :class="{ active: tab === 'prompt' }" @click="tab = 'prompt'">输入 prompt</button>
          </div>
          <div class="content">
            <JsonNode v-if="tab === 'output' && selected.output" :data="selected.output" />
            <pre v-else-if="tab === 'prompt'">{{ selected.prompt || '(无)' }}</pre>
            <p v-else class="empty">(无内容)</p>
          </div>
        </div>
        <div v-else class="placeholder">点击右上流程图节点，查看该节点的输入 prompt 与格式化输出</div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, watch } from 'vue'
import { VueFlow } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import JsonNode from './JsonNode.vue'
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
const tab = ref('output')
let timer = null

function buildGraph(graph, nodeStatus) {
  const ns = (graph?.nodes || []).map(n => ({
    id: n.id,
    position: { x: 0, y: 0 },
    data: { label: n.id },
    class: `status-${nodeStatus[n.id] || 'pending'}`,
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
  Object.keys(byLevel).forEach(l => { byLevel[l].forEach((id, i) => { const n = nodesArr.find(x => x.id === id); if (n) n.position = { x: i * 180, y: Number(l) * 100 } }) })
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
      if (run.status === 'success' || run.status === 'failed' || run.status === 'cancelled') { running.value = false; clearInterval(timer) }
    } catch (e) { /* 轮询中忽略 */ }
  }, 2000)
}

function onNodeClick({ node }) {
  const s = (runData.value?.nodes || {})[node.id]
  if (s) { selected.value = { id: node.id, ...s }; tab.value = 'output' }
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
.panel { width: 340px; padding: 16px; background: #fff; border-right: 1px solid #e2e8f0; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; flex-shrink: 0; }
.panel h2 { margin: 0 0 8px; font-size: 16px; }
.panel h3 { margin: 16px 0 8px; font-size: 14px; color: #475569; }
.panel label { font-size: 13px; color: #475569; }
.panel select, .panel textarea { padding: 8px; border: 1px solid #cbd5e1; border-radius: 6px; font-family: ui-monospace, monospace; font-size: 12px; }
.panel textarea { height: 160px; resize: vertical; }
.panel button.primary { padding: 10px; background: #3b82f6; color: #fff; border: none; border-radius: 6px; cursor: pointer; }
.panel button.primary:disabled { opacity: 0.5; cursor: not-allowed; }
.panel button.stop { padding: 10px; background: #ef4444; color: #fff; border: none; border-radius: 6px; cursor: pointer; }
.err { color: #dc2626; font-size: 12px; margin: 0; white-space: pre-wrap; }
.stat { font-size: 13px; display: flex; flex-direction: column; gap: 4px; }
.right { flex: 1; display: flex; flex-direction: column; }
.flow { height: 40%; border-bottom: 1px solid #e2e8f0; background: #f8fafc; }
.output { flex: 1; overflow-y: auto; background: #fff; padding: 16px; }
.node-detail .head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.node-detail h3 { margin: 0; font-size: 15px; }
.badge { font-size: 12px; padding: 2px 8px; border-radius: 10px; color: #fff; text-transform: uppercase; }
.badge.done { background: #22c55e; }
.badge.running { background: #3b82f6; }
.badge.failed { background: #ef4444; }
.badge.pending { background: #94a3b8; }
.badge.cancelled { background: #f59e0b; }
.meta { font-size: 12px; color: #64748b; }
.tabs { display: flex; gap: 8px; margin: 12px 0; border-bottom: 1px solid #e2e8f0; }
.tabs button { padding: 6px 14px; border: none; background: transparent; cursor: pointer; font-size: 13px; color: #64748b; border-bottom: 2px solid transparent; }
.tabs button.active { color: #3b82f6; border-bottom-color: #3b82f6; font-weight: 600; }
.content { max-height: 60vh; overflow-y: auto; }
.content pre { font-size: 11px; background: #f8fafc; padding: 10px; border-radius: 6px; overflow-x: auto; white-space: pre-wrap; }
.empty { color: #94a3b8; font-size: 13px; }
.placeholder { color: #94a3b8; font-size: 14px; display: flex; height: 100%; align-items: center; justify-content: center; }
</style>

<style>
/* 节点状态样式（非 scoped：Vue Flow 节点渲染在独立 DOM） */
.vue-flow__node.status-done { border: 2px solid #22c55e; background: #f0fdf4; border-radius: 6px; }
.vue-flow__node.status-running { border: 2px solid #3b82f6; background: #eff6ff; border-radius: 6px; animation: node-pulse 1.2s ease-in-out infinite; }
.vue-flow__node.status-pending { border: 2px solid #cbd5e1; background: #f8fafc; border-radius: 6px; opacity: 0.75; }
.vue-flow__node.status-failed { border: 2px solid #ef4444; background: #fef2f2; border-radius: 6px; }
.vue-flow__node.status-cancelled { border: 2px solid #f59e0b; background: #fffbeb; border-radius: 6px; }
.vue-flow__node.status-skipped { border: 2px solid #94a3b8; background: #f8fafc; border-radius: 6px; }
@keyframes node-pulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(59, 130, 246, 0.45); }
  50% { box-shadow: 0 0 0 8px rgba(59, 130, 246, 0); }
}
</style>
