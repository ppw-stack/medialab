<script setup lang="ts">
import { reactive, ref } from 'vue'
import { message } from 'ant-design-vue'
import { UserOutlined, LockOutlined } from '@ant-design/icons-vue'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const loading = ref(false)
const form = reactive({ username: '', password: '' })

async function submitLogin() {
  if (!form.username || !form.password) return
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
  <div
    style="display: flex; justify-content: center; align-items: center; min-height: 100vh"
  >
    <a-card style="width: 400px">
      <a-typography-title :level="3" style="text-align: center; margin-bottom: 24px">
        SeedHelper
      </a-typography-title>
      <a-form
        :model="form"
        size="large"
        @finish="submitLogin"
      >
        <a-form-item
          name="username"
          :rules="[{ required: true, message: '请输入用户名' }]"
        >
          <a-input
            v-model:value="form.username"
            placeholder="用户名"
          >
            <template #prefix><UserOutlined /></template>
          </a-input>
        </a-form-item>
        <a-form-item
          name="password"
          :rules="[{ required: true, message: '请输入密码' }]"
        >
          <a-input-password
            v-model:value="form.password"
            placeholder="密码"
            @press-enter="submitLogin"
          >
            <template #prefix><LockOutlined /></template>
          </a-input-password>
        </a-form-item>
        <a-form-item>
          <a-button type="primary" html-type="button" :loading="loading" block @click="submitLogin">
            登录
          </a-button>
        </a-form-item>
      </a-form>
    </a-card>
  </div>
</template>
