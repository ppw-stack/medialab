/**
 * 认证 E2E（FR-6.1 面板登录）
 *
 * 覆盖：
 *  1. 错误密码 401 + 不跳转
 *  2. 正确密码跳转 /，看到 Dashboard
 *  3. 直接访问受保护 API：401（apiCtx 用 Bearer 通过）
 *  4. 401 响应在前端触发跳转 /login
 *  5. logout 后 cookie 失效
 *  6. /api/v1/auth/status 反映状态
 */
import { expect, test, getShared } from "./fixtures";

test.describe("Authentication (FR-6.1)", () => {
  test("错误密码 401 且停留登录页", async ({ browser }) => {
    const s = getShared();
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    await page.goto(s.frontendUrl + "/login");
    await page.locator('input[placeholder="用户名"]').fill("admin");
    await page.locator('input[placeholder="密码"]').fill("wrong-password");
    await page.getByRole("button", { name: /登录/ }).click();
    await expect(page.locator("text=用户名或密码错误")).toBeVisible({ timeout: 5_000 });
    await expect(page).toHaveURL(/\/login$/);
    await ctx.close();
  });

  test("正确密码跳转 /，看到 Dashboard 标题", async ({ authedPage }) => {
    const s = getShared();
    // authedPage 已经登录；当前 URL 应是 /
    await expect(authedPage).toHaveURL(s.frontendUrl + "/");
    // Dashboard 标题
    await expect(authedPage.locator("h4", { hasText: "总览仪表盘" })).toBeVisible();
  });

  test("受保护端点不带 cookie 返回 401", async ({ request }) => {
    const s = getShared();
    const r = await request.get(s.backendUrl + "/api/v1/dashboard");
    expect(r.status()).toBe(401);
  });

  test("受保护端点带 Bearer token 通过", async ({ apiCtx }) => {
    const r = await apiCtx.get("/api/v1/dashboard");
    expect(r.ok()).toBeTruthy();
    const body = await r.json();
    expect(body).toHaveProperty("sites");
    expect(body).toHaveProperty("downloaders");
  });

  test("401 响应在前端触发跳转 /login", async ({ browser }) => {
    const s = getShared();
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    // 不登录直接访问受保护页 → SPA 路由：先尝试 load（401）→ api.ts 触发跳转
    await page.goto(s.frontendUrl + "/sites", { waitUntil: "domcontentloaded" });
    // 给前端最多 2s 跳转
    await page.waitForURL(/\/login$/, { timeout: 5_000 });
    await expect(page).toHaveURL(/\/login$/);
    await ctx.close();
  });

  test("logout 后 cookie 失效，refresh 跳 /login", async ({ authedContext, authedPage, browser }) => {
    // 触发 logout
    await authedPage.locator(".ant-avatar").click();
    await authedPage.getByText("退出登录").click();
    // 等待跳回 /login
    await authedPage.waitForURL(/\/login$/, { timeout: 5_000 });
    // 此时新 context 用旧 cookie 应 401
    const newCtx = await browser.newContext();
    // 把旧 cookie 拷过去
    const oldCookies = await authedContext.cookies();
    await newCtx.addCookies(oldCookies);
    const resp = await newCtx.request.get(
      getShared().backendUrl + "/api/v1/dashboard",
    );
    // 服务端 delete_cookie 已下发 Max-Age=0，旧 cookie 被浏览器标记为过期
    expect(resp.status()).toBe(401);
    await newCtx.close();
  });

  test("/api/v1/auth/status 反映状态", async ({ apiCtx }) => {
    const r = await apiCtx.get("/api/v1/auth/status");
    expect(r.ok()).toBeTruthy();
    const body = await r.json();
    // 我们用 Bearer 登录——status 应该是 authenticated=true，username="token"
    expect(body.authenticated).toBe(true);
    expect(body.username).toBe("token");
  });
});
