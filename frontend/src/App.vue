<template>
  <div class="app">
    <header class="topbar">
      <h1>AIOps Bug Fix 工作流平台</h1>
      <nav>
        <button :class="{ active: tab === 'config' }" @click="tab = 'config'">流程配置</button>
        <button :class="{ active: tab === 'solve' }" @click="tab = 'solve'">Bug 解决</button>
      </nav>
    </header>
    <main>
      <WorkflowConfig v-if="tab === 'config'" @solve="onSolve" />
      <BugSolve v-else :initial-workflow-id="selectedId" />
    </main>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import WorkflowConfig from './components/WorkflowConfig.vue'
import BugSolve from './components/BugSolve.vue'

const tab = ref('config')
const selectedId = ref(null)

function onSolve(id) {
  selectedId.value = id
  tab.value = 'solve'
}
</script>

<style>
* { box-sizing: border-box; }
body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f6f8; }
.app { height: 100vh; display: flex; flex-direction: column; }
.topbar { display: flex; align-items: center; gap: 24px; padding: 12px 24px; background: #1e293b; color: #fff; }
.topbar h1 { font-size: 16px; margin: 0; font-weight: 600; }
.topbar nav { display: flex; gap: 8px; }
.topbar button { padding: 8px 16px; border: 1px solid #475569; background: transparent; color: #cbd5e1; border-radius: 6px; cursor: pointer; }
.topbar button.active { background: #3b82f6; border-color: #3b82f6; color: #fff; }
main { flex: 1; overflow: hidden; }
</style>
