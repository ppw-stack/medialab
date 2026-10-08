const BASE = "/api/v1";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
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
    window.location.href = "/login";
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

export const api = {
  get: <T = unknown>(path: string) => apiFetch<T>(path),
  post: <T = unknown>(path: string, body?: unknown) =>
    apiFetch<T>(path, {
      method: "POST",
      body: body ? JSON.stringify(body) : undefined,
    }),
  put: <T = unknown>(path: string, body: unknown) =>
    apiFetch<T>(path, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  del: <T = unknown>(path: string) =>
    apiFetch<T>(path, { method: "DELETE" }),
};
