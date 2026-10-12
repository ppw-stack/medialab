# AGENTS.md — 仓库内 AI/协作者行为规范

## 必读文档
1. `.orca/drops/PT辅种与自动监测系统-项目需求文档.md` v1.2 — 需求基线。
2. `README.md` — 现状与命令。

## 工作准则
- 改动窗口锚定（FR-3.4）或中止链路（FR-3.5）前，先看需求文档 §3 / FR-5.2。
- 任何对数据删除的路径都需经 FR-5.2 六重保护（白名单/回收站/路径三重校验/二次确认/保护期/审计）。
- 站点适配层改动后用真实站点（测试环境 cookie）拉取验证，不使用 mock 数据测试。
- 不引入未文档化的反爬对抗 / 验证码绕过（R2 / 合规性）。

## 必备命令
```bash
uv sync --extra dev         # 装依赖
uv run ruff check app/
uv run mypy app/
```

静态检查不通过禁止合并。

## 提交信息格式
Conventional Commits，作用域示例：`feat(crypto)` / `fix(free-seed)` / `docs(req)` / `refactor(sites)`。
