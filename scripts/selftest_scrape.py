"""整理 + 刮削的**自测脚本**（造假文件跑全流程）。

用途：验证「整理 → 刮削」的四条路径在真实容器里跑得通：

1. **电影**（无季集号）→ ``<片名> (<年份>)/<片名> (<年份>).ext``，无 Season 目录
2. **多季目录**（同剧 S01 + S02）→ 两季各自落位，季 NFO 各一份
3. **字幕跟随**（同 stem 的 .ass / .srt）→ 随视频进媒体库、命名跟随新 stem
4. **move 模式**（复制后删源）→ 源文件消失、目标完好
5. **边界**：文件名解析不出来 → failed **点名**，不误动文件

**为什么造假文件**：识别与整理只依赖**文件名**，不依赖文件内容——造几 KB 的
假 mkv 就能跑完全流程，不必放真实媒体（几十 GB）。

这是**诊断工具**，不是应用代码（应用代码在 ``app/services/organize_service.py``、
``app/services/scrape_service.py``）。跑法::

    docker exec -i medialab-dev /opt/venv/bin/python - < scripts/selftest_scrape.py

脚本会**自清理**：删掉造的源文件、产物与数据库记录（不影响其它测试数据）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import httpx

BASE = os.environ.get("SELFTEST_BASE", "http://127.0.0.1:8080")
TOKEN = os.environ.get("WEB_TOKEN", "")
SOURCE_DIR = Path(os.environ.get("SELFTEST_SOURCE", "/app/test-media/_selftest"))
LIBRARY_DIR = Path(os.environ.get("SELFTEST_LIBRARY", "/app/library"))
DB_PATH = Path(os.environ.get("SELFTEST_DB", "/app/data/medialab.db"))

#: 造的测试数据 → 期望的产物目录名（TMDB 中文名 + 年份）
FIXTURES = {
    "movie": {
        "file": "Fight.Club.1999.1080p.mkv",
        "expect_dir": "搏击俱乐部 (1999)",
        "expect_file": "搏击俱乐部 (1999).mkv",
    },
    "multiseason": {
        "files": [
            "Game.of.Thrones.S01E01.1080p.mkv",
            "Game.of.Thrones.S02E01.1080p.mkv",
        ],
        "expect_dir": "权力的游戏 (2011)",
        "expect_files": [
            "Season 01/权力的游戏 - S01E01.mkv",
            "Season 02/权力的游戏 - S02E01.mkv",
        ],
    },
    "subtitle": {
        "file": "Inception.2010.1080p.mkv",
        "subtitles": ["Inception.2010.1080p.chs.ass", "Inception.2010.1080p.eng.srt"],
        "expect_dir": "盗梦空间 (2010)",
        "expect_file": "盗梦空间 (2010).mkv",
        "expect_subtitles": [
            "盗梦空间 (2010).chs.ass",
            "盗梦空间 (2010).eng.srt",
        ],
    },
}

#: move 模式的用例**单独造**——它在第 1 轮 copy 之后才出现，
#: 否则会被判重跳过（同一源文件只整理一次），move 根本没机会执行。
MOVE_FIXTURE = {
    "file": "Interstellar.2014.1080p.mkv",
    "expect_dir": "星际穿越 (2014)",
    "expect_file": "星际穿越 (2014).mkv",
}

#: 解析不出标题的文件名（纯画质标记）→ 必须 failed 点名
JUNK_FILE = "1080p.mkv"

_failures: list[str] = []


def check(condition: bool, message: str) -> None:
    """断言并记录失败（不中断——一次跑完看全部问题）。"""
    if condition:
        print(f"  ✓ {message}")
    else:
        print(f"  ✗ {message}")
        _failures.append(message)


def api(method: str, path: str, payload: dict | None = None) -> dict:
    """调面板 API（Bearer 认证）。"""
    headers = {"Authorization": f"Bearer {TOKEN}"}
    resp = httpx.request(
        method,
        f"{BASE}{path}",
        headers=headers,
        json=payload,
        timeout=900.0,
    )
    resp.raise_for_status()
    return resp.json()


def run_organize() -> dict:
    """触发整理作业，返回结果字典。"""
    data = api("POST", "/api/v1/scheduler/organize_files/run")
    return (data.get("data") or data).get("result") or {}


def make_fixtures() -> None:
    """造测试数据（假文件 + 字幕）。"""
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    payload = b"selftest-fake-media" * 64
    for fixture in FIXTURES.values():
        names = fixture.get("files") or [fixture["file"]]
        for name in names:
            (SOURCE_DIR / name).write_bytes(payload)
        for name in fixture.get("subtitles", []):
            (SOURCE_DIR / name).write_text("[V4+ Styles]\n", encoding="utf-8")
    (SOURCE_DIR / JUNK_FILE).write_bytes(payload)


def make_move_fixture() -> None:
    """造 move 模式的用例（第 3 轮才用）。"""
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    (SOURCE_DIR / MOVE_FIXTURE["file"]).write_bytes(b"selftest-fake-media" * 64)


def cleanup() -> None:
    """清理造的数据、产物与数据库记录。"""
    for fixture in (*FIXTURES.values(), MOVE_FIXTURE):
        expect_dir = LIBRARY_DIR / fixture["expect_dir"]
        if expect_dir.exists():
            for path in sorted(expect_dir.rglob("*"), reverse=True):
                path.unlink() if path.is_file() else path.rmdir()
            expect_dir.rmdir()
    if SOURCE_DIR.exists():
        for path in SOURCE_DIR.iterdir():
            path.unlink()
        SOURCE_DIR.rmdir()
    if DB_PATH.exists():
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute(
                "DELETE FROM organize_history WHERE source_path LIKE '%_selftest%'"
            )
            conn.execute(
                "DELETE FROM media_scrape WHERE scope_key LIKE '%搏击俱乐部%'"
                " OR scope_key LIKE '%盗梦空间%' OR scope_key LIKE '%星际穿越%'"
                " OR scope_key LIKE '%权力的游戏%'"
            )
            conn.commit()
        finally:
            conn.close()


def main() -> int:
    """跑自测。返回进程退出码。"""
    if not TOKEN:
        print("缺少 WEB_TOKEN（容器 env 里有）")
        return 2

    print(f"== 整理 + 刮削自测（{BASE}）==")
    cleanup()
    make_fixtures()

    # ---- copy 模式：电影 / 多季 / 字幕 ----
    print("\n[1] copy 模式（电影 / 多季 / 字幕跟随）")
    api(
        "PUT",
        "/api/scrape/config",
        {"organize_action": 0, "organize_enabled": 1, "scrape_enabled": 1},
    )
    result = run_organize()
    print(f"  summary: {result.get('summary')}")

    movie = FIXTURES["movie"]
    movie_dir = LIBRARY_DIR / movie["expect_dir"]
    check(
        (movie_dir / movie["expect_file"]).exists(),
        f"电影落盘：{movie['expect_dir']}/{movie['expect_file']}",
    )
    check(
        (movie_dir / movie["expect_file"].replace(".mkv", ".nfo")).exists(),
        "电影 NFO 生成",
    )
    check(
        not (movie_dir / "Season 01").exists(),
        "电影不该有 Season 目录",
    )

    multi = FIXTURES["multiseason"]
    multi_dir = LIBRARY_DIR / multi["expect_dir"]
    for expect in multi["expect_files"]:
        check((multi_dir / expect).exists(), f"多集落盘：{expect}")
    for season in ("Season 01", "Season 02"):
        check(
            (multi_dir / season / "season.nfo").exists(),
            f"季 NFO：{season}/season.nfo",
        )

    sub = FIXTURES["subtitle"]
    sub_dir = LIBRARY_DIR / sub["expect_dir"]
    for expect in sub["expect_subtitles"]:
        check((sub_dir / expect).exists(), f"字幕跟随：{expect}")

    # ---- 边界：解析不出来的文件名 ----
    print("\n[2] 边界：文件名解析失败必须点名")
    items = (result.get("steps") or [{}])[0].get("items") or []
    junk = [i for i in items if i.get("name") == JUNK_FILE]
    check(len(junk) == 1, "纯噪声文件名被记入明细")
    if junk:
        check(
            junk[0].get("status") == "fail" and "解析失败" in (junk[0].get("detail") or ""),
            f"噪声文件 failed 且点名：{junk[0].get('detail')}",
        )
    check(
        (SOURCE_DIR / JUNK_FILE).exists(),
        "识别失败的文件必须留在源目录（不误动）",
    )

    # ---- move 模式 ----
    print("\n[3] move 模式（复制后删源）")
    api("PUT", "/api/scrape/config", {"organize_action": 1})
    make_move_fixture()
    move = MOVE_FIXTURE
    source_file = SOURCE_DIR / move["file"]
    result = run_organize()
    print(f"  summary: {result.get('summary')}")
    move_dir = LIBRARY_DIR / move["expect_dir"]
    check((move_dir / move["expect_file"]).exists(), "move 目标文件存在")
    check(not source_file.exists(), "move 后源文件已删除")
    api("PUT", "/api/scrape/config", {"organize_action": 0})

    # ---- 清理 ----
    print("\n[4] 清理自测数据")
    cleanup()
    check(not SOURCE_DIR.exists(), "源目录已清理")
    check(
        not (LIBRARY_DIR / FIXTURES["movie"]["expect_dir"]).exists(),
        "产物已清理",
    )
    check(
        not (LIBRARY_DIR / MOVE_FIXTURE["expect_dir"]).exists(),
        "move 产物已清理",
    )

    print("\n== 结果 ==")
    if _failures:
        print(f"失败 {len(_failures)} 项：")
        for item in _failures:
            print(f"  - {item}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
