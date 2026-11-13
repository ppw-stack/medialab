"""批量获取光鸭账号全部视频文件的直链，并落盘备份。

**为什么需要**：光鸭「云添加」额度有限（实测 3 次/账号/天，免费档），
而直链本身可以先拿好、存着，等额度恢复（或换目标账号）时再批量提交。

⚠️ **直链有有效期**：``urlDuration=21600``（6 小时），过期后链接失效，
需重新生成。清单里记了 ``ts``（签发时刻）与 ``expires_at``，按需重取。

用法（生产容器跑，凭据不出容器）::

    ssh unraid "docker exec -i medialab /opt/venv/bin/python -" \\
        < scripts/fetch_guangya_links.py

环境变量：

- ``MEDIALAB_LINK_DRIVER`` 源驱动 id（默认 4 = 小号2）
- ``MEDIALAB_LINK_OUT``     输出 JSON 路径（默认打印到 stdout）
- ``MEDIALAB_LINK_INTERVAL`` 两次取链之间的间隔秒（默认 0.4）
- ``MEDIALAB_LINK_LIMIT``    最多取多少条（0 = 不限）

安全边界：只读（取链不传数据）；token 刷新走 app 自己的 ``store_credential``
写回；不碰任何文件数据。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.environ.get("MEDIALAB_APP_PATH", "/app"))

from app.core.config import get_settings
from app.db.models import Driver
from app.db.session import init_db, session_scope
from app.services.driver_factory import build_driver
from app.services.migration import persist_guangya_credential
from sqlalchemy import select

DRIVER_ID = int(os.environ.get("MEDIALAB_LINK_DRIVER", "4"))
INTERVAL = float(os.environ.get("MEDIALAB_LINK_INTERVAL", "0.4"))
LIMIT = int(os.environ.get("MEDIALAB_LINK_LIMIT", "0"))
OUT_PATH = os.environ.get("MEDIALAB_LINK_OUT", "")

VIDEO_EXTS = (
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv",
    ".m4v", ".ts", ".m2ts", ".rmvb", ".rm", ".webm", ".iso",
)


def log(msg: str) -> None:
    print(msg, flush=True)


def make_callback(driver_id: int):
    async def _cb(cred) -> None:
        await persist_guangya_credential(driver_id, cred)

    return _cb


async def traffic_snapshot(driver) -> str:
    """读一次直链流量计数器（用于判断「取链」是否计费）。"""
    import httpx
    from app.adapters.drivers.guangya import _client_id

    try:
        await driver._ensure_token()
        cred = driver._cred
        headers = {
            "Authorization": f"Bearer {driver.client.token}",
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
        data = resp.json().get("data") or {}
        if "highSpeedTraffic" not in data:
            return "未知（字段缺失）"
        hst = data.get("highSpeedTraffic")
        if hst is None:
            return "不限（VIP）"
        details = hst.get("details") or []
        used = sum(int(d.get("used") or 0) for d in details)
        return f"used={used} remained={hst.get('remained')} total={hst.get('total')}"
    except Exception as exc:
        return f"读取失败：{exc}"


async def list_videos(driver) -> list[dict]:
    """全账号视频文件（res_type=1 翻页 + 后缀过滤）。"""
    out: list[dict] = []
    page = 0
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
        for item in batch:
            name = str(item.get("fileName") or "")
            if name.lower().endswith(VIDEO_EXTS):
                out.append(item)
        page += 1
        if page >= 100:
            break
        await asyncio.sleep(0.2)
    return out


def link_expiry(url: str) -> int:
    """从直链 URL 的 ``ts`` 参数推算过期时刻（签发时刻 + 21600s）。"""
    try:
        ts = int((parse_qs(urlparse(url).query).get("ts") or ["0"])[0])
    except (TypeError, ValueError):
        return 0
    return ts + 21600 if ts else 0


async def list_dirs(driver) -> dict[str, str]:
    """全账号目录名映射（fileId → 目录名）。

    **目录结构必须一起备份**：光鸭接口不返回路径字符串，只有
    ``fullParentIds`` 的 id 链 + 这份名字映射才能还原出每个文件的位置。
    没有它，链接拿回来也不知道该落到目标目录的哪一层。
    """
    names: dict[str, str] = {}
    page = 0
    while True:
        payload = await asyncio.to_thread(
            driver.client.fs_files,
            parent_id="*",
            page=page,
            page_size=1000,
            res_type=2,
        )
        batch = list((payload.get("data") or {}).get("list") or [])
        if not batch:
            break
        for item in batch:
            file_id = str(item.get("fileId") or "")
            if file_id:
                names[file_id] = str(item.get("fileName") or "")
        page += 1
        if page >= 100:
            break
        await asyncio.sleep(0.2)
    return names


def chain_path(chain: list[str], dir_names: dict[str, str]) -> str:
    """id 链 → 路径字符串（缺名的 id 用 fileId 占位，路径仍可复现）。"""
    return "/".join(dir_names.get(seg, seg) for seg in chain)


async def main() -> None:
    settings = get_settings()
    init_db(settings)

    async with session_scope() as db:
        row = (
            await db.execute(select(Driver).where(Driver.id == DRIVER_ID))
        ).scalar_one_or_none()
    if row is None:
        raise SystemExit(f"❌ 驱动不存在：{DRIVER_ID}")

    driver = build_driver(
        row.type, row.credential, row.device_type,
        on_token_refresh=make_callback(row.id),
    )
    log(f"账号：驱动 {row.id}（{row.name}） media_cid={row.media_cid!r}")

    before = await traffic_snapshot(driver)
    log(f"取链前 traffic：{before}")

    dir_names = await list_dirs(driver)
    log(f"目录 {len(dir_names)} 个（用于还原路径结构）")

    videos = await list_videos(driver)
    log(f"视频文件 {len(videos)} 个，开始取链（间隔 {INTERVAL}s）")

    records: list[dict] = []
    failed: list[dict] = []
    out_file = Path(OUT_PATH) if OUT_PATH else None
    t0 = time.time()

    for index, item in enumerate(videos, start=1):
        if LIMIT and index > LIMIT:
            break
        file_id = str(item.get("fileId") or "")
        name = str(item.get("fileName") or "")
        chain = [seg for seg in str(item.get("fullParentIds") or "").split("/") if seg]
        url = ""
        for attempt in range(3):
            try:
                link = await driver.get_direct_link(file_id, "")
                url = link.url
                break
            except Exception as exc:
                wait = 5 * (attempt + 1)
                log(f"  ⚠️ 取链失败（第 {attempt + 1} 次）{name[:30]}：{exc}（{wait}s 后重试）")
                await asyncio.sleep(wait)
        if not url:
            failed.append({"fileId": file_id, "fileName": name, "reason": "取链失败"})
            continue
        records.append(
            {
                "fileId": file_id,
                "fileName": name,
                "fileSize": int(item.get("fileSize") or 0),
                "ext": item.get("ext"),
                "parentId": str(item.get("parentId") or ""),
                #: 相对**账号根**的目录路径（链首是账号根的直接子目录）
                "rel_path": chain_path(chain, dir_names),
                #: fullParentIds 原样（id 链）——换源目录时用它重算相对路径
                "chain": chain,
                "url": url,
                "issued_at": int(time.time()),
                "expires_at": link_expiry(url),
            }
        )
        if index % 20 == 0 or index == len(videos):
            log(f"  进度 {index}/{len(videos)}  已取 {len(records)}  失败 {len(failed)}  "
                f"耗时 {int(time.time() - t0)}s")
        # 增量落盘——中断不丢已取到的链接
        if out_file is not None:
            out_file.write_text(
                json.dumps(
                    {
                        "driver_id": row.id,
                        "driver_name": row.name,
                        "media_cid": row.media_cid,
                        "generated_at": int(time.time()),
                        "dir_names": dir_names,
                        "links": records,
                        "failed": failed,
                    },
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
        await asyncio.sleep(INTERVAL)

    after = await traffic_snapshot(driver)
    log(f"\n取链后 traffic：{after}")
    log(f"取链前 traffic：{before}")

    expiry = min((r["expires_at"] for r in records if r["expires_at"]), default=0)
    dirs = len({r["rel_path"] for r in records})
    log(f"\n✅ 取链完成：成功 {len(records)} / 失败 {len(failed)}  "
        f"涉及目录 {dirs} 个  耗时 {int(time.time() - t0)}s")
    if expiry:
        log(f"⏰ 最早一条链接将于 {time.strftime('%m-%d %H:%M', time.localtime(expiry))} 过期"
            "（urlDuration=21600s = 6 小时）")
    if out_file is not None:
        log(f"📄 清单已保存：{out_file}（{out_file.stat().st_size / 1024:.0f} KB）")
    else:
        log("📄 未指定输出路径（MEDIALAB_LINK_OUT）——只打印统计")


if __name__ == "__main__":
    asyncio.run(main())
