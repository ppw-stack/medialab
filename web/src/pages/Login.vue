<script setup lang="ts">
import { reactive, ref } from 'vue';
import { message } from 'ant-design-vue';
import { LockOutlined } from '@ant-design/icons-vue';
import { useAuthStore } from '../stores/auth';

const auth = useAuthStore();
const loading = ref(false);
const form = reactive({ username: '', password: '' });

async function submitLogin() {
  if (!form.username) {
    message.warning('请输入用户名');
    return;
  }
  if (!form.password) {
    message.warning('请输入密码');
    return;
  }
  loading.value = true;
  try {
    await auth.login(form.username, form.password);
    window.location.href = '/';
  } catch (exc) {
    message.error(exc instanceof Error ? exc.message : '登录失败');
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div class="login-canvas">
    <div class="login-card">
      <div class="login-brand">
        <span class="brand__dot" />
        <span class="brand__name">SeedHelper</span>
      </div>
      <div class="login-title">登录</div>
      <div class="login-sub">监控下载器，守卫免费窗口，看护做种比例。</div>

      <a-form :model="form" @finish="submitLogin" class="login-form">
        <a-form-item name="username" :rules="[{ required: true, message: '请输入用户名' }]">
          <a-input
            v-model:value="form.username"
            placeholder="用户名"
            autocomplete="username"
            size="large"
          />
        </a-form-item>
        <a-form-item name="password" :rules="[{ required: true, message: '请输入密码' }]">
          <a-input-password
            v-model:value="form.password"
            placeholder="密码"
            autocomplete="current-password"
            size="large"
            @press-enter="submitLogin"
          >
            <template #prefix><LockOutlined style="color: var(--text-tertiary)" /></template>
          </a-input-password>
        </a-form-item>
        <a-form-item style="margin-bottom: 0">
          <a-button
            type="primary"
            html-type="button"
            :loading="loading"
            block
            size="large"
            @click="submitLogin"
          >
            登录
          </a-button>
        </a-form-item>
      </a-form>

      <div class="login-foot">
        <span class="mono">SESSION · LOCAL</span>
        <span class="mono">v0.5</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.login-canvas {
  position: relative;
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--sp-6);
  /* grid paper inherited from body */
}

.login-card {
  position: relative;
  width: 400px;
  background: var(--bg-surface);
  border: 1px solid var(--border-hairline);
  border-radius: var(--r-lg);
  padding: var(--sp-10) var(--sp-10) var(--sp-6);
  box-shadow: var(--shadow-lg);
}

.login-brand {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  margin-bottom: var(--sp-8);
}

.login-title {
  font-size: var(--text-xl);
  font-weight: 700;
  letter-spacing: -0.02em;
  color: var(--text-primary);
  margin-bottom: var(--sp-2);
}

.login-sub {
  font-size: var(--text-sm);
  color: var(--text-secondary);
  margin-bottom: var(--sp-6);
}

.login-form :deep(.ant-form-item) {
  margin-bottom: var(--sp-4);
}

.login-foot {
  margin-top: var(--sp-6);
  padding-top: var(--sp-4);
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
