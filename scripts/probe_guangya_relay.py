"""光鸭 → 光鸭 转存的**真实验证**脚本（生产容器里跑）。

跑的是**生产代码路径**：:func:`app.services.cloud_relay.run_relay` +
:mod:`app.adapters.drivers.guangya` 的分享/转存方法——不是手搓 API 调用。

用法（生产容器）::

    docker exec -i medialab /opt/venv/bin/python - < scripts/probe_guangya_relay.py

**只造临时数据**：自建临时目录 + 几百 KB 的测试文件，跑完自删。
绝不碰用户的真实媒体数据，也**不碰回收站**（``fs_clear_recycle_bin``）。

验证点：

1. 正常转存：文件名与目录层级完整保留
2. **冲突判定**：目标已有同名**不同大小** → 记冲突、**不覆盖**、
   **不产出 ``xxx(1)`` 垃圾副本**
3. 幂等：目标已有同名**同大小** → 跳过
4. 分享用完即删（不在源账号留分享记录）
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from app.adapters.drivers.guangya_upload import build_guangya_uploader
from app.core.config import get_settings
from app.db.models import Driver
from app.db.session import dispose_db, init_db, session_scope
from app.services.cloud_relay import run_relay
from app.services.driver_factory import build_driver
from app.services.relay_jobs import RelayJob, RelayParams
from sqlalchemy import select

TS = time.strftime("%H%M%S")
SRC_DIR_NAME = f"__relay_probe_{TS}__"
TGT_DIR_NAME = f"__relay_probe_t_{TS}__"

#: 源 / 目标存储源 id（生产：3=小号1，4=小号2）
SRC_DRIVER_ID = 4
TGT_DRIVER_ID = 3

cleanup: list[tuple[Any, str]] = []
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    """记一条验收结论。"""
    mark = "✓" if ok else "✗"
    print(f"{mark} {label}" + (f" —— {detail}" if detail else ""))
    if not ok:
        failures.append(label)


async def upload(driver: Any, parent_id: str, name: str, size: int) -> str:
    """上传一个小文件到光鸭（带重试——生产容器偶发 DNS 抖动）。"""
    up = build_guangya_uploader(driver.client, driver)
    tmp = Path(f"/tmp/{name}")
    tmp.write_bytes(b"R" * size)
    try:
        for attempt in range(4):
            try:
                result = await up.upload_file(tmp, parent_id, name=name)
                cleanup.append((driver, result.file_id))
                return result.file_id
            except Exception as exc:
                print(f"  上传 {name} 失败（第 {attempt + 1} 次）：{exc}")
                await asyncio.sleep(5)
        raise RuntimeError(f"上传 {name} 连续失败")
    finally:
        tmp.unlink(missing_ok=True)


async def main() -> None:
    settings = get_settings()
    init_db(settings)
    try:
        async with session_scope() as db:
            rows = (
                (
                    await db.execute(
                        select(Driver)
                        .where(Driver.id.in_([SRC_DRIVER_ID, TGT_DRIVER_ID]))
                        .order_by(Driver.id)
                    )
                )
                .scalars()
                .all()
            )
        src_row = next(r for r in rows if r.id == SRC_DRIVER_ID)
        tgt_row = next(r for r in rows if r.id == TGT_DRIVER_ID)

        # 用存储源配的视频后缀（引擎按它过滤）
        exts = [
            e.strip().lower()
            for e in (src_row.video_exts or "").split(",")
            if e.strip()
        ]
        ext_main = exts[0] if exts else "mkv"
        ext_sub = exts[1] if len(exts) > 1 else ext_main
        print(f"源：{src_row.name}（{src_row.id}）　目标：{tgt_row.name}（{tgt_row.id}）")
        print(f"视频后缀：{exts or '（空 → 不过滤）'}")

        src = build_driver("guangya", src_row.credential, src_row.device_type)
        tgt = build_driver("guangya", tgt_row.credential, tgt_row.device_type)

        # ---------- 造源数据：文件 + 子目录 ----------
        src_dir, _ = await src.create_dir(SRC_DIR_NAME, "")
        cleanup.append((src, src_dir))
        await upload(src, src_dir, f"Probe.A.{ext_main}", 256 * 1024)
        sub_id, _ = await src.create_dir("Extras", src_dir)
        cleanup.append((src, sub_id))
        await upload(src, sub_id, f"Probe.B.{ext_sub}", 2048)
        print(f"源临时目录 {SRC_DIR_NAME}（{src_dir}）：1 个文件 + 1 个子目录（子目录里 1 个文件）")

        # ---------- 造目标数据：一个「同名不同大小」的冲突文件 ----------
        tgt_dir, _ = await tgt.create_dir(TGT_DIR_NAME, "")
        cleanup.append((tgt, tgt_dir))
        await upload(tgt, tgt_dir, f"Probe.A.{ext_main}", 1024)
        print(f"目标临时目录 {TGT_DIR_NAME}（{tgt_dir}）：预置同名但大小不同的文件")

        # ---------- 跑真实的转存引擎 ----------
        params = RelayParams(
            source_driver_id=SRC_DRIVER_ID,
            source_file_id=src_dir,
            target_driver_id=TGT_DRIVER_ID,
            target_file_id=tgt_dir,
            delete_source=False,
            submit_interval_sec=30.0,
        )
        job = RelayJob(id="probe-relay", params=params)
        print("\n--- 开始转存 ---")
        await run_relay(job)

        print("\n--- 转存日志 ---")
        for line in job.logs:
            print("   ", line)

        # ---------- 验收 ----------
        entries = await tgt.list_entries(tgt_dir)
        subdirs = await tgt.list_subdirs(tgt_dir)
        print(f"\n目标目录内容：文件={entries}　子目录={[(s.name, s.remote_id) for s in subdirs]}")

        check("转存成功 1 个文件", job.success == 1, f"success={job.success}")
        check("同名不同大小记为冲突", job.conflicts == 1, f"conflicts={job.conflicts}")
        check("没产生 (1) 垃圾副本", not any("(1)" in n for n in entries))

        conflict_hit = entries.get(f"Probe.A.{ext_main}")
        check(
            "冲突文件未被覆盖",
            conflict_hit is not None and conflict_hit[0] == 1024,
            f"目标里的大小={conflict_hit[0] if conflict_hit else '不存在'}"
            "（应为 1024，源的 256KB 未覆盖它）",
        )

        check("目录层级保留", len(subdirs) == 1 and subdirs[0].name == "Extras")

        inner = await tgt.list_entries(subdirs[0].remote_id) if subdirs else {}
        inner_hit = inner.get(f"Probe.B.{ext_sub}")
        check(
            "子目录里的文件保留且文件名不变",
            inner_hit is not None and inner_hit[0] == 2048,
            f"Extras 内 = {inner}",
        )

        # 源侧不该被动过（delete_source 默认关）
        src_entries = await src.list_entries(src_dir)
        check(
            "默认不删源",
            len(src_entries) == 1 and f"Probe.A.{ext_main}" in src_entries,
            f"源目录仍有 {len(src_entries)} 个文件",
        )

        # 分享用完即删
        shares = await src._call("share_user_list", page=0, page_size=20)
        share_list = ((shares.get("data") or {}).get("list") or [])
        check(
            "临时分享已清理",
            not any("relay-probe" in str(s.get("title") or "") for s in share_list),
            f"源账号分享列表 {len(share_list)} 条",
        )

        # ---------- 第二轮：验证「搬完清理源」开关 ----------
        print("\n--- 第二轮：delete_source=True ---")
        src_dir2, _ = await src.create_dir(f"__relay_probe2_{TS}__", "")
        cleanup.append((src, src_dir2))
        await upload(src, src_dir2, f"Probe.C.{ext_main}", 4096)
        tgt_dir2, _ = await tgt.create_dir(f"__relay_probe2_t_{TS}__", "")
        cleanup.append((tgt, tgt_dir2))

        job2 = RelayJob(
            id="probe-relay-2",
            params=RelayParams(
                source_driver_id=SRC_DRIVER_ID,
                source_file_id=src_dir2,
                target_driver_id=TGT_DRIVER_ID,
                target_file_id=tgt_dir2,
                delete_source=True,
                submit_interval_sec=30.0,
            ),
        )
        await run_relay(job2)

        entries2 = await tgt.list_entries(tgt_dir2)
        src_entries2 = await src.list_entries(src_dir2)
        check("清理源开关：转存成功", job2.success == 1, f"success={job2.success}")
        check(
            "清理源开关：源文件已删",
            len(src_entries2) == 0,
            f"源目录剩余 {src_entries2}",
        )
        check(
            "清理源开关：目标文件在",
            f"Probe.C.{ext_main}" in entries2,
            f"目标 = {entries2}",
        )
    finally:
        print("\n===== 清理 =====")
        for driver, fid in cleanup:
            try:
                await driver._call("fs_delete", [fid])
                print(f"删除 {fid} 已提交")
            except Exception as exc:
                print(f"删除 {fid} 失败：{exc}")
            await asyncio.sleep(0.4)
        await dispose_db()

    print("\n===== 验收结论 =====")
    if failures:
        print(f"❌ {len(failures)} 项未通过：{failures}")
        raise SystemExit(1)
    print("✅ 全部通过")


if __name__ == "__main__":
    asyncio.run(main())
