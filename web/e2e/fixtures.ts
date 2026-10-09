/**
 * E2E 共用 fixture
 *
 * 启动时读取 global-setup 写的 SH_E2E_SHARED，构造一个共享的 API 客户端
 * 与登录后的 cookie。每个 spec 可以直接用 ``authedPage`` 拿到已登录状态。
 */
import { test as base, expect, type APIRequestContext, type BrowserContext, type Page } from "@playwright/test";
import { readFileSync, existsSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";

export interface E2EShared {
  backendPid: number;
  frontendPid: number;
  workDir: string;
  backendUrl: string;
  frontendUrl: string;
  encryptionKey: string;
  password: string;
  hashedPassword: string;
  token: string;
}

let shared: E2EShared | null = null;

export function getShared(): E2EShared {
  if (shared) return shared;
  const path = process.env.SH_E2E_SHARED ?? join(tmpdir(), "sh-e2e-shared.json");
  if (!existsSync(path)) throw new Error(`SH_E2E_SHARED not found at ${path}`);
  shared = JSON.parse(readFileSync(path, "utf-8"));
  return shared!;
}

/** 已经登录到 / 的 page 上下文（每个用例一个 context，互不污染 cookie） */
export const test = base.extend<{
  authedContext: BrowserContext;
  authedPage: Page;
  apiCtx: APIRequestContext;
}>({
  authedContext: async ({ browser }, use) => {
    const s = getShared();
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    await page.goto(s.frontendUrl + "/login");
    await page.locator('input[placeholder="用户名"]').fill("admin");
    await page.locator('input[placeholder="密码"]').fill(s.password);
    await page.getByRole("button", { name: /登录/ }).click();
    await page.waitForURL(s.frontendUrl + "/", { timeout: 10_000 });
    await use(ctx);
    await ctx.close();
  },
  authedPage: async ({ authedContext }, use) => {
    const page = authedContext.pages()[0] ?? await authedContext.newPage();
    await use(page);
  },
  apiCtx: async ({ playwright }, use) => {
    const s = getShared();
    const ctx = await playwright.request.newContext({
      baseURL: s.backendUrl,
      extraHTTPHeaders: { Authorization: `Bearer ${s.token}` },
    });
    await use(ctx);
    await ctx.dispose();
  },
});

export { expect };

/** 走前端的未登录 page */
export const unauthedTest = base.extend({
  page: async ({ browser }, use) => {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    await use(page);
    await ctx.close();
  },
});
