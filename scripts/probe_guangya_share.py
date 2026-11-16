"""光鸭「分享 → 转存」探针（小文件归拢通道的可行性验证）。

回答四个问题（都只能在生产实测，文档里没有）：

1. **分享能不能建出来**——尤其是**小文件**（nfo/jpg）：AGENTS.md 记过
   ``shareAuditStatus=3``（分享审核拒绝）的文件**分享不出去**，但直链+云下载
   完全可用。附件类文件是否能分享，是小文件通道成立的前提。
2. **转存能不能落到指定目录**——``share_restore(access_token, file_ids, parent_id)``
   的 ``parent_id`` 若有效，就不必再把「来自：分享」里的东西挪一次。
3. **转存消不消耗云添加额度**——转存是服务端内部复制（与 URL 离线下载是
   不同的接口），预期**不消耗**；用账号信息里的计数器前后对比取证。
4. **转存后的文件是否保持「同名同大小」**——归拢的校验与「清理副盘副本」
   都依赖这个性质。

跑法（生产容器，凭据不出容器）::

    ssh unraid "docker exec -i medialab /opt/venv/bin/python -" \\
        < scripts/probe_guangya_share.py

安全边界：

- **不动副盘的原始文件**（只读它来建分享）——探针只删**主盘上自己建的**
  探针目录与其中的副本
- 删除走 ``fs_delete``（进回收站），**绝不**碰回收站清空接口
- 分享用完即删（``share_delete``），不留残余
- 最多消耗 **1 次**云添加额度（仅当回退路径被触发时）
"""

from __future__ import annotations

import asyncio
import json
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

#: 源（副盘）：小号2——它的「影视」下有大量 nfo/jpg 小文件
SOURCE_DRIVER_ID = int(os.environ.get("MEDIALAB_PROBE_SOURCE", "4"))
#: 目标（主盘）：光鸭盘
TARGET_DRIVER_ID = int(os.environ.get("MEDIALAB_PROBE_TARGET", "2"))
#: 主盘上的归拢落点（组 1 的 target_dir_id）
TARGET_ROOT_ID = os.environ.get("MEDIALAB_PROBE_TARGET_ROOT", "1962124234139361322")
#: 副盘上的取样目录（小号2 的「影视」）
SOURCE_ROOT_ID = os.environ.get("MEDIALAB_PROBE_SOURCE_ROOT", "1965459561881387015")

PROBE_DIR_NAME = f"归拢探针-{int(time.time())}"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _client(driver_row: Driver) -> object:
    """构造光鸭**适配器**（带 token 刷新写回）。

    分享/转存 API 在底层 ``guangyaclient`` 上（适配器没封装）→ 用
    ``driver.client`` 取。适配器的 ``client`` 属性是同步的（凭据已在
    构造时解好），token 过期由 ``_call`` 内部刷新。
    """
    return build_driver(
        "guangya",
        driver_row.credential,
        driver_row.device_type,
        on_token_refresh=lambda cred: persist_guangya_credential(
            driver_row.id, cred
        ),
    )


def _share_url_id(data: dict[str, object]) -> str:
    """从 share_create 的返回里拼出「URL 形态」的分享 id。

    实测（2026-11-16）：公开接口（share_summary / share_access_token）
    只认 ``{shareId}_{账号分享key}``，裸 shareId → 112 参数错误。
    key 从 shareUrl 末段取（``.../s/{shareId}_{key}``）。
    """
    share_id = str(data.get("shareId") or "")
    url = str(data.get("shareUrl") or "")
    tail = url.rsplit("/", 1)[-1]
    if "_" in tail:
        return tail  # 已经是 URL 形态
    return share_id


def _api(driver: object) -> object:
    """取底层 guangyaclient（分享/转存/账号信息都在它上面）。"""
    client = getattr(driver, "client", None)
    if client is None:
        raise RuntimeError("适配器没有 client 属性")
    return client


