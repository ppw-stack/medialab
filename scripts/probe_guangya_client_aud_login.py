"""用光鸭**客户端 clientId** 自助申领同 aud 的 token（扫码探针，只读）。

**背景**：服务端按 access_token 的 ``aud`` 认「是不是客户端」，``aud`` 就是
签发时请求里的 ``client_id``。客户端 token 只有 2 小时（``exp = iat + 7200``），
历史上靠从本机客户端凭据库解密同步（``export_guangya_client_token.py``）。

**本探针证明的事**：同一个账号不必从客户端抓——用客户端自己的 ``client_id``
走标准的 device-code 扫码流程，服务端会签发出 ``aud = 客户端 clientId``、
``scope = user sso offline profile`` 的 token（含 ``refresh_token``），
之后用同一 ``client_id`` 走 ``refresh_token`` grant 自己续期。

**不能做的事**：JWT 是 ``RS256``（``kid=48dd6c44-...``），私钥在服务端，
本地无法自签一个「对应 ID」的 token；只能请求服务端签发。

流程（四步，全在 ``account.guangyapan.com``）：

1. ``POST /v1/auth/device/code``   （headers: ``X-Client-Id`` / ``X-Device-Id``）
2. 浏览器打开 ``verification_uri_complete``，用光鸭账号确认授权
3. ``POST /v1/auth/token``  ``grant_type=urn:ietf:params:oauth:grant-type:device_code``
4. ``POST /v1/auth/token``  ``grant_type=refresh_token``（验证续期 + aud 是否跟随
   ``client_id``、``refresh_token`` 是否轮换）

用法::

    uv run python scripts/probe_guangya_client_aud_login.py
    uv run python scripts/probe_guangya_client_aud_login.py --device-id <已有设备id>
    uv run python scripts/probe_guangya_client_aud_login.py --out /tmp/gyclient-aud.json

输出（stdout / ``--out``）::

    {"sub": "...", "device_id": "...", "access_token": "...",
     "refresh_token": "...", "expires_at": 1795..., "aud": "al9eLEtLE7_pp8GR",
     "scope": "...", "client_id": "al9eLEtLE7_pp8GR"}

安全边界：只做读 + 登录；不写客户端数据库、不碰已导入的驱动凭据。
"""

from __future__ import annotations

import argparse
import base64
import json
import secrets
import sys
import time
import urllib.error
import urllib.request

#: 账号域名（登录 / 刷新 token）
ACCOUNT_BASE = "https://account.guangyapan.com"

#: macOS 客户端自己的 client_id（``app.asar`` 里的 ``macClientId``）。
#: **它决定签发出来的 token 的 ``aud``**——这就是「对应 ID」的来源。
CLIENT_ID_MAC = "al9eLEtLE7_pp8GR"

#: 客户端登录用的 scope（``offline`` 是拿到 ``refresh_token`` 的前提）
CLIENT_SCOPE = "user sso offline profile"

#: 轮询间隔 / 总超时（秒）
POLL_INTERVAL = 2
POLL_TIMEOUT = 180

#: device_code 的授权类型
DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


