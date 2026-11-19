"""收尾验证：直接直链 vs 分享直链（同文件参数对比 + 删除分享后是否失效）。

流程：
1. 视频：直接直链可用；分享直链 → 207（视频不允许免登录下载）
2. 图片：直接直链 + 分享直链都取到 → 逐参数对比、各自 HEAD/Range
3. 删除分享 → 再 HEAD 分享直链，判断链接是否随分享生命周期失效
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
from datetime import datetime
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.environ.get("MEDIALAB_APP_PATH", "/app"))

import httpx
from app.adapters.drivers.base import DriverError
from app.core.config import get_settings
from app.db.models import Driver
from app.db.session import dispose_db, init_db, session_scope
from app.services.driver_factory import build_driver
from app.services.migration import persist_guangya_credential
from sqlalchemy import select

DRIVER_ID = int(os.environ.get("MEDIALAB_PROBE_DRIVER", "2"))
VIDEO_HINT = os.environ.get("MEDIALAB_PROBE_VIDEO", "缅北电诈覆灭纪实 - S01E03 - 第 3 集.mp4")
IMAGE_HINT = os.environ.get("MEDIALAB_PROBE_IMAGE", "缅北电诈覆灭纪实 - S01E03 - 第 3 集.jpg")

logger = logging.getLogger(__name__)


def log(msg: str) -> None:
    """输出运行日志（走 logging，stdout 与旧版 print 行为一致）。"""
    logger.info(msg)


def fp(v: str) -> str:
    """字符串指纹（sha1 前 8 位）——只用于比对，不泄露原文。"""
    return hashlib.sha1(v.encode()).hexdigest()[:8]


def url_meta(url: str) -> dict:
    """拆解直链的 host / 参数 / 签发时刻（参数只留指纹，不留原值）。"""
    p = urlparse(url)
    q = parse_qs(p.query)
    meta = {
        "host": p.netloc,
        "path": p.path,
        "keys": sorted(q.keys()),
        "len": len(url),
        "params": {k: {"len": len(v[0]), "fp": fp(v[0])} for k, v in sorted(q.items())},
    }
    ts = q.get("ts", [None])[0]
    if ts and ts.isdigit():
        meta["ts"] = int(ts)
        meta["ts_bj"] = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    return meta


async def http_probe(url: str, do_range: bool = True, label: str = "") -> dict:
    """对直链做 HEAD（+ 可选 Range）探测。

    Args:
        url: 待探测的直链。
        do_range: 是否追加一次 Range 取样。
        label: 日志标签。

    Returns:
        dict: 探测结果（状态码 / 头部 / 取样字节数；失败时带 ``error``）。
    """
    headers = {"User-Agent": "curl/8.0.1", "Accept": "*/*"}
    for _ in range(3):
        out: dict = {}
        try:
            async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as http:
                try:
                    head = await http.head(url, headers=headers)
                    out["head"] = {
                        "status": head.status_code,
                        "final_host": urlparse(str(head.url)).netloc,
                        "content_type": head.headers.get("content-type"),
                        "content_length": head.headers.get("content-length"),
                        "content_disposition": head.headers.get("content-disposition"),
                        "accept_ranges": head.headers.get("accept-ranges"),
                        "etag": head.headers.get("etag"),
                    }
                except httpx.HTTPError as exc:
                    out["head"] = {"error": f"{type(exc).__name__}: {exc}"}
                if out.get("head", {}).get("status") == 200 or not do_range:
                    return out
                got = 0
                status = None
                try:
                    async with http.stream(
                        "GET", url, headers={**headers, "Range": "bytes=0-65563"}
                    ) as resp:
                        status = resp.status_code
                        async for chunk in resp.aiter_bytes():
                            got += len(chunk)
                            if got >= 65564:
                                break
                except httpx.HTTPError as exc:
                    out["range"] = {"error": f"{type(exc).__name__}: {exc}"}
                else:
                    out["range"] = {
                        "status": status,
                        "bytes_read": got,
                        "partial_206": status == 206,
                    }
                    return out
        except httpx.HTTPError as exc:
            out["fatal"] = f"{type(exc).__name__}: {exc}"
        log(f"  ({label} HTTP 重试)")
        await asyncio.sleep(1.5)
    return out


def compare(a: dict, b: dict) -> dict:
    """比对两条直链的参数（相同 / 不同 / 各自独有）。"""
    pa, pb = a["params"], b["params"]
    return {
        "same": sorted(k for k in set(pa) & set(pb) if pa[k]["fp"] == pb[k]["fp"]),
        "diff": sorted(k for k in set(pa) & set(pb) if pa[k]["fp"] != pb[k]["fp"]),
        "only_direct": sorted(set(pa) - set(pb)),
        "only_share": sorted(set(pb) - set(pa)),
    }


async def share_case(driver, client, file: dict, st: int = 0) -> dict:
    """建一个临时分享，取分享直链并探测，结束后删分享。

    Args:
        driver: 光鸭驱动实例。
        client: 底层 guangyaclient。
        file: 目标文件条目。
        st: ``shareType``（0 = 免登录下载，1 = 带码）。

    Returns:
        dict: 分享链路的结果（结论 / 直链元信息 / 探测结果）。
    """
    fid = str(file.get("fileId") or "")
    out: dict = {"shareType": st}
    share = None
    full_url = ""
    try:
        share = await asyncio.to_thread(
            client.share_create, [fid], f"probe-final-{st}", 0, st, "", True, "0", 0, 1
        )
        out["create_code"] = share.get("code")
        out["create_msg"] = share.get("msg")
        if share.get("code") not in (None, 0):
            return out
        data = share.get("data") or {}
        share_id = str(data.get("shareId") or "")
        share_url = str(data.get("shareUrl") or "")
        tail = share_url.rsplit("/", 1)[-1] if share_url else ""
        out["share_id"] = share_id
        out["share_url_tail"] = f"len={len(tail)} has_underscore={'_' in tail}"
        await asyncio.sleep(2.5)
        url_id = tail or share_id
        code = str(data.get("code") or "")
        try:
            token = await driver.share_access_token(url_id, code)
        except DriverError as exc:
            out["token_error"] = str(exc)
            return out
        contents = await driver.share_files(token)
        out["files_in_share"] = len(contents)
        if not contents:
            out["verdict"] = "分享内容为空"
            return out
        raw = await asyncio.to_thread(client.share_download_url, contents[0], token)
        out["code"] = raw.get("code")
        out["msg"] = raw.get("msg")
        d = raw.get("data") or {}
        if isinstance(d, str):
            d = {"downloadUrl": d}
        out["data_keys"] = sorted(d.keys()) if isinstance(d, dict) else []
        url = str(d.get("signedURL") or d.get("downloadUrl") or "")
        if url:
            full_url = url
            out["url"] = url_meta(url)
            out["http_before_delete"] = await http_probe(url, label="share")
            out["verdict"] = "可用"
        else:
            out["verdict"] = f"失败 code={raw.get('code')} msg={raw.get('msg')}"
    finally:
        if share and share.get("code") in (None, 0):
            sid = str((share.get("data") or {}).get("shareId") or "")
            if sid:
                try:
                    await driver.delete_share([sid])
                    out["share_deleted"] = True
                except DriverError as exc:
                    out["share_delete_error"] = str(exc)
    if full_url:
        await asyncio.sleep(2.0)
        out["http_after_delete"] = await http_probe(full_url, do_range=False, label="after-del")
    return out


async def main() -> int:
    """跑一遍直接直链 / 分享直链的对比探针。

    Returns:
        int: 0 表示探针跑完；1 表示存储源不存在。
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)

    init_db(get_settings())
    try:
        async with session_scope() as db:
            row = (
                (await db.execute(select(Driver).where(Driver.id == DRIVER_ID)))
                .scalars()
                .first()
            )
        if row is None:
            log("❌ 存储源不存在")
            return 1

        async def _on_refresh(cred: object) -> None:
            await persist_guangya_credential(row.id, cred)

        driver = build_driver(
            row.type, row.credential, row.device_type, on_token_refresh=_on_refresh
        )
        client = driver.client

        files: list[dict] = []
        for page in range(20):
            payload = await asyncio.to_thread(
                client.fs_files, parent_id="*", page=page, page_size=1000, res_type=1
            )
            files.extend(list((payload.get("data") or {}).get("list") or []))
            await asyncio.sleep(0.3)
        video = next((f for f in files if VIDEO_HINT in str(f.get("fileName") or "")), None)
        image = next((f for f in files if IMAGE_HINT in str(f.get("fileName") or "")), None)
        log(f"视频：{video.get('fileName') if video else None}")
        log(f"图片：{image.get('fileName') if image else None}")

        result: dict = {}
        for label, file in (("video", video), ("image", image)):
            if file is None:
                continue
            fid = str(file.get("fileId") or "")
            direct = await driver.get_direct_link(fid, "curl/8.0.1")
            dmeta = url_meta(direct.url)
            entry: dict = {
                "file": {
                    "name": file.get("fileName"),
                    "size": int(file.get("fileSize") or 0),
                    "fileId": fid,
                },
                "direct": {
                    "url": dmeta,
                    "ua_required": direct.ua_required,
                    "expires_at": direct.expires_at,
                    "http": await http_probe(direct.url, label="direct"),
                },
                "shareType0": await share_case(driver, client, file, 0),
            }
            if entry["shareType0"].get("url"):
                entry["compare"] = compare(dmeta, entry["shareType0"]["url"])
            entry["shareType1"] = await share_case(driver, client, file, 1)
            result[label] = entry
            share1 = entry["shareType1"]
            log(
                f"{label}: direct ok; shareType0={entry['shareType0'].get('verdict')} "
                f"shareType1={share1.get('verdict') or share1.get('token_error')}"
            )

        log("===== RESULT =====")
        log(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        await dispose_db()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
