<template>
  <a-card size="small" :title="title">
    <template #extra>
      <a-tag color="blue">{{ currentName }}</a-tag>
    </template>

    <a-breadcrumb class="crumb">
      <a-breadcrumb-item v-for="(c, i) in stack" :key="`${i}-${c.id}`">
        <a @click="$emit('go', i)">{{ crumbName(c) }}</a>
      </a-breadcrumb-item>
    </a-breadcrumb>

    <a-spin :spinning="loading">
      <div class="dir-list">
        <div
          v-for="d in dirs"
          :key="d.id"
          class="dir-item"
          @click="$emit('open', d)"
        >
          <FolderOutlined />
          <span class="dir-name">{{ d.name }}</span>
        </div>
        <span v-if="!dirs.length && !loading" class="muted">（没有可进入的子目录）</span>
      </div>
    </a-spin>
  </a-card>
</template>

<script setup lang="ts">
import { computed } from "vue"
import { FolderOutlined } from "@ant-design/icons-vue"
import type { DirEntry } from "../types"

const props = defineProps<{
  title: string
  stack: DirEntry[]
  dirs: DirEntry[]
  loading?: boolean
}>()

defineEmits<{
  (e: "open", dir: DirEntry): void
  (e: "go", index: number): void
}>()

const currentName = computed(() => props.stack[props.stack.length - 1]?.name ?? "—")

/**
 * 面包屑上显示的**目录名**（不是完整路径）。
 *
 * 本地目录浏览器的 ``id`` 是**完整路径**（如 ``/mnt/remotes/.../home``），
 * 直接显示会占满整行。取最后一段即可。
 */
function crumbName(entry: DirEntry): string {
  const name = entry.name
  if (name) return name
  const parts = entry.id.split("/").filter(Boolean)
  return parts.length ? parts[parts.length - 1] : "/"
}
</script>

<style scoped>
.crumb {
  margin-bottom: 8px;
  font-size: 12px;
}

.dir-list {
  max-height: 240px;
  overflow-y: auto;
  border: 1px solid #f0f0f0;
  border-radius: 4px;
  padding: 4px;
}

.dir-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 8px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 13px;
}

.dir-item:hover {
  background: #f5f8ff;
}

.dir-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.muted {
  color: #999;
  font-size: 12px;
}
</style>
