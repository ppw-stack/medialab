"""「分享 → 转存」通道的**本地**真实链路探针（用生产凭据）。

与 :mod:`scripts.probe_guangya_share` 的区别：那个在生产容器里跑、直接调
底层 ``guangyaclient``；这个在**本地**跑、用**我们的驱动实现**
（``GuangyaDriver.create_share`` / ``share_access_token`` / ``restore_share``）
——目的是验证**实现本身**能不能跑通真实 API（探针只验证了底层 API 可用）。

凭据来自本地 ``.env.probe``（**gitignored**），由生产容器解密导出：

    ssh unraid 'docker exec -i medialab /opt/venv/bin/python -' > .env.probe

跑法::

    uv run python scripts/probe_guangya_share_local.py

安全边界：

- **只读**副盘的样本文件（建分享不动原文件）
- 主盘只建**探针目录**、自建自删；删除走 ``fs_delete``（进回收站），
  **绝不**碰回收站清空接口
- 分享用完即删
- **不消耗云添加额度**（转存与云下载是不同接口）——但仍会消耗**直链流量**
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from app.adapters.drivers.guangya import GuangyaDriver  # noqa: E402

#: 凭据文件（**必须**在 .gitignore 里——.env.* 已覆盖）
CREDS_FILE = Path(os.environ.get("MEDIALAB_PROBE_CREDS", REPO / ".env.probe"))
#: 副盘（小号2）的取样目录 —— 与生产组 1 的缓冲根一致
SOURCE_ROOT = os.environ.get("MEDIALAB_PROBE_SOURCE_ROOT", "1965459561881387015")
#: 主盘的归拢落点（组 1 的 target_dir_id）
TARGET_ROOT = os.environ.get("MEDIALAB_PROBE_TARGET_ROOT", "1962124234139361322")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _driver(entry: dict[str, Any]) -> GuangyaDriver:
    """按导出的**明文凭据 JSON**构造驱动。

    驱动的 ``credential`` 参数接受「密文或明文 JSON」——这里直接给明文
    （本地不注册 token 刷新回调：刷新结果只留在内存，不写回任何库）。
    """
    return GuangyaDriver(entry["credential"], entry.get("device_type"))


async def main() -> int:
    if not CREDS_FILE.exists():
        log(f"❌ 找不到凭据文件 {CREDS_FILE}")
        return 1
    creds = json.loads(CREDS_FILE.read_text(encoding="utf-8"))
    source = _driver(creds["4"])  # 小号2（副盘）
    target = _driver(creds["2"])  # 光鸭盘（主盘）

    # ---------- 取样：一个小的附件文件 ----------
    sample = None
    async for entry in source.iter_tree_by_cid(
        SOURCE_ROOT, parent_path="", include_subdirs=True
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
        f"① 取样：{sample.relative_dir}/{sample.name}"
        f"（{sample.size} 字节，fileId={sample.remote_id}）"
    )

    probe_dir_name = f"归拢探针-{int(time.time())}"
    probe_dir_id = ""
    share_id = ""
    try:
        # ---------- 建分享（我们的实现）----------
        share = await source.create_share([sample.remote_id], title=probe_dir_name)
        share_id = share.share_id
        log(f"② create_share：shareId={share.share_id} url_id={share.url_id}")
        log(f"   提取码={share.code!r}")

        # ---------- 取访问令牌（我们的实现）----------
        token = await source.share_access_token(share.url_id, share.code)
        log(f"③ access_token 长度={len(token)}")

        # ---------- 主盘建探针目录 ----------
        probe_dir_id, created = await target.create_dir(probe_dir_name, TARGET_ROOT)
        log(f"④ 主盘探针目录 id={probe_dir_id}（新建={created}）")

        # ---------- 转存（我们的实现）----------
        task_id = await target.restore_share(
            token, [sample.remote_id], probe_dir_id
        )
        log(f"⑤ restore_share → taskId={task_id}")

        # ---------- 等落地（轮询目录）----------
        ok = False
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            await asyncio.sleep(2)
            entries = await target.list_entries(probe_dir_id)
            hit = entries.get(sample.name)
            if hit is not None:
                log(f"   探针目录内容：{[(n, s) for n, (s, _f) in entries.items()]}")
                if hit[0] == sample.size:
                    ok = True
                    break
        log(f"⑥ 校验：{'✅ 同名同大小' if ok else '❌ 未见同名同大小文件'}")
        log(f"   （落地名是否保持原名：{sample.name!r}）")
        return 0 if ok else 1
    finally:
        # ---------- 清理 ----------
        if probe_dir_id:
            try:
                entries = await target.list_entries(probe_dir_id)
                for _n, (_s, fid) in entries.items():
                    await target.delete_file(fid)
                await target.delete_file(probe_dir_id)
                log(f"🧹 已清理主盘探针目录（{probe_dir_name}）")
            except Exception as exc:
                log(f"⚠️ 清理探针目录失败：{exc}")
        if share_id:
            try:
                await source.delete_share([share_id])
                log(f"🧹 已删除分享 {share_id}")
            except Exception as exc:
                log(f"⚠️ 删除分享失败：{exc}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