def _post(
    path: str,
    payload: dict,
    client_id: str,
    device_id: str,
    extra=None,
    bearer: str = "",
) -> dict:
    """POST 一个 JSON 接口。"""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "X-Client-Id": client_id,
        "X-Device-Id": device_id,
    }
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    if extra:
        headers.update(extra)
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{ACCOUNT_BASE}{path}", data=body, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:  # 4xx 也带 JSON body，照样解析
        raw = exc.read()
    except urllib.error.URLError as exc:
        raise SystemExit(f"请求失败：{exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise SystemExit(f"响应不是 JSON：{raw[:200]!r}") from exc
    if not isinstance(data, dict):
        raise SystemExit("响应格式异常")
    return data


def _jwt_payload(token: str) -> dict:
    """解 JWT 的第二段（不校验签名，只为看 aud/scope/exp）。"""
    try:
        seg = token.split(".")[1]
    except IndexError:
        return {}
    seg += "=" * (-len(seg) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(seg))
    except (ValueError, json.JSONDecodeError):
        return {}


def _summarize(tag: str, data: dict) -> dict:
    """打印并返回一份摘要（不打印 token 明文）。"""
    token = str(data.get("access_token") or "")
    payload = _jwt_payload(token)
    info = {
        "tag": tag,
        "has_access_token": bool(token),
        "access_token_len": len(token),
        "has_refresh_token": bool(data.get("refresh_token")),
        "refresh_token_len": len(str(data.get("refresh_token") or "")),
        "expires_in": data.get("expires_in"),
        "sub": payload.get("sub") or data.get("sub"),
        "aud": payload.get("aud"),
        "scope": payload.get("scope"),
        "exp": payload.get("exp"),
        "iat": payload.get("iat"),
    }
    if payload.get("exp") and payload.get("iat"):
        info["lifetime_sec"] = int(payload["exp"]) - int(payload["iat"])
    print(json.dumps(info, ensure_ascii=False, indent=2), flush=True)
    return info


def main(argv=None) -> int:
    """入口。"""
    parser = argparse.ArgumentParser(description="光鸭客户端 aud token 自助申领探针")
    parser.add_argument(
        "--device-id", default="", help="设备标识（默认新生成 32 位 hex）"
    )
    parser.add_argument(
        "--client-id", default=CLIENT_ID_MAC, help=f"client_id（默认 {CLIENT_ID_MAC}）"
    )
    parser.add_argument(
        "--scope", default=CLIENT_SCOPE, help="登录 scope（需含 offline）"
    )
    parser.add_argument("--out", default="", help="成功时把凭据 JSON 写到该文件")
    parser.add_argument(
        "--no-refresh-test", action="store_true", help="跳过 refresh_token 续期验证"
    )
    parser.add_argument(
        "--timeout", type=int, default=POLL_TIMEOUT, help="等待授权的总秒数"
    )
    parser.add_argument(
        "--token-file",
        default="",
        help=(
            "已有账号 access_token 的 JSON 文件（含 access_token 字段）——给了就"
            "走 /v1/user/device/authorize **自助授权**，不需要任何人点浏览器"
        ),
    )
    args = parser.parse_args(argv)

    device_id = args.device_id.strip() or secrets.token_hex(16)

    # 1) 取 device code
    start = _post(
        "/v1/auth/device/code",
        {"scope": args.scope, "client_id": args.client_id},
        args.client_id,
        device_id,
    )
    if start.get("error") or start.get("code") not in (None, 0):
        raise SystemExit(f"取 device_code 失败：{start}")
    device_code = str(start.get("device_code") or "")
    if not device_code:
        raise SystemExit(f"响应里没有 device_code：{start}")

    print(f"device_id = {device_id}")
    print(f"user_code = {start.get('user_code')}")
    timeout = max(30, args.timeout)

    if args.token_file:
        # **自助授权**：用已有账号 token 调 /v1/user/device/authorize，
        # 服务端把这个 user_code 绑到该账号上——不需要任何人打开网页。
        with open(args.token_file, encoding="utf-8") as fh:
            seed = json.load(fh)
        seed_token = str(seed.get("access_token") or "")
        if not seed_token:
            raise SystemExit(f"{args.token_file} 里没有 access_token")
        authorized = _post(
            "/v1/user/device/authorize",
            {
                "client_id": args.client_id,
                "user_code": str(start.get("user_code") or ""),
            },
            args.client_id,
            device_id,
            bearer=seed_token,
        )
        print("device/authorize →", json.dumps(authorized, ensure_ascii=False)[:200])
        if authorized.get("error") or authorized.get("code") not in (None, 0):
            raise SystemExit(f"自助授权失败：{authorized}")
    else:
        print(f"请用光鸭账号批准：{start.get('verification_uri_complete')}")
        print(f"（{timeout} 秒内轮询，interval={start.get('interval')}）", flush=True)

    # 2) 轮询 token
    interval = int(start.get("interval") or POLL_INTERVAL)
    waited = 0
    token_data: dict = {}
    while waited < timeout:
        time.sleep(interval)
        waited += interval
        data = _post(
            "/v1/auth/token",
            {
                "grant_type": DEVICE_CODE_GRANT,
                "device_code": device_code,
                "client_id": args.client_id,
            },
            args.client_id,
            device_id,
        )
        error = str(data.get("error") or "")
        if error == "authorization_pending":
            print(f"  [{waited}s] 等待授权…", flush=True)
            continue
        if error:
            raise SystemExit(f"授权失败：{data}")
        if not data.get("access_token"):
            print(f"  [{waited}s] 响应无 access_token，继续轮询…", flush=True)
            continue
        token_data = data
        break

    if not token_data:
        raise SystemExit("轮询超时：没有拿到 token")

    info = _summarize("login", token_data)

    # 3) 续期验证（自家会话，不影响客户端）
    audit: dict = {"login": info}
    if not args.no_refresh_test and token_data.get("refresh_token"):
        refreshed = _post(
            "/v1/auth/token",
            {
                "grant_type": "refresh_token",
                "refresh_token": token_data["refresh_token"],
                "client_id": args.client_id,
            },
            args.client_id,
            device_id,
            extra={"x-action": "401"},
        )
        if refreshed.get("access_token"):
            audit["refresh"] = _summarize("refresh", refreshed)
            audit["refresh"]["refresh_token_rotated"] = (
                str(refreshed.get("refresh_token") or "")
                != str(token_data.get("refresh_token") or "")
            )
            print(
                "refresh_token_rotated =",
                audit["refresh"]["refresh_token_rotated"],
                flush=True,
            )
        else:
            audit["refresh"] = {"error": refreshed}
            print(json.dumps(audit["refresh"], ensure_ascii=False, indent=2))

    # 4) 输出凭据
    result = {
        "sub": info.get("sub") or "",
        "device_id": device_id,
        "access_token": token_data.get("access_token"),
        "refresh_token": token_data.get("refresh_token") or "",
        "expires_at": int(info.get("exp") or 0)
        or int(time.time()) + int(token_data.get("expires_in") or 0),
        "aud": info.get("aud") or "",
        "scope": info.get("scope") or "",
        "client_id": args.client_id,
    }
    text = json.dumps(result, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"已写入 {args.out}")
    else:
        print(text)

    if result["aud"] != args.client_id:
        print(
            f"⚠️ aud={result['aud']!r} 与请求的 client_id={args.client_id!r} 不一致",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
