/**
 * 请求层回归测试（`web/src/api.ts`）。
 *
 * 锁住两处**曾经静默改变运行时行为**的逻辑（PR #38 的 review 提出）：
 *
 * 1. `request()` 的请求头归一化——此前 `opts.headers` 被直接展开，
 *    **数组 / `Headers` 实例**形式会整体丢失；现改用 `new Headers()` 归一。
 * 2. `withBody()` 的请求体构造——此前 `body ? ... : undefined` 的真值判断
 *    会把 `0` / `false` / `''` / `null` 这类**合法请求体**吞成「无 body」。
 *
 * 运行：`npm test`（Node 内置 `node:test`，**不引入新依赖**）。
 * 需要 Node ≥ 22.18（原生 TypeScript 类型擦除；CI 与 Docker 都是 node:22）。
 *
 * 单独放在 `web/tests/`（不混进 `web/src/`）——测试跑在 **Node** 环境，
 * 需要 `node:test` / `node:assert` 的类型；这些类型**不能**进应用工程
 * （会把 Node 全局带进浏览器代码，`setInterval` 的返回类型会变）。
 * 类型工程见 `web/tsconfig.test.json`。
 */
import { equal, ok } from 'node:assert/strict';
import { afterEach, describe, it } from 'node:test';
import { ApiError, api, apiFetch } from '../src/api.ts';

/** 一次被捕获的请求（打桩 fetch 记录）。 */
interface CapturedRequest {
  url: string;
  method: string;
  headers: Headers;
  body: string | undefined;
  /** 请求体键是否存在（区分「键缺席」与「值为 undefined」） */
  hasBodyKey: boolean;
}

const originalFetch = globalThis.fetch;

/**
 * 打桩 fetch：记录请求参数，并按 `respond` 返回响应。
 *
 * @returns 取出最近一次请求的读取器（未发生请求则抛错）
 */
function stubFetch(respond: () => Response): () => CapturedRequest {
  let last: CapturedRequest | undefined;
  globalThis.fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const body = init?.body;
    last = {
      url: typeof input === 'string' ? input : input instanceof URL ? input.href : input.url,
      method: init?.method ?? 'GET',
      headers: new Headers(init?.headers),
      body: typeof body === 'string' ? body : undefined,
      hasBodyKey: init !== undefined && 'body' in init,
    };
    return Promise.resolve(respond());
  };
  return () => {
    if (last === undefined) throw new Error('fetch 未被调用');
    return last;
  };
}

/** 200 + JSON 空对象（成功路径的通用响应）。 */
function okResponse(): Response {
  return new Response('{}', {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** 捕获 promise 的拒绝值（成功则返回 null）。 */
async function captureError(p: Promise<unknown>): Promise<Error | null> {
  try {
    await p;
    return null;
  } catch (e) {
    return e instanceof Error ? e : new Error(String(e));
  }
}

afterEach(() => {
  globalThis.fetch = originalFetch;
});

// ⚠️ `describe` / `it` 返回 Promise（顶层可丢弃）——用 `void` 显式标注「有意忽略」
void describe('request() 请求头归一化', () => {
  void it('保留对象形式的请求头，并在缺省时补 Content-Type', async () => {
    const req = stubFetch(okResponse);
    await apiFetch('/x', { headers: { 'X-Test': '1' } });
    equal(req().headers.get('X-Test'), '1');
    equal(req().headers.get('Content-Type'), 'application/json');
  });

  void it('保留数组形式的请求头（展开写法会整体丢失）', async () => {
    const req = stubFetch(okResponse);
    await apiFetch('/x', { headers: [['X-Test', '1']] });
    equal(req().headers.get('X-Test'), '1');
  });

  void it('保留 Headers 实例形式的请求头', async () => {
    const req = stubFetch(okResponse);
    await apiFetch('/x', { headers: new Headers({ 'X-Test': '1' }) });
    equal(req().headers.get('X-Test'), '1');
  });

  void it('调用方显式设置的 Content-Type 优先于默认值', async () => {
    const req = stubFetch(okResponse);
    await apiFetch('/x', { headers: { 'Content-Type': 'text/plain' } });
    equal(req().headers.get('Content-Type'), 'text/plain');
  });
});

void describe('withBody() 请求体构造', () => {
  void it('body 缺席（undefined）→ 请求里不带 body 键', async () => {
    const req = stubFetch(okResponse);
    await api.post('/x');
    equal(req().method, 'POST');
    equal(req().hasBodyKey, false);
    equal(req().body, undefined);
  });

  void it('0 → "0"（真值判断会把它吞成无 body）', async () => {
    const req = stubFetch(okResponse);
    await api.post('/x', 0);
    equal(req().body, '0');
  });

  void it('false → "false"', async () => {
    const req = stubFetch(okResponse);
    await api.post('/x', false);
    equal(req().body, 'false');
  });

  void it('空串 → \'""\'', async () => {
    const req = stubFetch(okResponse);
    await api.post('/x', '');
    equal(req().body, '""');
  });

  void it('null → "null"', async () => {
    const req = stubFetch(okResponse);
    await api.post('/x', null);
    equal(req().body, 'null');
  });

  void it('对象 → JSON 字符串', async () => {
    const req = stubFetch(okResponse);
    await api.post('/x', { id: 7 });
    equal(req().body, '{"id":7}');
  });
});

void describe('request() 错误响应解析', () => {
  void it('JSON 的 detail → 用 detail 文案', async () => {
    stubFetch(
      () =>
        new Response(JSON.stringify({ detail: 'boom' }), {
          status: 400,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    const err = await captureError(api.post('/x'));
    ok(err !== null);
    if (!(err instanceof ApiError)) throw new Error('期望 ApiError');
    equal(err.status, 400);
    equal(err.message, 'boom');
  });

  void it('detail 为空串 → 回退 HTTP 状态文案', async () => {
    stubFetch(
      () =>
        new Response(JSON.stringify({ detail: '' }), {
          status: 400,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    const err = await captureError(api.post('/x'));
    ok(err !== null);
    if (!(err instanceof ApiError)) throw new Error('期望 ApiError');
    equal(err.message, 'HTTP 400');
  });

  void it('响应体不是 JSON → 回退 HTTP 状态文案，且解析失败挂到 cause（不静默吞掉）', async () => {
    stubFetch(
      () =>
        new Response('<html>502</html>', {
          status: 502,
          headers: { 'Content-Type': 'text/html' },
        }),
    );
    const err = await captureError(api.post('/x'));
    ok(err !== null);
    if (!(err instanceof ApiError)) throw new Error('期望 ApiError');
    equal(err.message, 'HTTP 502');
    ok(err.cause !== undefined, '解析失败必须挂到 cause 上，否则不可观测');
  });
});
