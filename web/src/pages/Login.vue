<script setup lang="ts">
import { reactive, ref } from 'vue'
import { message } from 'ant-design-vue'
import { LockOutlined } from '@ant-design/icons-vue'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const loading = ref(false)
const form = reactive({ username: '', password: '' })

async function submitLogin() {
  if (!form.username) {
    message.warning('请输入用户名')
    return
  }
  if (!form.password) {
    message.warning('请输入密码')
    return
  }
  loading.value = true
  try {
    await auth.login(form.username, form.password)
    window.location.href = '/'
  } catch (exc) {
    message.error(exc instanceof Error ? exc.message : '登录失败')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-canvas">
    <div class="login-grid" aria-hidden="true">
      <div v-for="i in 64" :key="i" class="login-grid__cell" />
    </div>
    <div class="login-card">
      <div class="login-brand">
        <span class="login-brand__dot" />
        <span class="login-brand__name">SeedHelper</span>
        <span class="login-brand__sub">PT&nbsp;·&nbsp;OPERATIONS&nbsp;CONSOLE</span>
      </div>
      <div class="login-eyebrow">登录</div>
      <div class="login-title">进入工作台</div>
      <div class="login-sub">监控下载器，守卫免费窗口，看护做种比例。</div>

      <a-form :model="form" @finish="submitLogin" class="login-form">
        <a-form-item
          name="username"
          :rules="[{ required: true, message: '请输入用户名' }]"
        >
          <label class="label-caps">用户名</label>
          <a-input
            v-model:value="form.username"
            placeholder="admin"
            autocomplete="username"
            size="large"
          />
        </a-form-item>
        <a-form-item
          name="password"
          :rules="[{ required: true, message: '请输入密码' }]"
        >
          <label class="label-caps">密码</label>
          <a-input-password
            v-model:value="form.password"
            placeholder="••••••••"
            autocomplete="current-password"
            @press-enter="submitLogin"
          >
            <template #prefix><LockOutlined style="color: var(--text-tertiary)" /></template>
          </a-input-password>
        </a-form-item>
        <a-form-item>
          <a-button
            type="primary"
            html-type="button"
            :loading="loading"
            block
            size="large"
            @click="submitLogin"
          >
            登 录
          </a-button>
        </a-form-item>
      </a-form>

      <div class="login-foot">
        <span class="mono">SESSION&nbsp;·&nbsp;LOCAL</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.login-canvas {
  position: relative;
  min-height: 100vh;
  background: var(--bg-canvas);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--sp-6);
  overflow: hidden;
}

.login-grid {
  position: absolute;
  inset: 0;
  display: grid;
  grid-template-columns: repeat(8, 1fr);
  grid-template-rows: repeat(8, 1fr);
  opacity: 0.25;
  pointer-events: none;
}

.login-grid__cell {
  border-right: 1px solid var(--border-hairline);
  border-bottom: 1px solid var(--border-hairline);
}

.login-grid__cell:nth-child(8n) {
  border-right: 0;
}

.login-card {
  position: relative;
  width: 360px;
  background: var(--bg-surface);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  padding: var(--sp-6);
}

.login-brand {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: var(--sp-5);
}

.login-brand__dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--accent-amber);
  box-shadow: 0 0 12px var(--accent-amber-strong);
}

.login-brand__name {
  font-family: var(--font-sans);
  font-weight: 500;
  font-size: var(--text-md);
  letter-spacing: -0.01em;
}

.login-brand__sub {
  margin-left: auto;
  font-family: var(--font-mono);
  font-size: var(--text-2xs);
  letter-spacing: 0.15em;
  color: var(--text-tertiary);
}

.login-eyebrow {
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  letter-spacing: 0.18em;
  color: var(--accent-amber);
  margin-bottom: var(--sp-2);
  text-transform: uppercase;
}

.login-title {
  font-size: var(--text-2xl);
  font-weight: 500;
  letter-spacing: -0.02em;
  margin-bottom: var(--sp-2);
}

.login-sub {
  font-size: var(--text-sm);
  color: var(--text-secondary);
  margin-bottom: var(--sp-5);
}

.login-form :deep(.ant-form-item) {
  margin-bottom: var(--sp-4);
}

.login-form :deep(.ant-form-item-label) {
  padding-bottom: var(--sp-1);
}

.login-form :deep(.ant-input),
.login-form :deep(.ant-input-affix-wrapper) {
  background: var(--bg-inset) !important;
  font-family: var(--font-mono);
  font-size: var(--text-sm);
}

.login-foot {
  margin-top: var(--sp-4);
  padding-top: var(--sp-3);
  border-top: 1px solid var(--border-hairline);
  display: flex;
  justify-content: space-between;
  font-size: var(--text-2xs);
  letter-spacing: 0.1em;
  color: var(--text-tertiary);
}

.login-foot .mono {
  font-family: var(--font-mono);
}
</style>
