"""光鸭扫描能力探针（一次性，跑在生产容器内）。

用法：
    ssh unraid "docker exec -i medialab /opt/venv/bin/python -" \
        < scripts/probe_guangya_scan.py

三个实验（对应「光鸭扫描能否优化」的调研）：

  A. ``fs_files`` 的 ``pageSize`` 服务端上限（只读）
  B. ``fs_files(parent_id="*")``「全部目录」模式的条目字段结构（只读）
     —— 判断能否用全量列表替代递归遍历（条目是否带父目录链/路径）
  C. 目录 ``utime`` 语义（写探针：在自建目录内建/改名/删子目录，
     逐步观察父目录 utime 是否变化——判断「目录 mtime 剪枝」可行性）

安全边界：
  - 只读实验不读不改任何业务数据（pageSize 探测的响应只取字段结构与计数）
  - 写实验只碰自建探针目录：名字带时间戳前缀下划线，删除前用
    ``fs_detail`` 校验名字匹配，结束必删并复核
  - **绝不调用** ``fs_clear_recycle_bin``（清空回收站会误删用户数据）；
    ``fs_delete`` 删除的探针目录留在回收站，无害
  - 全程走驱动的 ``_call``（自带 0.2s 限速 + token 刷新写回 DB）
"""

from __future__ import annotations

import asyncio
import json
import time

from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import Driver
from app.db.session import dispose_db, init_db, session_scope
from app.services.driver_factory import build_driver

PROBE_NAME = f"_scan_probe_tmp_{time.strftime('%Y%m%d%H%M')}"

#: 每个响应最多打印多少字符（防止刷屏）
DUMP_LIMIT = 2500


def dump(title: str, payload: object) -> None:
    print(f"\n===== {title} =====")
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if len(text) > DUMP_LIMIT:
        text = text[:DUMP_LIMIT] + f"\n...（截断，总长 {len(text)}）"
    print(text)


def field_names(item: dict) -> list[str]:
    """递归收集一条 item 的全部字段名（含嵌套 dict 的 key）。"""
    names: list[str] = []

    def walk(obj: object, prefix: str) -> None:
        if not isinstance(obj, dict):
            return
        for key, value in obj.items():
            names.append(f"{prefix}{key}")
            if isinstance(value, dict):
                walk(value, f"{prefix}{key}.")

    walk(item, "")
    return names


