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

  /**
   * 登出。
   *
   * ⚠️ **服务端登出失败也必须清本地登录态**——否则请求一失败，
   *    未处理的拒绝会向上抛，界面停在「已登录」状态。
   *
   * @returns `true` = 服务端登出成功；`false` = 请求失败（本地**已**清理），
   *          由调用方决定要不要提示用户。
   */
  async function logout(): Promise<boolean> {
    let ok = true;
    try {
      await fetch('/api/v1/auth/logout', { method: 'POST' });
    } catch {
      // 网络中断 / 服务端异常——记下失败，继续走本地清理
      ok = false;
    }
    authenticated.value = false;
    username.value = null;
    return ok;
  }

  return { authenticated, username, loading, login, logout, refresh };
});
