"""按「直链清单」批量提交云下载（清单 → 目标光鸭账号）。

**为什么需要**：云添加额度按**目标账号**计（实测 3 次/天，免费档），
而**取直链不吃流量、云下载也不吃流量**——所以可以先把直链批量取好存着，
等额度窗口（或换 VIP 目标账号）时再集中提交。

清单由 :mod:`scripts.fetch_guangya_links` 生成，字段见其 docstring。

跑法（生产容器，凭据不出容器）::

    ssh unraid "docker exec -i medialab /opt/venv/bin/python -" \\
        < scripts/submit_guangya_links.py

环境变量：

- ``MEDIALAB_SUBMIT_MANIFEST`` 清单路径（必填）
- ``MEDIALAB_SUBMIT_TARGET``   目标驱动 id（默认 2 = 光鸭盘，VIP 额度足）
- ``MEDIALAB_SUBMIT_ROOT``     源根前缀（清单里 ``rel_path`` 的公共前缀，
  去掉它得到目标下的相对目录；留空 = 保留整条路径）
- ``MEDIALAB_SUBMIT_TARGET_DIR`` 目标目录 id（默认 = 目标账号根）
- ``MEDIALAB_SUBMIT_LIMIT``    最多提交多少个（0 = 不限）
- ``MEDIALAB_SUBMIT_DRYRUN``   1 = 只打印计划不提交

安全边界：只写**目标**账号（源账号不动）；临时文件不清理——这是真迁移；
额度耗尽即停（不硬撞）；已存在同名同大小的文件跳过（幂等）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.environ.get("MEDIALAB_APP_PATH", "/app"))

from app.core.config import get_settings
from app.db.models import Driver
from app.db.session import init_db, session_scope
from app.services.driver_factory import build_driver
from app.services.migration import persist_guangya_credential
from sqlalchemy import select

MANIFEST = os.environ.get("MEDIALAB_SUBMIT_MANIFEST", "")
TARGET_ID = int(os.environ.get("MEDIALAB_SUBMIT_TARGET", "2"))
ROOT_PREFIX = os.environ.get("MEDIALAB_SUBMIT_ROOT", "")
TARGET_DIR = os.environ.get("MEDIALAB_SUBMIT_TARGET_DIR", "")
LIMIT = int(os.environ.get("MEDIALAB_SUBMIT_LIMIT", "0"))
DRYRUN = os.environ.get("MEDIALAB_SUBMIT_DRYRUN", "") == "1"

QUOTA_MARKERS = ("次数超限", "额度", "配额", "每日上限")


def log(msg: str) -> None:
    print(msg, flush=True)


def make_callback(driver_id: int):
    async def _cb(cred) -> None:
        await persist_guangya_credential(driver_id, cred)

    return _cb


def rel_dir_of(record: dict) -> str:
    """清单里的 ``rel_path`` → 目标下的相对目录（去掉源根前缀）。"""
    path = str(record.get("rel_path") or "").strip("/")
    prefix = ROOT_PREFIX.strip("/")
    if prefix and path.startswith(prefix + "/"):
        path = path[len(prefix) + 1 :]
    elif prefix and path == prefix:
        path = ""
    return path


async def ensure_dir(driver, rel_dir: str, root_id: str) -> str:
    """逐层建目录（幂等：同名已存在就复用）。"""
    current = root_id
    for part in [p for p in rel_dir.split("/") if p]:
        current, _created = await driver.create_dir(part, current)
    return current


async def wait_task(driver, task_id: str, wait_secs: float = 600.0) -> None:
    """等任务从活跃列表消失（连续 2 次缺席确认）。"""
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
    if not MANIFEST:
        raise SystemExit("❌ 缺 MEDIALAB_SUBMIT_MANIFEST（清单路径）")
    manifest_path = Path(MANIFEST)
    if not manifest_path.exists():
        raise SystemExit(f"❌ 清单不存在：{manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = list(manifest.get("links") or [])
    log(f"清单：{manifest_path.name}  账号={manifest.get('driver_name')}  "
        f"链接 {len(records)} 条")

    settings = get_settings()
    init_db(settings)
    async with session_scope() as db:
        row = (
            await db.execute(select(Driver).where(Driver.id == TARGET_ID))
        ).scalar_one_or_none()
    if row is None:
        raise SystemExit(f"❌ 目标驱动不存在：{TARGET_ID}")

    tgt = build_driver(
        row.type, row.credential, row.device_type,
        on_token_refresh=make_callback(row.id),
    )
    log(f"目标：驱动 {row.id}（{row.name}）")

    # 过期的链接先剔除（urlDuration 6 小时）
    now = int(time.time())
    live = [r for r in records if not r.get("expires_at") or r["expires_at"] > now]
    expired = len(records) - len(live)
    if expired:
        log(f"⚠️ 已过期链接 {expired} 条（6 小时有效期）——本次跳过")
    if LIMIT and len(live) > LIMIT:
        live = live[:LIMIT]
    total_gb = sum(int(r.get("fileSize") or 0) for r in live) / 1024**3
    log(f"待提交 {len(live)} 条 / {total_gb:.1f} GB")
    if DRYRUN:
        for r in live[:10]:
            log(f"  [dry-run] {rel_dir_of(r)}/{r['fileName']}")
        return

    root_id = TARGET_DIR
    if not root_id:
        root_id, _created = await tgt.create_dir(
            f"__relay_batch_{int(time.time())}__", ""
        )
        log(f"目标根目录：{root_id}（新建）")

    done = skipped = failed = 0
    for index, record in enumerate(live, start=1):
        name = str(record.get("fileName") or "")
        size = int(record.get("fileSize") or 0)
        rel_dir = rel_dir_of(record)
        target_dir = await ensure_dir(tgt, rel_dir, root_id)

        # 幂等：同名同大小已存在 → 跳过
        existing = await tgt.list_files(target_dir)
        if existing.get(name) == size:
            skipped += 1
            log(f"⏭️ 已存在：{rel_dir}/{name}")
            continue

        try:
            task_id = await tgt.create_offline_task(str(record["url"]), target_dir)
        except Exception as exc:
            text = str(exc)
            if any(m in text for m in QUOTA_MARKERS):
                log(f"⏸️ 额度耗尽，停止提交：{text}")
                log(f"   已完成 {done} / 跳过 {skipped} / 失败 {failed}；"
                    "剩余链接清单仍在（次日可续）")
                break
            failed += 1
            log(f"❌ 提交失败 {rel_dir}/{name}：{exc}")
            continue

        await wait_task(tgt, task_id)

        # 落盘名 = URL 末段（光鸭 CDN 直链末段 = download）→ 改名回原名
        entries = await tgt.list_entries(target_dir)
        landed = None
        for landed_name, (esize, fid) in entries.items():
            if esize != size:
                continue
            if landed_name == name:
                landed = fid
                break
            if landed_name.startswith("download"):
                try:
                    await tgt.rename_file(fid, name)
                    landed = fid
                except Exception as exc:
                    log(f"⚠️ 改名失败 {name}：{exc}")
                break
        if landed is None:
            failed += 1
            log(f"❌ 落盘未出现同名同大小：{rel_dir}/{name}")
            continue

        done += 1
        if index % 10 == 0 or index == len(live):
            log(f"  进度 {index}/{len(live)}  成功 {done}  跳过 {skipped}  失败 {failed}  "
                f"耗时 {int(time.time() - t0)}s")

    log(f"\n✅ 提交完成：成功 {done} / 跳过 {skipped} / 失败 {failed}")


if __name__ == "__main__":
    t0 = time.time()
    asyncio.run(main())
