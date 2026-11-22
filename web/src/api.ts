/**
 * API 客户端。
 *
 * **两个业务域，两个前缀**（后端路由天然不冲突）：
 *
 * - `api`   —— 辅种域，`/api/v1/*`
 * - `mediaApi` —— 媒体库域，`/api/*`
 *
 * 两边共用同一套 fetch 包装、同一套错误处理、**同一个 session cookie**
 * ——所以一次登录即可访问两个域的面板。
 */

/** 辅种域的 API 前缀。 */
const BASE = '/api/v1';

/** 媒体库域的 API 前缀。 */
const BASE_MEDIA = '/api';

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
export function errMsg(e: unknown, fallback = '未知错误'): string {
  if (e instanceof Error) return e.message || fallback;
  if (typeof e === 'string') return e || fallback;
  if (e && typeof e === 'object' && 'message' in e) {
    const m = e.message;
    if (typeof m === 'string' && m) return m;
  }
  return fallback;
}

/**
 * 发请求并解析响应。
 *
 * @param base API 前缀
 * @param path 端点路径（**不带**前缀）
 */
async function request<T = unknown>(
  base: string,
  path: string,
  opts: RequestInit = {},
): Promise<T> {
  // ⚠️ 不能把 opts.headers 直接展开——HeadersInit 可能是数组或 Headers
  // 实例（展开后属性全丢）。用 Headers 归一：调用方显式给了
  // Content-Type 就用它，否则默认 JSON。
  const headers = new Headers(opts.headers);
  if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  const resp = await fetch(`${base}${path}`, { ...opts, headers });

  if (resp.status === 401) {
    // 会话失效：清登录态并回登录页。不整页 reload（保留 SPA 路由栈，
    // 避免丢失滚动状态）；动态 import 避免与 router/pages 形成静态循环依赖。
    const [{ useAuthStore }, { default: router }] = await Promise.all([
      import('./stores/auth'),
      import('./router'),
    ]);
    const auth = useAuthStore();
    auth.authenticated = false;
    auth.username = null;
    if (router.currentRoute.value.path !== '/login') {
      await router.push('/login');
    }
    throw new ApiError(401, '未登录');
  }

  if (!resp.ok) {
    let detail = `HTTP ${String(resp.status)}`;
    try {
      const data = (await resp.json()) as { detail?: string };
      // 空串也退回 HTTP 状态文案（后端不会返回空 detail，这里只是保险）
      detail = (data.detail ?? '') || detail;
    } catch {
      // 响应体不是 JSON（网关错误页等）——保留 HTTP 状态文案
    }
    throw new ApiError(resp.status, detail);
  }

  if (resp.status === 204) return undefined as T;
  return resp.json();
}

/** 辅种域的请求（`/api/v1/*`）。 */
export function apiFetch<T = unknown>(path: string, opts: RequestInit = {}): Promise<T> {
  return request<T>(BASE, path, opts);
}

/** 媒体库域的请求（`/api/*`）。 */
export function mediaFetch<T = unknown>(path: string, opts: RequestInit = {}): Promise<T> {
  return request<T>(BASE_MEDIA, path, opts);
}

/**
 * 组装带请求体的 RequestInit。
 *
 * ⚠️ **不能写成 `body: body ? JSON.stringify(body) : undefined`**：
 * ① `exactOptionalPropertyTypes` 下可选属性不能显式赋 `undefined`
 *    （`RequestInit.body?: string | null`）——必须让整个 `body` 键**缺席**；
 * ② 真值判断会把 `0` / `false` / `''` 这类**合法请求体**吞成 undefined。
 */
function withBody(method: string, body?: unknown): RequestInit {
  return body === undefined ? { method } : { method, body: JSON.stringify(body) };
}

/**
 * 辅种域的请求方法。
 *
 * 以 GET / POST 为主（部分代理/防火墙会丢 PUT/DELETE）。
 * 删除走专用 POST 端点，id 放 body：
 *   POST /{resource}/update   body={id, ...fields}
 *   POST /{resource}/delete   body={id}
 * PUT 仅用于运行时参数更新（调度作业配置弹框）。
 */
export const api = {
  get: <T = unknown>(path: string) => apiFetch<T>(path),
  post: <T = unknown>(path: string, body?: unknown) => apiFetch<T>(path, withBody('POST', body)),
  put: <T = unknown>(path: string, body?: unknown) => apiFetch<T>(path, withBody('PUT', body)),
};

/**
 * 媒体库域的请求方法。
 *
 * 媒体库域的端点用标准的 REST 动词（PATCH / DELETE）——没有辅种域
 * 「代理会丢 PUT/DELETE」的历史包袱。
 */
export const mediaApi = {
  get: <T = unknown>(path: string) => mediaFetch<T>(path),
  post: <T = unknown>(path: string, body?: unknown) => mediaFetch<T>(path, withBody('POST', body)),
  put: <T = unknown>(path: string, body?: unknown) => mediaFetch<T>(path, withBody('PUT', body)),
  patch: <T = unknown>(path: string, body?: unknown) =>
    mediaFetch<T>(path, withBody('PATCH', body)),
  del: <T = unknown>(path: string) => mediaFetch<T>(path, { method: 'DELETE' }),
};
