"""读取客户端权益接口 + VOD 下载接口（判断客户端是否另有一条下载路）。

1. `get_user_rights`（客户端用于判断上传/下载限制）→ 打印非会员阈值
2. `get_vod_download_url(fileId, gcid)` → 非会员是否拿到转码流链接
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.environ.get("MEDIALAB_APP_PATH", "/app"))

import httpx
from app.core.config import get_settings
from app.db.models import Driver
from app.db.session import dispose_db, init_db, session_scope
from app.services.driver_factory import build_driver
from app.services.migration import persist_guangya_credential
from sqlalchemy import select

API = "https://api.guangyapan.com"
CLIENT_ID_MAC = "al9eLEtLE7_pp8GR"


def log(m: str) -> None:
    print(m, flush=True)


async def post(path: str, token: str, did: str, body: dict) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Client-Id": CLIENT_ID_MAC,
        "X-Device-Id": did,
        "did": did,
        "dt": "6",
        "av": "1.1.2",
        "vc": "1010",
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.guangyapan.com",
        "Referer": "https://www.guangyapan.com/",
    }
    r = await asyncio.to_thread(
        httpx.post, f"{API}{path}", json=body, headers=headers, timeout=25.0
    )
    try:
        return r.json()
    except Exception:
        return {"http": r.status_code, "raw": r.text[:300]}


async def main() -> int:
    init_db(get_settings())
    try:
        async with session_scope() as db:
            rows = (
                (await db.execute(select(Driver).where(Driver.id.in_([2, 5])))).scalars().all()
            )
        out = {}
        for row in rows:
            async def cb(cred, _id=row.id):
                await persist_guangya_credential(_id, cred)

            d = build_driver(row.type, row.credential, row.device_type, on_token_refresh=cb)
            await d._ensure_token()
            token = d.client.token
            did = getattr(d._cred, "device_id", "") or ""
            entry = {"name": row.name}

            rights = await post("/nd.bizassets.s/v1/get_user_rights", token, did, {})
            entry["user_rights_code"] = rights.get("code")
            entry["user_rights_msg"] = rights.get("msg")
            entry["user_rights_data"] = rights.get("data")

            # 找一个视频（带 gcid）
            item = None
            for page in range(10):
                p = await asyncio.to_thread(
                    d.client.fs_files, parent_id="*", page=page, page_size=1000, res_type=1
                )
                for it in (p.get("data") or {}).get("list") or []:
                    if str(it.get("fileName", "")).lower().endswith((".mkv", ".mp4")):
                        item = it
                        break
                if item:
                    break
                await asyncio.sleep(0.3)
            if item:
                fid = str(item.get("fileId") or "")
                gcid = str(item.get("gcid") or item.get("resGcid") or "")
                entry["file"] = {
                    "name": item.get("fileName"),
                    "size": int(item.get("fileSize") or 0),
                    "fileId": fid,
                    "gcid": gcid[:12] + "…" if gcid else "",
                    "keys": sorted(item.keys()),
                }
                vod = await post(
                    "/userres/v1/file/get_vod_download_url",
                    token,
                    did,
                    {"fileId": fid, "gcid": gcid},
                )
                data = vod.get("data") or {}
                entry["vod"] = {
                    "code": vod.get("code"),
                    "msg": vod.get("msg"),
                    "keys": sorted(data.keys()) if isinstance(data, dict) else [],
                    "has_url": bool(isinstance(data, dict) and data.get("signedURL")),
                    "duration": data.get("urlDuration") if isinstance(data, dict) else None,
                }
            out[row.id] = entry
            log(
                f"{row.id} {row.name}: rights={entry['user_rights_code']} "
                f"{entry['user_rights_msg']} vod={entry.get('vod')}"
            )

        log("===== RESULT =====")
        log(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    finally:
        await dispose_db()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
