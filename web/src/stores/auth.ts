import { defineStore } from 'pinia';
import { ref } from 'vue';

export const useAuthStore = defineStore('auth', () => {
  const authenticated = ref(false);
  const username = ref<string | null>(null);
  const loading = ref(true);

  async function refresh() {
    try {
      const resp = await fetch('/api/v1/auth/status');
      const data = await resp.json();
      authenticated.value = data.authenticated;
      username.value = data.username;
    } catch {
      authenticated.value = false;
      username.value = null;
    } finally {
      loading.value = false;
    }
  }

  async function login(user: string, password: string) {
    const resp = await fetch('/api/v1/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: user, password }),
    });
    if (!resp.ok) {
      const data = (await resp.json().catch(() => ({}))) as { detail?: string };
      throw new Error((data.detail ?? '') || '登录失败');
    }
    await refresh();
  }

  async function logout() {
    await fetch('/api/v1/auth/logout', { method: 'POST' });
    authenticated.value = false;
    username.value = null;
  }

  return { authenticated, username, loading, login, logout, refresh };
});
