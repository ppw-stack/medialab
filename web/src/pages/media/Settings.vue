<template>
  <div class="page">
    <a-card size="small" title="签名密钥" style="margin-bottom: 16px">
      <a-alert
        type="warning"
        show-icon
        message="中转 URL 的签名没有有效期"
        description="换来了 STRM 文件永不过期、无需定期重写。代价是：URL 一旦泄露，长期可用直到轮换密钥。"
        style="margin-bottom: 16px"
      />

      <p class="muted">
        轮换会：批量重签所有条目 + 重写全部 STRM 文件。
        <strong>文件名与目录结构不变</strong>，媒体服务器只需刷新元数据，不需重扫整个库。
      </p>

      <a-space style="margin: 12px 0">
        <a-input
          v-model:value="newSecret"
          placeholder="留空则自动生成 32 字节随机密钥"
          style="width: 380px"
        />
        <a-popconfirm
          title="轮换后旧链接全部失效，确定？"
          ok-text="轮换"
          cancel-text="取消"
          @confirm="onRotate"
        >
          <a-button type="primary" danger :loading="rotating">轮换密钥</a-button>
        </a-popconfirm>
      </a-space>

      <a-alert
        v-if="rotatedSecret"
        type="success"
        show-icon
        style="margin-top: 12px"
      >
        <template #message>新密钥（只显示这一次，请立刻保存）</template>
        <template #description>
          <div class="mono secret">{{ rotatedSecret }}</div>
          <div class="muted" style="margin-top: 8px">
            把它写进环境变量 <code>SIGNING_SECRET</code> 后重启服务。
            运行时密钥已即时切换，当前链接立即可用。
          </div>
          <a-button size="small" style="margin-top: 8px" @click="copySecret">复制</a-button>
        </template>
      </a-alert>
    </a-card>

    <a-card size="small" title="关于本服务">
      <a-descriptions bordered :column="1" size="small">
        <a-descriptions-item label="核心能力">
          媒体字节流不经过本服务 —— 播放器跟随 302 直接连网盘 CDN
        </a-descriptions-item>
        <a-descriptions-item label="本服务职责">
          换取直链（几 KB）+ 维护本地直链缓存 + 管理 STRM 库
        </a-descriptions-item>
        <a-descriptions-item label="缓存分桶">
          按 (media_id, UA) 分桶 —— 115 直链 f=1 绑定 UA，混用会 403
        </a-descriptions-item>
        <a-descriptions-item label="直连播放器">
          Infuse / Kodi 直连模式 / VidHub / SPlayer / VLC 直开 strm
        </a-descriptions-item>
        <a-descriptions-item label="服务端代理场景">
          Emby / Jellyfin 的 strm 由服务端拉流，流量仍走它们自己的服务器
        </a-descriptions-item>
      </a-descriptions>
    </a-card>
  </div>
</template>

<script setup lang="ts">
import { ref } from "vue"
import { message } from "ant-design-vue"
import { api, errMsg } from "../../api"

const newSecret = ref("")
const rotatedSecret = ref("")
const rotating = ref(false)

async function onRotate(): Promise<void> {
  rotating.value = true
  rotatedSecret.value = ""
  try {
    const res = await api.post<{ resigned: number; rewritten: number; failed: number; new_secret: string }>(
      "/media/rotate-secret",
      newSecret.value ? { new_secret: newSecret.value } : {},
    )
    rotatedSecret.value = res.new_secret
    newSecret.value = ""
    message.success(`已重签 ${res.resigned} 条，重写 ${res.rewritten} 个文件`)
  } catch (e) {
    message.error(errMsg(e, "轮换失败"))
  } finally {
    rotating.value = false
  }
}

async function copySecret(): Promise<void> {
  try {
    await navigator.clipboard.writeText(rotatedSecret.value)
    message.success("已复制")
  } catch {
    message.error("复制失败")
  }
}
</script>

<style scoped>
.secret {
  word-break: break-all;
  background: #f5f5f5;
  padding: 8px;
  border-radius: 4px;
}
</style>
