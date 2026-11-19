"""对比 Web 路径与客户端路径取直链（含 dt=5/6 客户端身份）。

客户端逆向结论（/Applications/光鸭云盘.app，Electron 1.1.2）：
- 取链接口 `/userres/v1/get_res_download_url`（与 Web 的
  `/nd.bizuserres.s/v1/get_res_download_url` 前缀不同）
- 请求头：`Authorization: Bearer`、`did: <deviceId>`、`dt: darwin?6:5`、
  `av: <版本>`、`vc: <versionCode>`
- 响应除 signedURL/urlDuration 外还有 `speedupSignature`
  （原生 P2P 下载器 `download-addon.node` 用它 setAccelerateToken）

本探针验证：同一账号、同一文件，四条路径/身份组合的返回差异
（重点看非会员的大文件是否出现 254）。

只读：不下载文件、不写任何数据。
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

DRIVER_IDS = [2, 3, 4, 5]
API = "https://api.guangyapan.com"
WEB_PATH = "/nd.bizuserres.s/v1/get_res_download_url"
CLIENT_PATH = "/userres/v1/get_res_download_url"
CLIENT_ID_WEB = "aMe-8VSlkrbQXpUR"       # Web 端固定值（guangyaclient 用）
CLIENT_ID_MAC = "al9eLEtLE7_pp8GR"       # 逆向自 app.asar（macClientId）
CLIENT_SECRET_MAC = "DuwM94T7EfGVeS4XhMncKg"


def log(msg: str) -> None:
    print(msg, flush=True)


async def get_assets(driver, token: str, did: str, dt: str, client_id: str) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Client-Id": client_id,
        "X-Device-Id": did,
        "did": did,
        "dt": dt,
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
    }
    r = await asyncio.to_thread(
        httpx.post, f"{API}/nd.bizassets.s/v1/get_assets", json={}, headers=headers, timeout=20.0
    )
    return r.json()


async def try_link(
    token: str, did: str, path: str, dt: str, client_id: str, fid: str, extra: dict | None = None
) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Client-Id": client_id,
        "X-Device-Id": did,
        "did": did,
        "dt": dt,
        "av": "1.1.2",
        "vc": "1010",
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.guangyapan.com",
        "Referer": "https://www.guangyapan.com/",
    }
    body = {"fileId": str(fid)}
    if extra:
        body.update(extra)
    r = await asyncio.to_thread(
        httpx.post, f"{API}{path}", json=body, headers=headers, timeout=25.0
    )
    try:
        p = r.json()
    except Exception:
        return {"http": r.status_code, "raw": r.text[:200]}
    d = p.get("data") or {}
    return {
        "http": r.status_code,
        "code": p.get("code"),
        "msg": p.get("msg"),
        "has_url": bool(isinstance(d, dict) and (d.get("signedURL") or d.get("downloadUrl"))),
        "keys": sorted(d.keys()) if isinstance(d, dict) else [],
        "duration": d.get("urlDuration") if isinstance(d, dict) else None,
        "has_speedup": bool(isinstance(d, dict) and d.get("speedupSignature")),
    }


async def pick_big(driver) -> dict | None:
    client = driver.client
    videos: list[dict] = []
    for page in range(12):
        payload = await asyncio.to_thread(
            client.fs_files, parent_id="*", page=page, page_size=1000, res_type=1
        )
        videos.extend(list((payload.get("data") or {}).get("list") or []))
        await asyncio.sleep(0.3)
    vids = [
        f
        for f in videos
        if str(f.get("fileName", "")).lower().endswith((".mkv", ".mp4", ".ts"))
    ]
    if not vids:
        return None
    big = [f for f in vids if int(f.get("fileSize") or 0) >= 1024**3]
    if big:
        return min(big, key=lambda f: int(f.get("fileSize") or 0))
    return max(vids, key=lambda f: int(f.get("fileSize") or 0))


async def main() -> int:
    init_db(get_settings())
    try:
        async with session_scope() as db:
            rows = (
                (await db.execute(select(Driver).where(Driver.id.in_(DRIVER_IDS))))
                .scalars()
                .all()
            )
        by_id = {r.id: r for r in rows}

        out: dict = {}
        for did_driver in DRIVER_IDS:
            row = by_id.get(did_driver)
            if row is None:
                continue
            async def _on_refresh(cred: object, _id=did_driver) -> None:
                await persist_guangya_credential(_id, cred)

            driver = build_driver(
                row.type, row.credential, row.device_type, on_token_refresh=_on_refresh
            )
            await driver._ensure_token()
            token = driver.client.token
            did = (getattr(driver._cred, "device_id", "") or "")
            entry: dict = {"name": row.name}
            try:
                a = await get_assets(driver, token, did, "6", CLIENT_ID_MAC)
                d = a.get("data") or {}
                entry["vip_status"] = d.get("vipStatus")
                entry["vip_left"] = d.get("vipLeftTime")
                entry["traffic"] = d.get("freeDirectLinkTraffic")
            except Exception as exc:
                entry["assets_error"] = str(exc)

            target = await pick_big(driver)
            if target:
                fid = str(target.get("fileId") or "")
                entry["file"] = {
                    "name": target.get("fileName"),
                    "size": int(target.get("fileSize") or 0),
                    "fileId": fid,
                }
                entry["web_dt1"] = await try_link(
                    token, did, WEB_PATH, "1", CLIENT_ID_WEB, fid
                )
                entry["web_dt6"] = await try_link(
                    token, did, WEB_PATH, "6", CLIENT_ID_MAC, fid
                )
                entry["client_dt6"] = await try_link(
                    token, did, CLIENT_PATH, "6", CLIENT_ID_MAC, fid
                )
                entry["client_dt5"] = await try_link(
                    token, did, CLIENT_PATH, "5", CLIENT_ID_MAC, fid
                )
                entry["client_dt6_reqid"] = await try_link(
                    token, did, CLIENT_PATH, "6", CLIENT_ID_MAC, fid, {"requestId": "0"}
                )
            out[f"{row.id} {row.name}"] = entry
            log(
                f"驱动 {row.id}（{row.name}）："
                f"vip={entry.get('vip_status')} 文件={entry.get('file')}"
            )
            for k in ("web_dt1", "web_dt6", "client_dt6", "client_dt5"):
                if k in entry:
                    log(f"  {k}: {json.dumps(entry[k], ensure_ascii=False)}")

        log("===== RESULT =====")
        log(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    finally:
        await dispose_db()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
