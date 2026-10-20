const BASE = "/api/v1";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

/**
 * 从 catch 的 unknown 值中提取错误消息——比 `(e as Error).message` 安全：
 * 当 e 是非 Error 值时不会抛 TypeError。
 *
 * 依次尝试：
 * 1. `Error` 实例 → `e.message`（空串则退回 fallback）
 * 2. 字符串 → 原样
 * 3. 带 `message` 字符串字段的普通对象（fetch 拒绝值常见形态）→ 该字段
 * 4. 否则 → fallback
 */
export function errMsg(e: unknown, fallback = "未知错误"): string {
  if (e instanceof Error) return e.message || fallback;
  if (typeof e === "string") return e || fallback;
  if (e && typeof e === "object" && "message" in e) {
    const m = (e as { message: unknown }).message;
    if (typeof m === "string" && m) return m;
  }
  return fallback;
}

export async function apiFetch<T = unknown>(
  path: string,
  opts: RequestInit = {}
): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    ...opts,
    headers: {
      "Content-Type": "application/json",
      ...opts.headers,
    },
  });

  if (resp.status === 401) {
    // 会话失效：清登录态并回登录页。不整页 reload（保留 SPA 路由栈，
    // 避免丢失滚动状态）；动态 import 避免与 router/pages 形成静态循环依赖。
    const [{ useAuthStore }, { default: router }] = await Promise.all([
      import("./stores/auth"),
      import("./router"),
    ]);
    const auth = useAuthStore();
    auth.authenticated = false;
    auth.username = null;
    if (router.currentRoute.value.path !== "/login") {
      await router.push("/login");
    }
    throw new ApiError(401, "未登录");
  }

  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const data = await resp.json();
      detail = data.detail || detail;
    } catch {}
    throw new ApiError(resp.status, detail);
  }

  if (resp.status === 204) return undefined as T;
  return resp.json();
}

/**
 * 以 GET / POST 为主（部分代理/防火墙会丢 PUT/DELETE）。
 * 删除走专用 POST 端点，id 放 body：
 *   POST /{resource}/update   body={id, ...fields}
 *   POST /{resource}/delete   body={id}
 * PUT 仅用于运行时参数更新（调度作业配置弹框）。
 */
export const api = {
  get: <T = unknown>(path: string) => apiFetch<T>(path),
  post: <T = unknown>(path: string, body?: unknown) =>
    apiFetch<T>(path, {
      method: "POST",
      body: body ? JSON.stringify(body) : undefined,
    }),
  put: <T = unknown>(path: string, body?: unknown) =>
    apiFetch<T>(path, {
      method: "PUT",
      body: body ? JSON.stringify(body) : undefined,
    }),
};
