"""光鸭 → 光鸭「直链 → 云下载 → 改名」探针。

两种模式：

- **``probe``**（默认）：单文件端到端——取直链 → 提交云下载 → 等任务结束 →
  改名回原名 → 校验 → 自清理。回答三个问题：
  1. **云添加接受光鸭 CDN 直链吗**（此前实测「疑似只吃媒体 URL」，而光鸭
     直链 URL 无扩展名，末段是 ``download``）
  2. **落盘名是什么**——预期是 ``download``，需要改名
  3. **改名是否可行**——``fs_rename`` 后能否变回原名、大小一致
- **``engine``**：跑**真实的迁移引擎**（``migration.run_migration``）——
  挑一个小目录当源 → 落到目标账号的临时目录 → 校验结构与改名 →
  **再跑一遍验证幂等**（同名同大小应全部跳过）→ 自清理。

跑法（生产容器，凭据不出容器）::

    ssh unraid "docker exec -i medialab /opt/venv/bin/python -" \\
        < scripts/probe_guangya_migration.py
    ssh unraid "docker exec -i medialab /opt/venv/bin/python - engine" \\
        < scripts/probe_guangya_migration.py

安全边界：

- 源文件是**已有的**（不新建、不上传）；目标端只建**临时目录**，跑完自删
- 删除走 ``fs_delete``（进回收站），**绝不**碰回收站清空接口
- ``probe`` 消耗 **1 次**云添加额度；``engine`` 消耗源目录里文件个数那么多
  次（失败也扣——这是既定代价）
- token 刷新走 app 自己的 ``store_credential`` 写回（裸 sqlite3 写 shfs 上的
  WAL 库已实测不可靠）
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

#: 应用代码路径——默认容器内的 ``/app``；验证未部署的新代码时可用
#: ``MEDIALAB_APP_PATH=/tmp/newapp`` 指向注入的新代码（不改生产镜像）。
sys.path.insert(0, os.environ.get("MEDIALAB_APP_PATH", "/app"))

from app.core.config import get_settings
from app.db.models import Driver
from app.db.session import init_db, session_scope
from app.services.driver_factory import build_driver
from app.services.migration import persist_guangya_credential
from sqlalchemy import select

SOURCE_DRIVER_ID = int(os.environ.get("MEDIALAB_PROBE_SOURCE", "2"))  # 光鸭盘（源）
TARGET_DRIVER_ID = int(os.environ.get("MEDIALAB_PROBE_TARGET", "3"))  # 小号1（目标）
TARGET_FILE = "长安的荔枝 - S01E08 - 第 8 集.mp4"

POLL_INTERVAL = 5.0
ABSENT_CONFIRM = 2  # 连续两次不在活跃列表才算结束
WATCH_TIMEOUT = 900.0  # 15 分钟上限
HEARTBEAT = 30.0


def log(msg: str) -> None:
    print(msg, flush=True)


def make_callback(driver_id: int):
    async def _cb(cred) -> None:
        await persist_guangya_credential(driver_id, cred)

    return _cb


def fs_files_retry(client, **kwargs) -> dict:
    """``fs_files`` 带重试——生产容器 DNS 偶发解析失败（已知问题）。"""
    last: Exception | None = None
    for attempt in range(4):
        try:
            return client.fs_files(**kwargs)
        except Exception as exc:
            last = exc
            wait = 5 * (attempt + 1)
            log(f"  ⚠️ fs_files 第 {attempt + 1} 次失败：{exc}（{wait}s 后重试）")
            time.sleep(wait)
    raise RuntimeError(f"fs_files 重试耗尽：{last}")


async def find_source_file(driver, filename: str) -> dict | None:
    """在全账号文件列表里按文件名定位（fs_files 支持 parent_id="*"）。"""
    page = 0
    while True:
        payload = fs_files_retry(
            driver.client, parent_id="*", page=page, page_size=1000, res_type=1
        )
        batch = list((payload.get("data") or {}).get("list") or [])
        if not batch:
            return None
        for item in batch:
            if item.get("fileName") == filename:
                return item
        page += 1
        if page >= 100:
            return None
        await asyncio.sleep(0.2)


async def main() -> None:
    settings = get_settings()
    init_db(settings)

    async with session_scope() as db:
        rows = (
            await db.execute(
                select(Driver).where(Driver.id.in_([SOURCE_DRIVER_ID, TARGET_DRIVER_ID]))
            )
        ).scalars().all()
    info = {r.id: (r.type, r.name, r.credential) for r in rows}
    if SOURCE_DRIVER_ID not in info or TARGET_DRIVER_ID not in info:
        raise SystemExit("❌ 驱动不存在")

    src = build_driver(
        info[SOURCE_DRIVER_ID][0], info[SOURCE_DRIVER_ID][2], None,
        on_token_refresh=make_callback(SOURCE_DRIVER_ID),
    )
    tgt = build_driver(
        info[TARGET_DRIVER_ID][0], info[TARGET_DRIVER_ID][2], None,
        on_token_refresh=make_callback(TARGET_DRIVER_ID),
    )
    log(f"源：驱动 {SOURCE_DRIVER_ID}（{info[SOURCE_DRIVER_ID][1]}）")
    log(f"目标：驱动 {TARGET_DRIVER_ID}（{info[TARGET_DRIVER_ID][1]}）")

    # ---------- ① 定位源文件 ----------
    log(f"\n▶ 查找源文件：{TARGET_FILE}")
    item = await find_source_file(src, TARGET_FILE)
    if item is None:
        raise SystemExit(f"❌ 源账号里找不到 {TARGET_FILE}")
    file_id = str(item.get("fileId"))
    size = int(item.get("fileSize") or 0)
    log(
        f"  找到：fileId={file_id}  {size / 1024**2:.1f}MB  "
        f"shareAuditStatus={item.get('shareAuditStatus')}  canShare={item.get('canShare')}"
    )

    # ---------- ② 取直链 ----------
    log("\n▶ 换取直链（光鸭源不绑 UA）")
    link = await src.get_direct_link(file_id, "")
    log(f"  ✅ 直链已取到  expires_at={link.expires_at}  ua_required={link.ua_required}")
    log(f"  URL: {link.url[:110]}...")
    log(f"  URL 末段: {link.url.split('?')[0].rstrip('/').split('/')[-1]!r}  ← 预期落盘名来源")

    # ---------- ③ 目标临时目录 ----------
    tmp_name = f"__relay_p0_{int(time.time())}__"
    dir_id, created = await tgt.create_dir(tmp_name, "")
    log(f"\n▶ 目标临时目录：{tmp_name}（fileId={dir_id}，新建={created}）")

    task_id = ""
    try:
        # ---------- ④ 提交云下载 ----------
        log("\n▶ 提交云下载任务（cloud_create_task）")
        t0 = time.time()
        task_id = await tgt.create_offline_task(link.url, dir_id)
        log(f"  ✅ 已提交 taskId={task_id}（{time.time() - t0:.1f}s）")

        # ---------- ⑤ 轮询直到任务从活跃列表消失 ----------
        log("\n▶ 轮询活跃任务列表（任务消失即结束，连续 2 次缺席确认）")
        absent = 0
        deadline = time.time() + WATCH_TIMEOUT
        last_beat = 0.0
        while time.time() < deadline:
            active = await tgt.list_active_offline_tasks()
            ids = {str(t.get("taskId")) for t in active}
            still = task_id in ids
            if not still:
                absent += 1
            else:
                absent = 0
            if time.time() - last_beat >= HEARTBEAT:
                last_beat = time.time()
                log(
                    f"  ⏱️ {int(time.time() - t0)}s  活跃任务 {len(active)} 个  "
                    f"本任务{'仍在' if still else '已消失'}（缺席 {absent}/{ABSENT_CONFIRM}）"
                )
            if absent >= ABSENT_CONFIRM:
                log(f"  ✅ 任务已结束（{int(time.time() - t0)}s）")
                break
            await asyncio.sleep(POLL_INTERVAL)
        else:
            log(f"  ⚠️ 等待超时（{int(WATCH_TIMEOUT)}s），任务仍在跑")

        # ---------- ⑥ 看落盘结果 ----------
        log("\n▶ 检查落盘结果")
        entries = await tgt.list_entries(dir_id)
        log(f"  目标目录内容：{ {k: v[0] for k, v in entries.items()} }")
        if not entries:
            failed = await tgt.list_failed_offline_tasks()
            for t in failed[:5]:
                log(
                    f"  ❌ 失败任务：taskId={t.get('taskId')} errCode={t.get('errCode')} "
                    f"errMsg={t.get('errMsg')} fileName={t.get('fileName')}"
                )
            log("  ❌ 目录为空——云下载未产出文件")
        else:
            name, (fsize, fid) = next(iter(entries.items()))
            log(f"  落盘文件：name={name!r}  size={fsize}  fileId={fid}")
            log(f"  期望大小：{size}  →  {'✅ 一致' if fsize == size else '❌ 不一致'}")

            # ---------- ⑦ 改名 ----------
            log("\n▶ 改名（fs_rename → 原名）")
            rename_payload = tgt.client.fs_rename(fid, TARGET_FILE)
            rcode = rename_payload.get("code")
            log(f"  fs_rename 返回 code={rcode} msg={rename_payload.get('msg')}")
            await asyncio.sleep(1.5)
            after = await tgt.list_entries(dir_id)
            log(f"  改名后目录内容：{ {k: v[0] for k, v in after.items()} }")
            hit = after.get(TARGET_FILE)
            if hit:
                log(
                    f"  ✅ 改名成功：{TARGET_FILE}  size={hit[0]}  "
                    f"{'大小一致' if hit[0] == size else '❌ 大小不一致'}"
                )
            else:
                log("  ❌ 改名后目录里没有原名")
    finally:
        # ---------- ⑧ 清理 ----------
        log("\n▶ 清理（fs_delete → 回收站）")
        try:
            entries = await tgt.list_entries(dir_id)
            ids = [v[1] for v in entries.values()]
            if ids:
                tgt.client.fs_delete(ids)
                log(f"  已删除文件 {len(ids)} 个（进回收站）")
            tgt.client.fs_delete([dir_id])
            log("  已删除临时目录（进回收站）")
        except Exception as exc:
            log(f"  ⚠️ 清理异常（请人工检查 {tmp_name}）：{exc}")
        await asyncio.sleep(1.0)
        rest = await tgt.list_entries(dir_id)
        log(f"  清理后目录内容：{rest if rest else '（空）'}")


# --------------------------------------------------------------------------
# engine 模式：跑真实迁移引擎
# --------------------------------------------------------------------------


async def _pick_source_dir(
    driver,
    max_files: int = 12,
    max_video_bytes: int = 2 * 1024**3,
    video_exts: tuple[str, ...] = (".mkv", ".mp4", ".avi", ".ts", ".m2ts"),
):
    """在源账号里挑一个小目录当源（**至少 1 个视频**文件）。

    **不新建、不上传**——只用已有的文件。

    目录里混着 jpg / nfo 没关系——迁移引擎按存储源的 ``video_exts`` 过滤，
    非视频会被丢掉（顺带验证了过滤逻辑）。按**视频**总体积挑小的：快、
    省云添加额度。
    """
    dirs, page = [], 0
    while True:
        payload = fs_files_retry(
            driver.client,
            parent_id="*", page=page, page_size=1000, res_type=2,
        )
        batch = list((payload.get("data") or {}).get("list") or [])
        if not batch:
            break
        dirs.extend(batch)
        page += 1
        if page >= 100:
            break
        await asyncio.sleep(0.2)
    best: tuple | None = None
    for d in dirs:
        dir_id = str(d.get("fileId") or "")
        name = str(d.get("fileName") or "")
        if not dir_id or not name or name.startswith("."):
            continue
        payload = fs_files_retry(driver.client, parent_id=dir_id, page=0, page_size=50)
        files = [
            f
            for f in ((payload.get("data") or {}).get("list") or [])
            if int(f.get("resType") or 1) == 1
        ]
        if not files or len(files) > max_files:
            continue
        videos = [
            f
            for f in files
            if str(f.get("fileName", "")).lower().endswith(video_exts)
        ]
        if not videos:
            continue
        if any(str(f.get("fileName", "")).startswith(".mp-backup") for f in files):
            continue
        total = sum(int(f.get("fileSize") or 0) for f in videos)
        if total > max_video_bytes:
            continue
        if best is None or total < best[2]:
            best = (
                dir_id,
                name,
                total,
                [(f.get("fileName"), int(f.get("fileSize") or 0)) for f in videos],
            )
    if best is None:
        return None
    return best[0], best[1], best[3]


async def run_engine_mode() -> None:
    """用真实迁移引擎跑一轮光鸭 → 光鸭，并验证幂等重跑。"""
    from app.services import migration_jobs
    from app.services.migration import run_migration

    settings = get_settings()
    init_db(settings)

    async with session_scope() as db:
        rows = (
            await db.execute(
                select(Driver).where(
                    Driver.id.in_([SOURCE_DRIVER_ID, TARGET_DRIVER_ID])
                )
            )
        ).scalars().all()
    info = {r.id: (r.type, r.name, r.credential) for r in rows}

    src = build_driver(
        "guangya", info[SOURCE_DRIVER_ID][2], None,
        on_token_refresh=make_callback(SOURCE_DRIVER_ID),
    )
    tgt = build_driver(
        "guangya", info[TARGET_DRIVER_ID][2], None,
        on_token_refresh=make_callback(TARGET_DRIVER_ID),
    )
    log(f"源：驱动 {SOURCE_DRIVER_ID}（{info[SOURCE_DRIVER_ID][1]}）")
    log(f"目标：驱动 {TARGET_DRIVER_ID}（{info[TARGET_DRIVER_ID][1]}）")

    log("\n▶ 挑一个小目录当源")
    picked = await _pick_source_dir(src)
    if picked is None:
        raise SystemExit("❌ 源账号里没有合适的小目录（1~2 个文件、<50MB）")
    src_dir_id, src_dir_name, files = picked
    log(f"  源目录：{src_dir_name}（fileId={src_dir_id}）")
    for fname, fsize in files:
        log(f"    - {fname}（{fsize / 1024**2:.1f}MB）")

    tmp_name = f"__relay_probe_{int(time.time())}__"
    dir_id, _created = await tgt.create_dir(tmp_name, "")
    log(f"\n▶ 目标临时目录：{tmp_name}（fileId={dir_id}）")

    try:
        params = migration_jobs.MigrationParams(
            source_driver_id=SOURCE_DRIVER_ID,
            source_cid=src_dir_id,
            source_path=src_dir_name,
            target_driver_id=TARGET_DRIVER_ID,
            target_file_id=dir_id,
            target_path=tmp_name,
            concurrency=5,  # 后端应收敛到 1
            submit_interval_sec=60.0,
        )
        job = migration_jobs.MigrationJob(id="probe", params=params)

        log("\n▶ 第一轮：跑真实迁移引擎")
        await run_migration(job)
        for line in job.logs:
            log(f"  {line}")
        log(f"\n  结果：成功 {job.success} / 跳过 {job.skipped_existing} / "
            f"失败 {job.failed} / 冲突 {job.conflicts}")

        entries = await tgt.list_entries(dir_id)
        log(f"  目标目录内容：{ {k: v[0] for k, v in entries.items()} }")
        # 落盘的文件：名字是原名（不是 download）、大小与源一致
        landed_ok = all(
            name in dict(files) and dict(files)[name] == size
            for name, (size, _) in entries.items()
        )
        log(
            "  ✅ 落盘文件名与大小全部正确"
            if landed_ok
            else "  ❌ 落盘结果不符"
        )
        if job.state == migration_jobs.STATE_PAUSED:
            log(f"  ⏸️ 作业因额度耗尽暂停（已迁 {job.success} 个，剩余文件次日再迁）")

        log("\n▶ 第二轮：验证幂等（同名同大小应全部跳过，不重复提交）")
        job2 = migration_jobs.MigrationJob(id="probe2", params=params)
        await run_migration(job2)
        log(f"  结果：成功 {job2.success} / 跳过 {job2.skipped_existing} / "
            f"失败 {job2.failed}")
        idempotent = job2.success == 0 and job2.skipped_existing == len(entries)
        log("  ✅ 幂等：全部跳过，未重复提交" if idempotent else "  ❌ 幂等异常")
    finally:
        log("\n▶ 清理（fs_delete → 回收站）")
        try:
            for sub in await tgt.list_subdirs(dir_id):
                await tgt.client.fs_delete([sub.remote_id])
            entries = await tgt.list_entries(dir_id)
            ids = [v[1] for v in entries.values()]
            if ids:
                tgt.client.fs_delete(ids)
            tgt.client.fs_delete([dir_id])
            log("  已清理（进回收站）")
        except Exception as exc:
            log(f"  ⚠️ 清理异常（请人工检查 {tmp_name}）：{exc}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "probe"
    if mode == "engine":
        asyncio.run(run_engine_mode())
    else:
        asyncio.run(main())