async def main() -> None:
    settings = get_settings()
    init_db(settings)

    try:
        async with session_scope() as db:
            row = (
                await db.execute(select(Driver).where(Driver.type == "guangya"))
            ).scalars().first()
        if row is None:
            print("没有 guangya 类型的存储源行，探针终止")
            return
        print(f"使用存储源：id={row.id} name={row.name} media_cid={row.media_cid}")

        from app.services.redirect import _persist_refreshed_credential

        async def _on_refresh(cred: object) -> None:
            await _persist_refreshed_credential(row.id, cred)

        driver = build_driver(
            row.type, row.credential, row.device_type, on_token_refresh=_on_refresh
        )

        # ---------- 实验 A + B：pageSize 上限 + "*" 模式字段结构 ----------
        print("\n########## 实验 A/B：fs_files(parent_id='*') ##########")
        sample_item: dict | None = None
        for size in (100, 200, 500, 1000):
            # 形态 1：与驱动扫描一致（file_types=[] 不筛选）
            try:
                payload = await driver._call(
                    "fs_files",
                    parent_id="*",
                    page=0,
                    page_size=size,
                    order_by=0,
                    sort_type=0,
                    file_types=[],
                )
            except Exception as exc:  # noqa: BLE001 —— 探针要看到一切异常
                print(f"page_size={size}（混合）异常：{type(exc).__name__}: {exc}")
                continue
            code = payload.get("code")
            data = payload.get("data") or {}
            items = data.get("list") or []
            print(
                f"page_size={size}（混合）: code={code} "
                f"返回条数={len(items)} total={data.get('total')} "
                f"msg={payload.get('msg')}"
            )
            if code not in (None, 0):
                continue
            if items and sample_item is None:
                sample_item = items[0]

        if sample_item is not None:
            print("\n--- 实验 B：条目字段名（首条，混合列表）---")
            print(json.dumps(field_names(sample_item), ensure_ascii=False))
            dump("实验 B：首条样例（完整结构）", sample_item)

            # 形态 2：视频列表（fs_video_list 等价）——看视频条目是否带父目录信息
            try:
                payload = await driver._call(
                    "fs_files",
                    parent_id="*",
                    page=0,
                    page_size=100,
                    order_by=3,
                    sort_type=1,
                    file_types=[2],
                    res_type=1,
                )
                data = payload.get("data") or {}
                vitems = data.get("list") or []
                print(
                    f"\npage_size=100（视频 file_types=[2] res_type=1）: "
                    f"code={payload.get('code')} 返回条数={len(vitems)} "
                    f"total={data.get('total')}"
                )
                if vitems:
                    print("--- 视频条目字段名（首条）---")
                    print(json.dumps(field_names(vitems[0]), ensure_ascii=False))
                    dump("实验 B：视频条目样例（完整结构）", vitems[0])
            except Exception as exc:  # noqa: BLE001
                print(f"视频列表探测异常：{type(exc).__name__}: {exc}")

        # fs_detail 的返回结构（实验 C 的 utime 探测依赖它，顺便摸清）
        # ---------- 实验 C：目录 utime 语义 ----------
        print("\n########## 实验 C：目录 utime 语义 ##########")
        print(f"探针目录名：{PROBE_NAME}")
        probe_id: str | None = None
        child_id: str | None = None
        timeline: list[tuple[str, object]] = []

        async def read_utime(tag: str) -> None:
            """读探针目录 utime 并记录（fs_detail，一次请求）。"""
            nonlocal probe_id
            if probe_id is None:
                return
            try:
                payload = await driver._call("fs_detail", probe_id)
            except Exception as exc:  # noqa: BLE001
                print(f"[{tag}] fs_detail 异常：{type(exc).__name__}: {exc}")
                timeline.append((tag, f"error: {exc}"))
                return
            data = payload.get("data") or {}
            keys = {}
            for key in data:
                low = key.lower()
                if any(m in low for m in ("time", "utime", "date")):
                    keys[key] = data.get(key)
            print(f"[{tag}] fs_detail 时间相关字段：{json.dumps(keys, ensure_ascii=False)}")
            dump(f"[{tag}] fs_detail 完整返回", payload)
            timeline.append((tag, keys))

        async def safe_delete(file_id: str, expect_name: str) -> bool:
            """删除前用 fs_detail 校验名字，防误删。返回是否已删除。"""
            try:
                detail = (await driver._call("fs_detail", file_id)).get("data") or {}
                name = str(detail.get("fileName") or detail.get("name") or "")
                if name and name != expect_name:
                    print(
                        f"⚠️ 拒绝删除：fileId={file_id} 名字不匹配 "
                        f"（期望 {expect_name}，实际 {name}）"
                    )
                    return False
                await driver._call("fs_delete", [file_id])
                return True
            except Exception as exc:  # noqa: BLE001
                print(f"删除 {expect_name}（{file_id}）异常：{type(exc).__name__}: {exc}")
                return False

        try:
            # ① 建探针目录（根目录下）
            payload = await driver._call(
                "fs_create_dir", PROBE_NAME, parent_id="", fail_if_name_exist=True
            )
            code = payload.get("code")
            probe_id = str((payload.get("data") or {}).get("fileId") or "")
            print(
                f"① 建探针目录：code={code} fileId={probe_id} "
                f"msg={payload.get('msg')}"
            )
            if code not in (None, 0, 159) or not probe_id:
                print("建探针目录失败，实验 C 终止")
                return
            await read_utime("① 初始")

            # ② 在探针目录内建子目录
            payload = await driver._call(
                "fs_create_dir", "child", parent_id=probe_id, fail_if_name_exist=True
            )
            child_id = str((payload.get("data") or {}).get("fileId") or "")
            print(f"② 建子目录：code={payload.get('code')} fileId={child_id}")
            await read_utime("② 建子目录后")

            # ③ 改名子目录
            if child_id:
                payload = await driver._call("fs_rename", child_id, "child2")
                print(f"③ 改名子目录：code={payload.get('code')} msg={payload.get('msg')}")
                await read_utime("③ 改名后")

            # ④ 删子目录
            if child_id:
                ok = await safe_delete(child_id, "child2")
                print(f"④ 删子目录：deleted={ok}")
                child_id = None if ok else child_id
                await read_utime("④ 删子目录后")
        finally:
            # 清理：删子目录（若有）→ 删探针目录 → 复核
            print("\n--- 清理 ---")
            if child_id:
                ok = await safe_delete(child_id, "child2")
                print(f"清理子目录：deleted={ok}")
            if probe_id:
                ok = await safe_delete(probe_id, PROBE_NAME)
                print(f"清理探针目录：deleted={ok}")
                # 复核：确认名字已不在（fs_detail 应报不存在）
                try:
                    detail = await driver._call("fs_detail", probe_id)
                    code = detail.get("code")
                    print(f"复核 fs_detail：code={code}（非 0 = 已不存在）")
                except Exception as exc:  # noqa: BLE001
                    print(f"复核 fs_detail 异常：{type(exc).__name__}: {exc}")

        print("\n--- utime 时间线汇总 ---")
        for tag, value in timeline:
            print(f"{tag}: {json.dumps(value, ensure_ascii=False)}")
    finally:
        await dispose_db()


if __name__ == "__main__":
    asyncio.run(main())
