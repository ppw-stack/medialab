/**
 * Dashboard + Settings + 调度作业 E2E
 *
 * 覆盖：
 *  1. 统计卡片渲染
 *  2. 设置页：告警策略表单加载、修改并保存（mutation 实际生效）
 *  3. 调度作业：6 个真实作业 + 启停开关
 *  4. 通知渠道 CRUD：新增 + 测试（mock） + 删除
 *  5. 401 → 自动跳 /login（重复一个保险）
 */
import { expect, test, getShared } from "./fixtures";

test.describe("Dashboard", () => {
  test("统计卡片正常显示", async ({ authedPage }) => {
    await expect(authedPage.locator("text=总览仪表盘")).toBeVisible();
    // 四个统计卡
    await expect(authedPage.locator("text=站点（在线 / 总数）")).toBeVisible();
    await expect(authedPage.locator("text=下载器（启用 / 总数）")).toBeVisible();
    await expect(authedPage.locator("text=活跃任务")).toBeVisible();
    await expect(authedPage.locator("text=暂停")).toBeVisible();
  });
});

test.describe("Settings / 告警策略", () => {
  test("加载与保存", async ({ authedPage }) => {
    await authedPage.goto("/settings");
    // 切到告警策略标签
    await authedPage.getByRole("tab", { name: "告警策略" }).click();

    // 加载默认值
    const muteInput = authedPage.locator('input[role="spinbutton"]').first();
    await expect(muteInput).toHaveValue("6", { timeout: 5_000 });

    // 修改：风暴阈值从 50 改到 30
    const stormInput = authedPage.locator('input[role="spinbutton"]').nth(3);
    // 用 .fill 强制写
    await stormInput.click({ clickCount: 3 });
    await stormInput.fill("30");
    await authedPage.getByRole("button", { name: "保存" }).click();

    // 验证：再次访问 /alerts/policy 应当读到 30
    const r = await authedPage.request.get("/api/v1/alerts/policy");
    const body = await r.json();
    expect(body.storm_threshold).toBe(30);

    // 改回去（不影响其它测试）
    await stormInput.click({ clickCount: 3 });
    await stormInput.fill("50");
    await authedPage.getByRole("button", { name: "保存" }).click();
  });
});

test.describe("Settings / 通知渠道 CRUD", () => {
  test("新增 Webhook 渠道 → 列表出现 → 渠道测试 → 删除", async ({ authedPage, apiCtx }) => {
    const uniqueName = `e2e-webhook-${Date.now()}`;
    await authedPage.goto("/settings");
    await authedPage.getByRole("tab", { name: "通知渠道" }).click();

    // 新增
    await authedPage.getByRole("button", { name: /新增渠道/ }).click();
    await authedPage.locator('input#name').fill(uniqueName);
    // 提交（type 默认 webhook，secret 留空）
    await authedPage.getByRole("button", { name: "确 定" }).click({ timeout: 5_000 });

    // 列表应出现
    await expect(authedPage.locator(`text=${uniqueName}`)).toBeVisible({ timeout: 5_000 });

    // 通过 API 测试：未配置 secret 时应失败
    const id = (await apiCtx.get("/alerts/channels"))
      .then((r) => r.json())
      .then((cs) => cs.find((c: { name: string }) => c.name === uniqueName)?.id);
    expect(id).toBeTruthy();
    const r = await apiCtx.post(`/alerts/channels/${id}/test`);
    expect(r.ok()).toBeTruthy();
    const body = await r.json();
    // 缺少 url 应当失败
    expect(body.ok).toBe(false);

    // 删除
    await apiCtx.delete(`/alerts/channels/${id}`);
    const after = await (await apiCtx.get("/alerts/channels")).json();
    expect(after.find((c: { id: number }) => c.id === id)).toBeUndefined();
  });
});

test.describe("Settings / 调度作业", () => {
  test("6 个真实作业 + 启停 cross_seed", async ({ authedPage, apiCtx }) => {
    await authedPage.goto("/settings");
    await authedPage.getByRole("tab", { name: "调度作业" }).click();

    // 6 个作业名
    const expectedJobs = [
      "辅种", "队列重放", "巡检", "免费种拉取", "窗口看护", "中止处理",
    ];
    for (const j of expectedJobs) {
      await expect(authedPage.locator(`text=${j}`).first()).toBeVisible();
    }

    // 启停 cross_seed
    const before = await (await apiCtx.get("/scheduler")).json();
    const beforeJob = before.jobs.find((j: { name: string }) => j.name === "cross_seed");
    expect(beforeJob.enabled).toBe(true);

    // 找到含 cross_seed 行的 Switch
    const switchInput = authedPage.locator("tr", { hasText: "辅种拉取" })
      .locator("button[role='switch']");
    await switchInput.click();
    await expect(authedPage.locator("text=cross_seed → 暂停")).toBeVisible({ timeout: 3_000 });

    // API 验证
    const after = await (await apiCtx.get("/scheduler")).json();
    const afterJob = after.jobs.find((j: { name: string }) => j.name === "cross_seed");
    expect(afterJob.enabled).toBe(false);

    // 恢复
    await switchInput.click();
    await expect(authedPage.locator("text=cross_seed → 启用")).toBeVisible({ timeout: 3_000 });
  });
});