async def main() -> int:
    init_db(get_settings())
    async with session_scope() as db:
        rows = (
            (
                await db.execute(
                    select(Driver).where(
                        Driver.id.in_([SOURCE_DRIVER_ID, TARGET_DRIVER_ID])
                    )
                )
            )
            .scalars()
            .all()
        )
    by_id = {r.id: r for r in rows}
    src_row = by_id.get(SOURCE_DRIVER_ID)
    dst_row = by_id.get(TARGET_DRIVER_ID)
    if src_row is None or dst_row is None:
        log("❌ 存储源不存在")
        return 1

    src = _client(src_row)
    dst = _client(dst_row)

    # ---------- ① 账号信息（额度计数器取证用）----------
    def _counters(client: object) -> dict[str, object]:
        try:
            info = _api(client).user_info()
        except Exception as exc:
            return {"_err": str(exc)}
        data = (info or {}).get("data") or {}
        # 只留「像计数器」的字段（次数/额度/流量）
        return {
            k: v
            for k, v in data.items()
            if isinstance(v, (int, str)) and any(
                s in k.lower() for s in ("count", "times", "quota", "limit", "traffic", "use")
            )
        }

    before = _counters(src)
    log(f"① 源账号计数器（转存前）：{before}")

    # ---------- ② 找一个小的附件类文件 ----------
    sample = None
    async for entry in src.iter_tree_by_cid(  # type: ignore[attr-defined]
        SOURCE_ROOT_ID, parent_path="", include_subdirs=True
    ):
        if entry.is_dir:
            continue
        if entry.name.lower().endswith((".nfo", ".jpg", ".png")):
            sample = entry
            break
    if sample is None:
        log("❌ 副盘上没找到 nfo/jpg 小文件")
        return 1
    log(
        f"② 取样：{sample.relative_dir}/{sample.name} "
        f"（{sample.size} 字节，fileId={sample.remote_id}）"
    )

    share_id = ""
    probe_dir_id = ""
    restored_name = ""
    try:
        # ---------- ③ 建分享 ----------
        resp = _api(src).share_create(
            [sample.remote_id],
            title=f"聚合探针 {PROBE_DIR_NAME}",
            validate_duration=0,  # 永久
            code="",
            auto_fill_code=False,  # 不要提取码（转存要带码就麻烦了）
            traffic_limit="0",
            max_restore_count=0,  # 转存次数不限
        )
        log(f"③ share_create 返回：{json.dumps(resp, ensure_ascii=False)[:400]}")
        data = resp.get("data") or {}
        share_id = str(data.get("shareId") or data.get("id") or "")
        code = str(data.get("code") or "")
        if not share_id:
            log("❌ 分享没返回 shareId")
            return 1

        # ---------- ④ 分享内容（公开接口，无需登录）----------
        # ⚠️ **公开接口要的是「URL 形态」的分享 id**：``{shareId}_{账号分享key}``
        #   ——裸 shareId 一律 112「参数错误」（2026-11-16 实测）。
        #   key 从 share_create 返回的 shareUrl 末段取（两次分享实测同值，
        #   是账号的固定分享 key）；也可用 share_summary 的 userId 字段。
        url_id = _share_url_id(data)
        summary = _api(src).share_summary(url_id)
        log(
            "④ share_summary（URL 形态 id="
            f"{url_id}）：{json.dumps(summary, ensure_ascii=False)[:300]}"
        )
        token_resp = _api(src).share_access_token(url_id, code)
        token_data = token_resp.get("data") or {}
        if isinstance(token_data, str):
            access_token = token_data
        else:
            access_token = str(
                token_data.get("accessToken")
                or token_data.get("token")
                or token_data.get("access_token")
                or ""
            )
        log(f"   access_token 长度={len(access_token)}")
        if not access_token:
            log("❌ 没拿到 access_token")
            return 1
        listing = _api(src).share_files_list(access_token, "", 1, 50)
        log(f"   分享内容：{json.dumps(listing, ensure_ascii=False)[:300]}")

        # ---------- ⑤ 主盘建探针目录 ----------
        probe_dir_id, created = await dst.create_dir(  # type: ignore[attr-defined]
            PROBE_DIR_NAME, TARGET_ROOT_ID
        )
        log(f"⑤ 主盘探针目录：{PROBE_DIR_NAME}（id={probe_dir_id} 新建={created}）")

        # ---------- ⑥ 转存到指定目录 ----------
        restore = _api(dst).share_restore(
            access_token, [sample.remote_id], probe_dir_id
        )
        log(f"⑥ share_restore：{json.dumps(restore, ensure_ascii=False)[:400]}")

        # ---------- ⑦ 校验落盘（同名同大小）----------
        ok = False
        for _ in range(10):
            await asyncio.sleep(3)
            entries = await dst.list_entries(probe_dir_id)  # type: ignore[attr-defined]
            log(f"   探针目录内容：{[(n, s) for n, (s, _f) in entries.items()]}")
            hit = entries.get(sample.name)
            if hit is not None and hit[0] == sample.size:
                ok = True
                restored_name = sample.name
                break
        log(f"⑦ 校验：{'✅ 同名同大小' if ok else '❌ 未见同名同大小文件'}")

        after = _counters(src)
        log(f"⑧ 源账号计数器（转存后）：{after}")
        diff = {
            k: (before.get(k), after.get(k))
            for k in set(before) | set(after)
            if before.get(k) != after.get(k)
        }
        log(f"   前后差异：{diff}")
    finally:
        # ---------- 清理 ----------
        if probe_dir_id:
            try:
                entries = await dst.list_entries(probe_dir_id)  # type: ignore[attr-defined]
                for _name, (_size, fid) in entries.items():
                    await dst.delete_file(fid)  # type: ignore[attr-defined]
                await dst.delete_file(probe_dir_id)  # type: ignore[attr-defined]
                log(f"🧹 已清理主盘探针目录（{PROBE_DIR_NAME}）")
            except Exception as exc:
                log(f"⚠️ 清理探针目录失败：{exc}")
        if share_id:
            try:
                _api(dst).share_delete([share_id])
                log(f"🧹 已删除分享 {share_id}")
            except Exception as exc:
                log(f"⚠️ 删除分享失败：{exc}")

    log(f"结论：分享转存通道 {'✅ 可行' if restored_name else '❌ 未跑通'}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
