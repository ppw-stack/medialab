"""光鸭「每月上传限额」撞墙探针（抓真实报错原文）。

## 为什么需要这个探针

光鸭的**每月上传限额**没有查询接口——只能撞墙（与「云添加额度」同一套
哲学）。「本地同步 → 网盘聚合」的落点分配器靠**文案特征**判定额度墙
（``app/services.aggregate_upload.is_upload_quota_wall``），但**真实撞墙
文案未知**——这里把它抓出来，顺带验证现有关键词表是否命中。

## 回答三个问题

1. **额度墙在哪一步**：`upload_token`（取凭证）/ OSS PUT（传字节）/
   ``upload_info``（落盘轮询）——三处的报错形态不同。
2. **原始返回长什么样**：``code`` + ``msg`` 原文（生产视角的
   ``DriverError`` 文案只是它的再包装）。
3. **现有关键词表命中吗**：把抓到的文案喂给 ``is_upload_quota_wall``，
   逐条打印 ✅/❌——这就是要补的清单。

## 跑法（生产容器，凭据不出容器）::

    ssh unraid "docker exec -i medialab /opt/venv/bin/python -" \\
        < scripts/probe_guangya_upload_quota.py

环境变量：

- ``MEDIALAB_PROBE_DRIVER_ID`` —— 目标存储源 id（默认按名字含「小号1」选）
- ``MEDIALAB_PROBE_CONTROL`` —— 可选：对照账号 id（健康副盘），跑同款
- ``MEDIALAB_PROBE_NAME`` —— 目标名字关键字（默认「小号1」）

## 安全边界

- **只建/只删自己命名的探针目录**（``上传额度探针-{时间戳}``），不动任何
  既有文件
- 探针文件 **64 字节**——即便计入额度也可忽略（账号本就到顶）
- 删除走 ``fs_delete``（**进回收站**），**绝不**碰回收站清空接口
- token 刷新**写回 DB**（``on_token_refresh``）——漏传会导致重启后 401
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, os.environ.get("MEDIALAB_APP_PATH", "/app"))

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models import Driver
from app.db.session import init_db, session_scope
from app.services.driver_factory import build_driver
from app.services.migration import persist_guangya_credential
from sqlalchemy import select

logger = get_logger(__name__)

#: 目标账号的名字关键字
TARGET_NAME = os.environ.get("MEDIALAB_PROBE_NAME", "小号1")
#: 目标账号 id（给了就按 id 选）
TARGET_DRIVER_ID = os.environ.get("MEDIALAB_PROBE_DRIVER_ID")
#: 对照账号 id（可选）
CONTROL_DRIVER_ID = os.environ.get("MEDIALAB_PROBE_CONTROL")

#: 探针文件名后缀
STAMP = str(int(time.time()))
#: 探针文件大小（字节）——**极小**，够触发服务端校验即可。
#:
#: ⚠️ **小于 1MB 走「单 PUT 直传」，≥1MB 才走「分片上传」**（服务端的两条
#: 链路，限额校验可能只在大文件那条上）。想测大文件链路就把它调到
#: 1MB 以上（如 ``1200000``）。
PROBE_SIZE = int(os.environ.get("MEDIALAB_PROBE_SIZE", "64"))
#: 探针文件后缀（默认 txt；真实同步传的是媒体文件）
PROBE_EXT = os.environ.get("MEDIALAB_PROBE_EXT", ".txt")
#: 目标目录 = **组里的副盘缓冲根**（真实落点）而不是自建探针目录
PROBE_BUFFER = os.environ.get("MEDIALAB_PROBE_BUFFER", "") == "1"
#: 测**所有**副盘（不限目标账号）
PROBE_ALL = os.environ.get("MEDIALAB_PROBE_ALL", "") == "1"
#: 探针目录名（**自建自删**）
PROBE_DIR_NAME = f"上传额度探针-{STAMP}"
#: 探针文件名
PROBE_FILE_NAME = f"probe-upload-{STAMP}{PROBE_EXT}"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def dump(title: str, payload: Any) -> None:
    """打印一段原始返回（JSON 缩进，便于人眼核对）。"""
    print(f"--- {title} ---", flush=True)
    if isinstance(payload, (dict, list)):
        print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
    else:
        print(repr(payload), flush=True)


def _b64_md5(data: bytes) -> str:
    return base64.b64encode(hashlib.md5(data).digest()).decode("ascii")


async def _raw_upload_token(
    client: Any, name: str, size: int, parent_id: str, md5: str | None
) -> Any:
    """调底层 ``upload_token``（**只取凭证，不传字节**）。

    不同版本的客户端签名不一样 → 依次尝试几种调用方式。

    ``md5``：**小文件必带**（服务端不认不带 md5 的小文件凭证），大文件
    不带（生产同款行为）。
    """
    if md5:
        attempts = (
            lambda: client.upload_token(name, size, parent_id=parent_id, md5=md5),
            lambda: client.upload_token(name, size, parent_id),
            lambda: client.upload_token(name, size),
        )
    else:
        attempts = (
            lambda: client.upload_token(name, size, parent_id=parent_id),
            lambda: client.upload_token(name, size),
        )
    last: Exception | None = None
    for fn in attempts:
        try:
            return await asyncio.to_thread(fn)
        except TypeError as exc:  # 签名不匹配 → 换下一种
            last = exc
            continue
    raise last if last else RuntimeError("upload_token 调用失败")


def _check_matcher(texts: list[str]) -> None:
    """把抓到的文案喂给撞墙判定器 —— **现有关键词表命中吗**。

    ⚠️ 生产容器跑的是**已部署的镜像**——若它还没有
    ``app.services.aggregate_upload``（本次改动尚未部署），则退回探针
    内联的**同一份关键词表**（等价，但要提醒「这是本地副本」）。
    """
    matcher = None
    source = "已部署的 aggregate_upload"
    try:
        from app.services.aggregate_upload import is_upload_quota_wall

        matcher = is_upload_quota_wall
    except Exception as exc:
        source = f"探针内联副本（容器里没装 aggregate_upload：{exc}）"
        matcher = _local_matcher

    print(f"=== 关键词表命中情况（{source}）===", flush=True)
    for text in texts:
        if not text:
            continue
        try:
            hit = matcher(text)
        except Exception as exc:  # pragma: no cover - 防御性
            print(f"⚠️ 判定器异常：{exc}", flush=True)
            continue
        print(f"{'✅ 命中' if hit else '❌ 未命中'}：{text}", flush=True)


#: 关键词表（与 ``app.services.aggregate_upload`` 同款——容器没装时兜底）
_QUOTA_WALL_KEYWORDS = (
    "限额",
    "额度",
    "配额",
    "超限",
    "上传流量",
    "流量超",
    "quota",
    "limit exceeded",
)
_NOT_QUOTA_KEYWORDS = ("限流", "过于频繁", "too many requests", "rate limit")


async def _buffer_root_of(driver_id: int) -> str:
    """取该副盘在聚合组里的**缓冲根 fileId**（真实落点）。"""
    from app.db.models import AggregateGroupRoot

    async with session_scope() as db:
        row = (
            await db.execute(
                select(AggregateGroupRoot).where(
                    AggregateGroupRoot.driver_id == driver_id
                )
            )
        ).scalar_one_or_none()
        return str(row.root_id) if row is not None else ""


def _local_matcher(reason: str) -> bool:
    """撞墙判定的**本地副本**（与线上同一份关键词表）。"""
    text = (reason or "").lower()
    if not text:
        return False
    if any(word in text for word in _NOT_QUOTA_KEYWORDS):
        return False
    return any(word in text for word in _QUOTA_WALL_KEYWORDS)


async def _probe_one(driver_row: Driver, label: str) -> dict[str, Any]:
    """对单个账号跑一遍探针，返回结论字典。"""
    result: dict[str, Any] = {
        "driver_id": int(driver_row.id),
        "name": str(driver_row.name),
        "label": label,
        "probe_dir": "",
        "steps": [],
    }
    log(f"===== {label}：{driver_row.name}（driver {driver_row.id}）=====")

    driver = build_driver(
        "guangya",
        driver_row.credential,
        driver_row.device_type,
        on_token_refresh=lambda cred: persist_guangya_credential(
            driver_row.id, cred
        ),
    )
    client = driver.client
    uploader = None
    try:
        from app.adapters.drivers.guangya_upload import build_guangya_uploader

        uploader = build_guangya_uploader(client, driver)
    except Exception as exc:  # pragma: no cover - 防御性
        log(f"⚠️ 构造上传器失败：{exc}")

    # ① 建探针目录（自建自删）；``PROBE_BUFFER`` 模式直接用**真实落点**
    #    （组里的副盘缓冲根）——只上传后删文件，不动目录
    dir_id = ""
    use_buffer = PROBE_BUFFER
    if use_buffer:
        dir_id = await _buffer_root_of(driver_row.id)
        log(f"真实落点（组内缓冲根）：fileId={dir_id}")
        if not dir_id:
            log("⚠️ 该副盘没有配缓冲根——改用自建探针目录")
            use_buffer = False
    if not use_buffer:
        dir_id, _created = await driver.create_dir(PROBE_DIR_NAME, "")
        result["probe_dir"] = PROBE_DIR_NAME
        log(f"探针目录：{PROBE_DIR_NAME}（fileId={dir_id}）")

    # 账号容量（**空间满也会拒上传**——别把容量当成限额）
    try:
        space = await driver.get_space_info()
        log(
            f"账号容量：总 {space.get('total_bytes')} / 已用 "
            f"{space.get('used_bytes')} / 剩余 {space.get('remain_bytes')}"
        )
        result["space"] = space
    except Exception as exc:
        log(f"（容量读取失败：{exc}）")

    # 账号**原始资产信息**（``get_assets``）——上传额度（若有）就藏在这里
    #
    # 我们的 ``get_space_info`` 只取了容量 / vipStatus / vipLeftTime /
    # freeDirectLinkTraffic（直链流量）。**上传流量 / 上传次数**这类字段
    # 可能也在原始返回里，只是封装时没取——全量打印出来自己看。
    try:
        assets = await driver._call_assets()  # 探针要看原始返回
        dump("get_assets 原始返回", assets)
        result["assets"] = assets
        data = assets.get("data") or {}
        for key, value in sorted(data.items()):
            if key in ("totalSpaceSize", "usedSpaceSize"):
                continue
            log(f"  资产字段 {key} = {value!r}")
    except Exception as exc:
        log(f"（get_assets 失败：{exc}）")

    probe_file = Path(f"/tmp/{PROBE_FILE_NAME}")
    payload_bytes = (b"probe-upload-quota-check\n" + b"0" * PROBE_SIZE)[
        :PROBE_SIZE
    ]
    probe_file.write_bytes(payload_bytes)
    # 小文件才要 md5（大文件生产路径不带）
    md5 = _b64_md5(payload_bytes) if PROBE_SIZE < 1024 * 1024 else None
    log(
        f"探针文件：{PROBE_FILE_NAME}（{PROBE_SIZE} 字节，"
        f"{'小文件单 PUT' if md5 else '大文件分片'}路径）"
    )

    try:
        # ② 第一发：**只取凭证**（零字节消耗）——墙多半在这里
        log("① 调 upload_token（只取凭证，不传字节）…")
        token_payload = await _raw_upload_token(
            client, PROBE_FILE_NAME, PROBE_SIZE, dir_id, md5
        )
        dump("upload_token 原始返回", token_payload)
        result["token_payload"] = token_payload
        code = token_payload.get("code") if isinstance(token_payload, dict) else None
        msg = (
            str(token_payload.get("msg") or "")
            if isinstance(token_payload, dict)
            else ""
        )
        result["token_code"] = code
        result["token_msg"] = msg
        if code not in (None, 0):
            log(f"❌ upload_token 被拒：code={code} msg={msg}")
            prod_text = f"光鸭取上传凭证失败：{msg or token_payload}"
            result["prod_message"] = prod_text
            log(f"生产视角文案：{prod_text}")
            _check_matcher([msg, prod_text, str(token_payload)])
        else:
            log("✅ upload_token 通过（墙不在凭证步）→ 走完整上传")
            # 秒传预检（``check_can_flash_upload``）——**额度信息可能藏在这**
            task_id = ""
            data = token_payload.get("data") or {}
            if isinstance(data, dict):
                task_id = str(data.get("taskId") or "")
            if task_id:
                try:
                    flash = await asyncio.to_thread(
                        client.check_can_flash_upload, task_id, str(probe_file)
                    )
                    dump("check_can_flash_upload 原始返回", flash)
                    result["flash"] = flash
                except Exception as exc:
                    log(f"（秒传预检失败：{exc}）")

        # ③ 第二发：完整上传（64 字节）——看墙在 OSS 还是轮询
        if uploader is not None:
            log("② 走完整上传（64 字节，生产同款路径）…")
            try:
                up = await uploader.upload_file(probe_file, dir_id, name=PROBE_FILE_NAME)
                dump("upload_file 结果", up)
                result["upload_ok"] = True
                result["uploaded_file_id"] = up.file_id
                log(f"✅ 上传成功：fileId={up.file_id} size={up.size}")
            except Exception as exc:
                log(f"❌ 上传失败：{type(exc).__name__}: {exc}")
                result["upload_ok"] = False
                result["upload_error"] = f"{type(exc).__name__}: {exc}"
                prod_text = str(exc)
                _check_matcher([prod_text])
    finally:
        # ④ 收尾：删探针文件（+ 探针目录）——进回收站，绝不碰清空接口
        if use_buffer:
            # 真实落点模式：**只删自己上传的那个文件**，目录不动
            log("③ 清理探针文件（真实落点模式，目录不动）…")
            try:
                entries = await driver.list_dir_by_cid(dir_id)
                mine = [
                    str(e.remote_id)
                    for e in entries
                    if not e.is_dir and e.name == PROBE_FILE_NAME
                ]
                if mine:
                    await driver.delete_files(mine)
                    log(f"已删探针文件 {len(mine)} 个")
                else:
                    log("（没找到探针文件——可能上传本就失败）")
            except Exception as exc:
                log(f"⚠️ 清理失败（可稍后手动删）：{exc}")
        else:
            log("③ 清理探针目录…")
            try:
                entries = await driver.list_dir_by_cid(dir_id)
                file_ids = [str(e.remote_id) for e in entries if not e.is_dir]
                if file_ids:
                    await driver.delete_files(file_ids)
                    log(f"已删探针文件 {len(file_ids)} 个")
                await driver.delete_files([dir_id])
            except Exception as exc:
                log(f"⚠️ 清理失败（可稍后手动删）：{exc}")
            try:
                remain = await driver.list_dir_by_cid("")
                still = [e for e in remain if e.name == PROBE_DIR_NAME]
                if still:
                    log(
                        f"⚠️ 探针目录仍在（删除是异步任务，稍后复查）：{PROBE_DIR_NAME}"
                    )
                else:
                    log("✅ 探针目录已消失")
            except Exception as exc:
                log(f"（复查跳过：{exc}）")
        with contextlib.suppress(Exception):
            probe_file.unlink(missing_ok=True)

    return result


async def main() -> None:
    init_db(get_settings())
    async with session_scope() as db:
        rows = (
            (
                await db.execute(
                    select(Driver).where(Driver.type == "guangya").order_by(Driver.id)
                )
            )
            .scalars()
            .all()
        )
        drivers = [
            {
                "id": int(r.id),
                "name": str(r.name),
                "enabled": bool(r.enabled),
                "role": r.role,
                "group": r.aggregate_group_id,
                "credential": r.credential,
                "device_type": r.device_type,
            }
            for r in rows
        ]

    log("=== 光鸭存储源清单 ===")
    for d in drivers:
        log(
            f"  id={d['id']} name={d['name']} enabled={d['enabled']} "
            f"role={d['role']} group={d['group']}"
        )

    # 选目标
    target: dict[str, Any] | None = None
    if TARGET_DRIVER_ID:
        target = next(
            (d for d in drivers if d["id"] == int(TARGET_DRIVER_ID)), None
        )
    else:
        target = next((d for d in drivers if TARGET_NAME in d["name"]), None)

    results: list[dict[str, Any]] = []
    if PROBE_ALL:
        # 测**所有**副盘（逐个）——看是不是只有某一个到顶
        for d in drivers:
            if d["role"] != "secondary":
                continue
            results.append(await _probe_one(_row(d), "副盘"))
    else:
        if target is None:
            log(f"❌ 找不到目标账号（名字含「{TARGET_NAME}」）")
            return
        results.append(await _probe_one(_row(target), "目标账号"))

    if CONTROL_DRIVER_ID:
        control = next(
            (d for d in drivers if d["id"] == int(CONTROL_DRIVER_ID)), None
        )
        if control is not None:
            results.append(await _probe_one(_row(control), "对照账号"))
        else:
            log(f"⚠️ 找不到对照账号 id={CONTROL_DRIVER_ID}")

    print("\n==================== 结论 ====================", flush=True)
    for r in results:
        print(f"--- {r['label']}：{r['name']}（driver {r['driver_id']}）", flush=True)
        for step in r.get("steps", []):
            print(f"  {step}", flush=True)
        if "token_code" in r:
            print(
                f"  upload_token → code={r.get('token_code')} "
                f"msg={r.get('token_msg')!r}",
                flush=True,
            )
        if "upload_ok" in r:
            print(f"  完整上传 → {'成功' if r['upload_ok'] else '失败'}", flush=True)
            if not r["upload_ok"]:
                print(f"    错误：{r.get('upload_error')}", flush=True)


def _row(d: dict[str, Any]) -> Driver:
    """把字典还原成 ORM 行（只为了过 ``_probe_one`` 的类型）。"""
    row = Driver()
    row.id = d["id"]
    row.name = d["name"]
    row.credential = d["credential"]
    row.device_type = d["device_type"]
    return row


if __name__ == "__main__":
    asyncio.run(main())
