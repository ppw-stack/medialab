"""光鸭直链「生成 vs 使用」的计费归属探针（2026-11-13）。

回答三个问题（决定「批量预取直链」这条路值不值得走）：

1. **生成直链是否消耗流量/次数**？——预取的意义在于「生成」是瓶颈
2. **用（预先生成的）链接做云下载是否照常可用**？
3. **云下载的传输是否消耗流量**？——消耗在**源账号**还是**目标账号**？

做法：源 = 小号1（免费档，有流量计数器可观测）、目标 = 光鸭盘（VIP，额度充足）
→ 批量生成 5 条直链 → 记录源侧计数器 → 用其中 3 条提交云下载 → 记录两侧。

跑法（生产容器，凭据不出容器）::

    ssh unraid "docker exec -i medialab /opt/venv/bin/python - prelink" \\
        < scripts/probe_guangya_prelink.py

安全边界：
- 只建临时目录、跑完自删（fs_delete → 回收站）；不新建/不上传任何源文件
- 消耗 **3 次云添加额度**（目标账号）+ 少量直链调用
- token 刷新走 app 自己的 store_credential 写回
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.environ.get("MEDIALAB_APP_PATH", "/app"))

from app.core.config import get_settings
from app.db.models import Driver
from app.db.session import init_db, session_scope
from app.services.driver_factory import build_driver
from app.services.migration import persist_guangya_credential
from sqlalchemy import select

SOURCE_DRIVER_ID = int(os.environ.get("MEDIALAB_PROBE_SOURCE", "3"))  # 小号1（免费档）
TARGET_DRIVER_ID = int(os.environ.get("MEDIALAB_PROBE_TARGET", "2"))  # 光鸭盘（VIP）
LINK_COUNT = 5
CLOUD_ADD_COUNT = 3


def log(msg: str) -> None:
    print(msg, flush=True)


def make_callback(driver_id: int):
    async def _cb(cred) -> None:
        await persist_guangya_credential(driver_id, cred)

    return _cb


async def get_assets(driver) -> dict:
    """取账号资产信息（含 highSpeedTraffic 计数器）。

    **必须先刷新 token**——过期时接口返回 117 + 空 data，字段全缺
    （曾把「字段缺失」误判成「不限流量」）。
    """
    import httpx
    from app.adapters.drivers.guangya import _client_id

    await driver._ensure_token()
    cred = driver._cred
    client = driver.client
    headers = {
        "Authorization": f"Bearer {client.token}",
        "X-Client-Id": _client_id(),
        "X-Device-Id": cred.device_id if cred else "",
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
    }
    resp = await asyncio.to_thread(
        lambda: httpx.post(
            "https://api.guangyapan.com/nd.bizassets.s/v1/get_assets",
            json={},
            headers=headers,
            timeout=20.0,
        )
    )
    payload = resp.json()
    if payload.get("code") not in (None, 0):
        raise RuntimeError(
            f"get_assets 失败：code={payload.get('code')} msg={payload.get('msg')}"
        )
    return payload.get("data") or {}


def traffic_used(data: dict) -> str:
    """把 highSpeedTraffic 计数压成一行可读文本。

    ⚠️ **「字段缺失」≠「不限」**：字段不存在说明这次请求没拿到资产信息
    （token 过期 / 接口异常），必须与「显式 None（不限流量）」区分开。
    """
    if "highSpeedTraffic" not in data:
        return "未知（字段缺失——请求没拿到资产信息）"
    hst = data.get("highSpeedTraffic")
    if hst is None:
        return "不限（VIP）"
    details = hst.get("details") or []
    used = sum(int(d.get("used") or 0) for d in details)
    return (
        f"total={hst.get('total')} remained={hst.get('remained')} "
        f"used={used}（details={details}）"
    )


async def pick_videos(driver, count: int) -> list[tuple[str, str]]:
    """挑 N 个视频文件（fileId, 文件名）——按体积从小到大。"""
    page = 0
    files: list[dict] = []
    while True:
        payload = await asyncio.to_thread(
            driver.client.fs_files,
            parent_id="*",
            page=page,
            page_size=1000,
            res_type=1,
        )
        batch = list((payload.get("data") or {}).get("list") or [])
        if not batch:
            break
        files.extend(batch)
        page += 1
        if page >= 50:
            break
        await asyncio.sleep(0.2)
    videos = [
        f
        for f in files
        if str(f.get("fileName", "")).lower().endswith((".mkv", ".mp4"))
    ]
    videos.sort(key=lambda f: int(f.get("fileSize") or 0))
    # ⚠️ **同名去重**：小号账号里同名文件可能有两份（不同路径各一份），
    # 落到同一个目标目录会撞「文件名已存在」
    seen: set[str] = set()
    picked: list[tuple[str, str]] = []
    for f in videos:
        name = str(f.get("fileName"))
        if name in seen:
            continue
        seen.add(name)
        picked.append((str(f.get("fileId")), name))
        if len(picked) >= count:
            break
    return picked


async def wait_task(driver, task_id: str, wait_secs: float = 300.0) -> None:
    """等任务从活跃列表消失（连续 2 次缺席）。"""
    absent = 0
    deadline = time.time() + wait_secs
    while time.time() < deadline:
        items = await driver.list_active_offline_tasks()
        ids = {str(i.get("taskId")) for i in items}
        absent = 0 if task_id in ids else absent + 1
        if absent >= 2:
            return
        await asyncio.sleep(5.0)
    log(f"  ⚠️ 等待任务结束超时（{task_id}）")


async def main() -> None:
    settings = get_settings()
    init_db(settings)

    async with session_scope() as db:
        rows = (
            await db.execute(
                select(Driver).where(Driver.id.in_([SOURCE_DRIVER_ID, TARGET_DRIVER_ID]))
            )
        ).scalars().all()
    info = {r.id: (r.name, r.credential) for r in rows}

    src = build_driver(
        "guangya", info[SOURCE_DRIVER_ID][1], None,
        on_token_refresh=make_callback(SOURCE_DRIVER_ID),
    )
    tgt = build_driver(
        "guangya", info[TARGET_DRIVER_ID][1], None,
        on_token_refresh=make_callback(TARGET_DRIVER_ID),
    )
    log(f"源：驱动 {SOURCE_DRIVER_ID}（{info[SOURCE_DRIVER_ID][0]}）")
    log(f"目标：驱动 {TARGET_DRIVER_ID}（{info[TARGET_DRIVER_ID][0]}）")

    # ---------- ① 基线 ----------
    src_before = await get_assets(src)
    tgt_before = await get_assets(tgt)
    log(f"\n① 基线　源 traffic: {traffic_used(src_before)}")
    log(f"　　　　目标 traffic: {traffic_used(tgt_before)}")

    # ---------- ② 批量生成直链 ----------
    log(f"\n② 批量生成 {LINK_COUNT} 条直链（源账号）")
    picks = await pick_videos(src, LINK_COUNT)
    if not picks:
        raise SystemExit("❌ 源账号里没有视频文件")
    links: list[tuple[str, str, str]] = []
    for file_id, name in picks:
        link = await src.get_direct_link(file_id, "")
        links.append((file_id, name, link.url))
        log(f"   已生成：{name[:40]}  → {link.url[:80]}...")
    src_after_gen = await get_assets(src)
    log(f"   生成后源 traffic: {traffic_used(src_after_gen)}")

    # ---------- ③ 用预先生成的链接做云下载 ----------
    tmp_name = f"__prelink_probe_{int(time.time())}__"
    dir_id, _created = await tgt.create_dir(tmp_name, "")
    log(f"\n③ 用预取的链接云下载 {CLOUD_ADD_COUNT} 条 → 目标临时目录 {tmp_name}")
    try:
        for _file_id, name, url in links[:CLOUD_ADD_COUNT]:
            task_id = await tgt.create_offline_task(url, dir_id)
            log(f"   已提交：{name[:40]}  taskId={task_id}")
            await wait_task(tgt, task_id)
            entries = await tgt.list_entries(dir_id)
            log(f"     落盘目录：{ {k: v[0] for k, v in entries.items()} }")
            # 改名回原名（与迁移引擎一致）
            for landed_name, (size, landed_id) in list(entries.items()):
                if landed_name == "download" and size > 0:
                    try:
                        await tgt.rename_file(landed_id, name)
                        log(f"     已改名：download → {name[:40]}")
                    except Exception as exc:
                        log(f"     ⚠️ 改名失败（{name[:30]}）：{exc}")
                    break
        final = await tgt.list_entries(dir_id)
        log(f"   最终目录内容：{ {k: v[0] for k, v in final.items()} }")
    finally:
        log("\n④ 清理（fs_delete → 回收站）")
        try:
            entries = await tgt.list_entries(dir_id)
            ids = [v[1] for v in entries.values()]
            if ids:
                tgt.client.fs_delete(ids)
            tgt.client.fs_delete([dir_id])
            log("   已清理")
        except Exception as exc:
            log(f"   ⚠️ 清理异常（请人工检查 {tmp_name}）：{exc}")

    # ---------- ④ 终态计数 ----------
    src_after = await get_assets(src)
    tgt_after = await get_assets(tgt)
    log(f"\n⑤ 终态　源 traffic: {traffic_used(src_after)}")
    log(f"　　　　目标 traffic: {traffic_used(tgt_after)}")
    log("\n判读：")
    log("   - 生成阶段源计数涨了 → 「生成直链」消耗流量/次数（预取不能减量）")
    log("   - 生成阶段没涨、传输阶段源涨了 → 流量算在源账号的「使用」上")
    log("   - 两边都没涨 → 云下载既不消耗直链流量也不计生成次数")


if __name__ == "__main__":
    asyncio.run(main())
