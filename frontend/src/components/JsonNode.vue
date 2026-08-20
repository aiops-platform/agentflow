<template>
  <div class="json-node">
    <template v-if="isObject(data)">
      <div v-for="(v, k) in data" :key="k" class="entry">
        <span class="key">{{ k }}</span>
        <div class="children">
          <JsonNode :data="v" />
        </div>
      </div>
    </template>
    <template v-else-if="isArray(data)">
      <div v-for="(v, i) in data" :key="i" class="entry">
        <span class="idx">[{{ i }}]</span>
        <div class="children">
          <JsonNode :data="v" />
        </div>
      </div>
    </template>
    <span v-else class="val" :class="valClass(data)">{{ display(data) }}</span>
  </div>
</template>

<script setup>
defineOptions({ name: 'JsonNode' })
const props = defineProps({ data: { type: null, default: null } })

function isObject(v) { return v !== null && typeof v === 'object' && !Array.isArray(v) }
function isArray(v) { return Array.isArray(v) }
function display(v) {
  if (v === null) return 'null'
  if (typeof v === 'string') return `"${v}"`
  if (typeof v === 'boolean') return v ? 'true' : 'false'
  return String(v)
}
function valClass(v) {
  if (v === null) return 'null'
  if (typeof v === 'number') return 'num'
  if (typeof v === 'boolean') return 'bool'
  return 'str'
}
</script>

<style scoped>
.json-node { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.entry { margin: 2px 0; }
.key { color: #7c3aed; font-weight: 600; }
.idx { color: #64748b; }
.children { margin-left: 16px; border-left: 1px solid #e2e8f0; padding-left: 8px; }
.val { color: #334155; word-break: break-all; }
.val.null { color: #94a3b8; }
.val.num { color: #0284c7; }
.val.bool { color: #d97706; }
.val.str { color: #16a34a; }
</style>
