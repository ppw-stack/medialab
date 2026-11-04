"""离线下载的**实测验证脚本**（诊断工具）。

用途：验证「光鸭的离线下载器能不能拉本机的 HTTP 服务」，并测出**实际
速率**——这是决定要不要用离线下载的唯一依据。

**这是诊断工具，不是应用代码**（应用代码在
``app/services/offline_download.py``）。跑法：

    # 在页面上配好三项（配置已落库，读取走 app_env_settings）
    docker exec -i medialab /opt/venv/bin/python - < scripts/verify_offline.py

    # 或在本地（用 dev 的 .env；env 只作**首次启动的默认值**，之后以 DB 为准）
    OFFLINE_HMAC_SECRET=... OFFLINE_PUBLIC_BASE=... \\
    TEST_REL='电影/xxx.mkv' uv run python scripts/verify_offline.py

**实测结论见 ``docs/offline-download.md``**：功能可行（文件完整落地、
md5 一致），但速率 ~0.75 MB/s（比直传慢 4.4 倍）——光鸭对随机分片的
重试吃掉了 73% 的带宽。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

# 容器里 app 装在 venv 的 site-packages（不是 /app/app）
for _p in ("/opt/venv/lib/python3.12/site-packages", "/app"):
    if _p not in sys.path:
        sys.path.append(_p)


def _hmac_for(secret: str, rel: str) -> str:
    return hmac.new(secret.encode(), rel.encode(), hashlib.sha256).hexdigest()[:32]


async def _main() -> None:
    from app.core.config import get_settings
    from app.db.models import Driver
    from app.db.session import init_db, session_scope
    from app.services.driver_factory import build_driver
    from app.services.offline_config import current_config
    from sqlalchemy import select

    settings = get_settings()

    settings.db_path = Path("/app/data/medialab.db")
    if not settings.db_path.exists():
        settings.db_path = Path(settings.db_path)
    init_db(settings)

    # 初始化环境变量配置（读 DB 里的 OFFLINE_*）——必须在 init_db 之后。
    # 配置落库后，真源是 app_env_settings（页面上改的）——env 只是首次默认值。
    from app.services.env_settings import init_env_settings_async, set_env_settings

    set_env_settings(await init_env_settings_async())

    cfg = current_config()
    secret = os.environ.get("OFFLINE_HMAC_SECRET") or cfg.secret
    public_base = os.environ.get("OFFLINE_PUBLIC_BASE") or cfg.public_base
    root = os.environ.get("OFFLINE_FILES_ROOT") or cfg.files_root
    test_rel = os.environ.get("TEST_REL", "")

    if not secret or not public_base or not root:
        print("❌ 缺 OFFLINE_HMAC_SECRET / OFFLINE_PUBLIC_BASE / OFFLINE_FILES_ROOT")
        return
    if not test_rel:
        print("❌ 缺 TEST_REL（要传的文件的相对路径）")
        return

    async with session_scope() as session:
        row = (
            await session.execute(select(Driver).where(Driver.type == "guangya"))
        ).scalar_one_or_none()
        if row is None:
            print("❌ 没有光鸭存储源")
            return
        driver_id = row.id
        cred = row.credential
        print(f"✅ 光鸭 driver_id={driver_id} name={row.name}")

    async def _on_refresh(new_cred: object) -> None:
        from app.adapters.drivers.guangya_credential import SCHEME
        from app.services.driver_service import store_credential

        async with session_scope() as s:
            await store_credential(s, driver_id, new_cred.to_json(), scheme=SCHEME)
        print("🔄 Token 已刷新写回")

    driver = build_driver("guangya", cred, None, on_token_refresh=_on_refresh)

    from urllib.parse import quote

    url = f"{public_base.rstrip('/')}/offline-files/{_hmac_for(secret, test_rel)}/{quote(test_rel)}"
    size = os.path.getsize(os.path.join(root, test_rel))
    print("\n📤 提交离线任务")
    print(f"   文件: {test_rel}")
    print(f"   大小: {size:,} bytes ({size / 1024**3:.2f} GB)")
    print(f"   URL : {url[:110]}...")

    t0 = time.time()
    try:
        task_id = await driver.create_offline_task(url, "")
    except Exception as exc:
        print(f"❌ 提交失败: {type(exc).__name__}: {exc}")
        return
    print(f"✅ 提交成功 taskId={task_id}（耗时 {time.time() - t0:.1f}s）")

    print("\n⏳ 轮询任务状态（每 10s）...")
    deadline = time.time() + 2400
    while time.time() < deadline:
        try:
            active = await driver.list_active_offline_tasks()
            failed = await driver.list_failed_offline_tasks()
        except Exception as exc:
            print(f"   ⚠️ 查询失败: {exc}")
            await asyncio.sleep(10)
            continue

        mine_a = [a for a in active if str(a.get("taskId")) == task_id]
        mine_f = [f for f in failed if str(f.get("taskId")) == task_id]

        if mine_f:
            print(f"❌ 任务失败: {json.dumps(mine_f[0], ensure_ascii=False)[:800]}")
            return
        if not mine_a:
            el = time.time() - t0
            print(f"✅ 任务结束（用时 {el:.0f}s，速率 {size / 1024**2 / max(el, 1):.2f} MB/s）")
            try:
                entries = await driver.list_entries("")
                print(f"   目标根目录 {len(entries)} 个文件")
                for name, (sz, _fid) in list(entries.items())[:8]:
                    print(f"     - {name} ({sz:,} bytes)")
            except Exception as exc:
                print(f"   ⚠️ 列目录失败: {exc}")
            return

        info = mine_a[0]
        el = time.time() - t0
        print(
            f"   progress={info.get('progress')} 用时 {el:.0f}s"
            f"（速率 {size / 1024**2 / max(el, 1):.2f} MB/s）"
        )
        await asyncio.sleep(10)

    print("⏱️ 超时")


if __name__ == "__main__":
    asyncio.run(_main())
