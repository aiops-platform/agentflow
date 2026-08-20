<template>
  <div class="config">
    <aside class="panel">
      <h2>流程配置</h2>
      <label>流程名称</label>
      <input v-model="name" placeholder="例如：order-service-quotation-print-fail" />

      <label>YAML（粘贴流程定义）</label>
      <textarea v-model="yaml" placeholder="name: xxx&#10;nodes:&#10;  triage: { agent: triage, ... }&#10;edges:&#10;  - { from: triage, to: logs }" spellcheck="false"></textarea>

      <div class="actions">
        <button @click="preview">预览节点图</button>
        <button class="primary" @click="save" :disabled="!yaml.trim()">保存流程</button>
      </div>
      <p v-if="error" class="err">{{ error }}</p>

      <h3>已保存流程（选择复用）</h3>
      <ul class="wf-list">
        <li v-for="w in workflows" :key="w.id">
          <span class="wf-name">{{ w.name }}</span>
          <button @click="$emit('solve', w.id)">去解决</button>
          <button class="danger" @click="remove(w.id)">删</button>
        </li>
        <li v-if="!workflows.length" class="empty">暂无已保存流程</li>
      </ul>
    </aside>

    <div class="canvas">
      <VueFlow :nodes="nodes" :edges="edges" :default-viewport="{ zoom: 0.8 }" fit-view-on-init>
        <Background />
        <Controls />
      </VueFlow>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { VueFlow } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import { load as parseYaml } from 'js-yaml'
import { api } from '../api'

const emit = defineEmits(['solve'])

const name = ref('')
const yaml = ref('')
const error = ref('')
const workflows = ref([])
const nodes = ref([])
const edges = ref([])

function layout(wf) {
  const ids = Object.keys(wf.nodes || {})
  const rawEdges = (wf.edges || []).map(e => [e.from, e.to])
  const indeg = {}
  ids.forEach(id => (indeg[id] = 0))
  const adj = {}
  rawEdges.forEach(([u, v]) => {
    if (ids.includes(u) && ids.includes(v)) {
      ;(adj[u] = adj[u] || []).push(v)
      indeg[v]++
    }
  })
  const level = {}
  const queue = ids.filter(id => indeg[id] === 0)
  queue.forEach(id => (level[id] = 0))
  let qi = 0
  while (qi < queue.length) {
    const u = queue[qi++]
    ;(adj[u] || []).forEach(v => {
      level[v] = Math.max(level[v] || 0, (level[u] || 0) + 1)
      if (--indeg[v] === 0) queue.push(v)
    })
  }
  const byLevel = {}
  ids.forEach(id => {
    const l = level[id] || 0
    ;(byLevel[l] = byLevel[l] || []).push(id)
  })
  const ns = []
  Object.keys(byLevel).forEach(l => {
    byLevel[l].forEach((id, i) => {
      ns.push({
        id,
        position: { x: i * 220, y: Number(l) * 140 },
        data: { label: `${id}\n(${wf.nodes[id]?.agent || ''})` },
      })
    })
  })
  const es = rawEdges.map(([u, v], i) => ({ id: `e${i}`, source: u, target: v }))
  return { nodes: ns, edges: es }
}

function preview() {
  try {
    const wf = parseYaml(yaml.value)
    const { nodes: ns, edges: es } = layout(wf)
    nodes.value = ns
    edges.value = es
    error.value = ''
  } catch (e) {
    error.value = `解析失败: ${e.message}`
  }
}

async function save() {
  try {
    const res = await api.saveWorkflow(name.value || '未命名', yaml.value)
    error.value = ''
    await loadList()
    alert(`已保存流程 #${res.id}`)
  } catch (e) {
    error.value = e.message
  }
}

async function remove(id) {
  await api.deleteWorkflow(id)
  await loadList()
}

async function loadList() {
  workflows.value = await api.listWorkflows()
}

onMounted(loadList)
</script>

<style scoped>
.config { display: flex; height: 100%; }
.panel { width: 380px; padding: 16px; background: #fff; border-right: 1px solid #e2e8f0; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; }
.panel h2 { margin: 0 0 8px; font-size: 16px; }
.panel h3 { margin: 16px 0 8px; font-size: 14px; color: #475569; }
.panel label { font-size: 13px; color: #475569; }
.panel input, .panel textarea { padding: 8px; border: 1px solid #cbd5e1; border-radius: 6px; font-family: ui-monospace, monospace; font-size: 12px; }
.panel textarea { height: 240px; resize: vertical; }
.actions { display: flex; gap: 8px; }
.actions button { flex: 1; padding: 8px; border: 1px solid #cbd5e1; background: #f8fafc; border-radius: 6px; cursor: pointer; }
.actions button.primary { background: #3b82f6; color: #fff; border-color: #3b82f6; }
.err { color: #dc2626; font-size: 12px; margin: 0; white-space: pre-wrap; }
.wf-list { list-style: none; padding: 0; margin: 0; }
.wf-list li { display: flex; align-items: center; gap: 8px; padding: 8px; border-bottom: 1px solid #f1f5f9; }
.wf-name { flex: 1; font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.wf-list button { padding: 4px 10px; border: 1px solid #cbd5e1; background: #f8fafc; border-radius: 4px; cursor: pointer; font-size: 12px; }
.wf-list button.danger { color: #dc2626; }
.empty { color: #94a3b8; font-size: 13px; }
.canvas { flex: 1; background: #f8fafc; }
</style>
