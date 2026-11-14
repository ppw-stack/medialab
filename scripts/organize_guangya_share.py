"""把光鸭盘「来自：分享」目录的内容归置进「影视」媒体库。

光鸭的分享内容落地在账号根的 ``来自：分享`` 目录（平台每次投放都重建这个
目录，名字可能变：早期叫 ``分享``），目录结构与媒体库一致::

    来自：分享/影视/电影/外语电影/被解救的姜戈 (2012)/poster.jpg
    → 影视/电影/外语电影/被解救的姜戈 (2012)/poster.jpg

本脚本把分享树**按路径映射**并进媒体库：先复制 → 校验 → 再删源。

跑法（生产容器，凭据不出容器）::

    # 只出统计，不动数据
    ssh unraid "docker exec -i medialab /opt/venv/bin/python -" \\
        < scripts/organize_guangya_share.py --dry-run
    # 实跑，冲突按「保留大的」
    ssh unraid "docker exec -i medialab /opt/venv/bin/python -" \\
        < scripts/organize_guangya_share.py --conflict keep-larger

安全边界：

- **删除只走 ``fs_delete``（进回收站），绝不碰 ``fs_clear_recycle_bin``**
  ——但平台清空回收站比 ``leftTime`` 字段说的 100 天短得多（实测上轮
  1.28 万项几天内被清空），**回收站不是长期保险**
- **不碰「来自：云添加」**（平台离线下载暂存区，与媒体库无关）
- Token 刷新走 app 自己的 ``persist_guangya_credential`` 写回（裸
  sqlite3 写 shfs 上的 WAL 库已实测不可靠），避免 ``refresh_token``
  轮换后账号掉线

三个**已踩过的坑**（改这段代码前先读）：

1. **「残留」判定只算文件，子目录一律不碰**。曾把「目录里还有条目就重删」
   做成目录级判定 → 把还没处理的 37 个剧目目录 + 160 多个 Season 目录连同
   内容删进回收站（内容没丢，但那一轮白干）。空目录只在最后自底向上删，
   且删前必须重列确认「无文件且无子目录」。
2. **写操作后立刻失效目录缓存**。曾用缓存的目录列表做「等复制落地」「等
   文件消失」的轮询判据 → 读到陈旧快照，每个目录白等 60 s、大目录白等
   900 s（40 分钟只跑 5 个目录）。本脚本的轮询一律用**新鲜列表**。
3. **``fs_copy`` / ``fs_delete`` 的 id 必须是字符串**。``fileIds`` 与
   ``parentId`` 传数字会得到 ``code=112 参数错误``；``fs_move`` 的类型
   签名写的是 ``Optional[int]``，按 int 传会失败。

冲突策略：

- ``keep-larger``（默认，与数据同步 ``conflict_policy=1``、迁移功能口径
  一致）：同名不同大小时**保留大的**——源更大就删掉目标那份小的、复制源的
  过来；目标更大就跳过复制、删掉源的副本
- ``keep``：一律保留目标（库里）那份，只删源的副本
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

#: 应用代码路径——默认容器内的 ``/app``；验证未部署的新代码时可用
#: ``MEDIALAB_APP_PATH=/tmp/newapp`` 指向注入的新代码（不改生产镜像）。
sys.path.insert(0, os.environ.get("MEDIALAB_APP_PATH", "/app"))

from app.adapters.drivers.base import DriverError
from app.core.config import get_settings
from app.db.models import Driver
from app.db.session import init_db, session_scope
from app.services.driver_factory import build_driver
from app.services.migration import persist_guangya_credential
from sqlalchemy import select

#: 分享根目录的候选名（平台投放时用过两个）
SHARE_ROOT_NAMES = ("来自：分享", "分享")
#: 媒体库根目录名
TARGET_ROOT_NAME = "影视"
#: 平台离线下载暂存区——**跳过**，与媒体库无关
SKIP_ROOT_NAMES = ("来自：云添加",)

#: 批量复制的文件数（实测光鸭接受 20/批）
COPY_CHUNK = 20
#: 批量删除的文件数
DELETE_CHUNK = 50
#: ≥ 这个大小才做 gcid（内容哈希）校验
GCID_MIN = 1024 * 1024
#: 落地等待的最低/最高预算（秒）
LANDING_MIN_SEC = 120
LANDING_MAX_SEC = 1200
#: 保守的云端拷贝速率（用于算落地等待预算）
BYTES_PER_SEC = 30 * 1024 * 1024
#: 删源后等「文件消失」的轮次上限（× 2s）
DELETE_WAIT_ROUNDS = 15
#: 全量列表翻页上限（防御服务端忽略 page 参数）
MAX_LIST_PAGES = 1000
#: 全量列表的每页条数（实测 1000 生效）
LIST_PAGE_SIZE = 1000


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def chunks(items: list, n: int):
    for i in range(0, len(items), n):
        yield items[i : i + n]


async def with_retry(fn, *args, tries: int = 4, **kwargs):
    """调用失败重试——**128 容器 DNS 偶发解析失败**是已知问题。

    实测报错是 ``httpx.ConnectError: [Errno -3] Temporary failure in name
    resolution``，过几秒就好。驱动层的 ``_call`` 只做 token 刷新与错误映射，
    **不重试网络失败**——不自己兜住的话，一个目录失败会让整批整理少一块。
    """
    last: Exception | None = None
    for i in range(tries):
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            last = exc
            wait = 3 * (i + 1)
            log(
                f"    ⚠️ 调用失败（第 {i + 1}/{tries} 次）：{str(exc)[:80]}"
                f" → {wait}s 后重试"
            )
            await asyncio.sleep(wait)
    raise last


async def iter_all_with_retry(driver, res_type: int, tries: int = 4) -> list[dict]:
    """全账号列表（带重试）。失败就从头重来——索引重建成本很低。"""
    last: Exception | None = None
    for i in range(tries):
        try:
            items: list[dict] = []
            async for item in driver._iter_all_items(res_type=res_type):
                items.append(item)
            return items
        except Exception as exc:
            last = exc
            wait = 3 * (i + 1)
            log(f"  ⚠️ 全量列表失败（第 {i + 1}/{tries} 次）：{str(exc)[:80]} → {wait}s 后重试")
            await asyncio.sleep(wait)
    raise last


class AccountIndex:
    """全账号索引：目录表 + 按父目录分组的文件表。

    **一次性用全账号批量列表建好**（``parent_id="*"`` + ``res_type`` 过滤 +
    ``fullParentIds`` 链）——生产实测 788 个目录 / 15107 个文件 ≈ 3 个请求，
    比逐目录递归（上千请求）快两个数量级。

    只读不写：写操作（建目录/复制/删除）后由调用方显式更新本表。
    """

    def __init__(self) -> None:
        self.dir_name: dict[str, str] = {}
        self.dir_parent: dict[str, str] = {}
        self.files_by_parent: dict[str, list[tuple[str, int, str]]] = {}

    def add_dir(self, file_id: str, name: str, parent_id: str) -> None:
        self.dir_name[file_id] = name
        self.dir_parent[file_id] = parent_id

    def path_under(self, root_id: str, node_id: str) -> list[str] | None:
        """从 ``root_id`` 到 ``node_id`` 的目录名链（不含 root_id 本身）。

        走 ``fullParentIds`` 反查的父指针；链断了（有 fileId 不在表里）返回
        ``None``——**不做静默兜底**：宁可少处理一个文件，也不要把文件归到
        错误的目录。
        """
        segs: list[str] = []
        cur = node_id
        seen: set[str] = set()
        while cur and cur != root_id:
            if cur in seen:
                return None  # 环 → 索引坏了
            seen.add(cur)
            name = self.dir_name.get(cur)
            if name is None:
                return None
            segs.append(name)
            cur = self.dir_parent.get(cur, "")
        if cur != root_id:
            return None
        return list(reversed(segs))

    def resolve(self, base_id: str, rel: list[str]) -> str | None:
        """按名字链从 ``base_id`` 往下解析目录 id（不创建）。"""
        cur = base_id
        for seg in rel:
            nxt = self.find_child(cur, seg)
            if nxt is None:
                return None
            cur = nxt
        return cur

    def find_child(self, parent_id: str, name: str) -> str | None:
        for fid, nm in self.dir_name.items():
            if nm == name and self.dir_parent.get(fid) == parent_id:
                return fid
        return None

    def children_of(self, parent_id: str) -> list[str]:
        return [f for f, p in self.dir_parent.items() if p == parent_id]


async def load_index(driver) -> AccountIndex:
    """拉全账号目录 + 文件，建索引。"""
    idx = AccountIndex()

    for item in await iter_all_with_retry(driver, res_type=2):
        fid = str(item.get("fileId") or "")
        name = str(item.get("fileName") or "")
        if not fid or not name:
            continue
        chain = [x for x in str(item.get("fullParentIds") or "").split("/") if x]
        idx.add_dir(fid, name, chain[-1] if chain else "")
    log(f"全账号目录 {len(idx.dir_name)} 个")

    for item in await iter_all_with_retry(driver, res_type=1):
        fid = str(item.get("fileId") or "")
        name = str(item.get("fileName") or "")
        if not fid or not name:
            continue
        chain = [x for x in str(item.get("fullParentIds") or "").split("/") if x]
        parent = chain[-1] if chain else ""
        idx.files_by_parent.setdefault(parent, []).append(
            (name, int(item.get("fileSize") or 0), fid)
        )
    log(f"全账号文件 {sum(len(v) for v in idx.files_by_parent.values())} 个")
    return idx


async def list_dir(driver, dir_id: str) -> dict[str, tuple[int, str]]:
    """列目录 → ``{文件名: (字节数, fileId)}``。**每次都重新拉**（不缓存）。"""
    out: dict[str, tuple[int, str]] = {}
    for entry in await with_retry(driver.list_dir_by_cid, dir_id):
        if entry.is_dir:
            continue
        out[entry.name] = (entry.size, entry.remote_id)
    return out


async def ensure_dir(
    driver, idx: AccountIndex, base_id: str, rel: list[str], dry_run: bool = False
) -> str | None:
    """逐层确保 ``rel`` 相对 ``base_id`` 存在，返回最终 fileId。

    建完把新目录登记进索引——否则后面的路径解析找不到它。

    **干跑时绝不建目录**：解析不到就返回 ``None``（调用方按「目标目录还不
    存在」处理）。
    """
    cur = base_id
    for name in rel:
        nxt = idx.find_child(cur, name)
        if nxt is None:
            if dry_run:
                return None
            new_id, created = await with_retry(driver.create_dir, name, cur)
            idx.add_dir(new_id, name, cur)
            log(f"    [建目录] {name} id={new_id}（新建={created}）")
            cur = new_id
        else:
            cur = nxt
    return cur


async def _delete_batch(driver, file_ids: list[str]) -> None:
    """批量删除。

    **优先用驱动层的公开方法**（``delete_files``，内部统一把 id 转成
    字符串）——旧镜像里还没有这个方法，故留一条 ``_call`` 兜底路径
    （同样强制转字符串）。两条路径都不碰第三方客户端那个
    ``Optional[int]`` 的签名。
    """
    fn = getattr(driver, "delete_files", None)
    if fn is not None:
        await fn(file_ids)
        return
    payload = await driver._call("fs_delete", [str(fid) for fid in file_ids])
    code = payload.get("code")
    if code not in (None, 0):
        raise DriverError(f"光鸭删除文件失败：{payload.get('msg') or payload}")


async def _copy_batch(driver, file_ids: list[str], parent_id: str) -> None:
    """批量复制（同 :func:`_delete_batch` 的新旧兼容说明）。"""
    fn = getattr(driver, "copy_files", None)
    if fn is not None:
        await fn(file_ids, parent_id=parent_id)
        return
    payload = await driver._call(
        "fs_copy", [str(fid) for fid in file_ids], parent_id=str(parent_id)
    )
    code = payload.get("code")
    if code not in (None, 0):
        raise DriverError(f"光鸭复制文件失败：{payload.get('msg') or payload}")


async def _delete_all(driver, file_ids: list[str]) -> None:
    """批量删除（进回收站）。**id 必须是字符串**。"""
    for chunk in chunks(file_ids, DELETE_CHUNK):
        try:
            await with_retry(_delete_batch, driver, list(chunk))
        except DriverError as exc:
            log(f"    ⚠️ 删除批次失败：{exc}")


async def _copy_all(driver, file_ids: list[str], parent_id: str) -> bool:
    """批量复制到 ``parent_id``。**id 与 parent 都必须是字符串**。"""
    ok = True
    for chunk in chunks(file_ids, COPY_CHUNK):
        try:
            await with_retry(_copy_batch, driver, list(chunk), parent_id)
        except DriverError as exc:
            log(f"    ⚠️ 复制批次失败：{exc}")
            ok = False
    return ok


async def wait_landing(driver, target_dir: str, expected: dict[str, int]) -> bool:
    """等复制落地：目标目录里出现同名同大小的文件。

    **每次重新拉目录**（不读缓存）——否则会读到陈旧快照空等到超时。
    """
    if not expected:
        return True
    budget = max(
        LANDING_MIN_SEC,
        min(LANDING_MAX_SEC, sum(expected.values()) / BYTES_PER_SEC + 90),
    )
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        cur = await list_dir(driver, target_dir)
        if all(n in cur and cur[n][0] == s for n, s in expected.items()):
            return True
        await asyncio.sleep(2)
    return False


async def wait_gone(driver, dir_id: str, names: set[str]) -> list[str]:
    """等目录里的这些**文件**消失，返回还没消失的。

    **只等文件，不等目录变空**——子目录本来就还在，等目录变空会白等 60 s。
    """
    deadline_rounds = DELETE_WAIT_ROUNDS
    for _ in range(deadline_rounds):
        cur = await list_dir(driver, dir_id)
        left = {n for n in names if n in cur}
        if not left:
            return []
        await asyncio.sleep(2)
    cur = await list_dir(driver, dir_id)
    return sorted(n for n in names if n in cur)


async def file_gcid(driver, file_id: str) -> str:
    payload = await with_retry(driver._call, "fs_detail", str(file_id))
    info = (payload.get("data") or {}).get("fileInfo") or {}
    return str(info.get("gcid") or info.get("md5") or "")


async def process_source_dir(
    driver,
    idx: AccountIndex,
    share_root: str,
    target_root: str,
    rel_src: list[str],
    src_files: dict[str, tuple[int, str]],
    conflict: str,
    dry_run: bool,
    skip_gcid: bool,
    stats: dict,
    conflicts: list[str],
    failures: list[str],
) -> None:
    """处理一个源目录里的文件。

    源路径 → 目标路径：剥掉前导的 ``分享根`` 与 ``影视`` 段（分享树的
    结构与媒体库一致，剥完就是媒体库里的相对路径）。
    """
    rel_tgt = list(rel_src)
    while rel_tgt and rel_tgt[0] in (SHARE_ROOT_NAMES[0], SHARE_ROOT_NAMES[1], TARGET_ROOT_NAME):
        rel_tgt = rel_tgt[1:]

    tgt_dir = await ensure_dir(driver, idx, target_root, rel_tgt, dry_run=dry_run)
    if tgt_dir is None:
        # 干跑：目标目录还不存在 → 该目录下全是「待复制」
        stats["dirs"] += 1
        stats["files"] += len(src_files)
        stats["missing"] += len(src_files)
        stats["bytes"] += sum(sz for sz, _ in src_files.values())
        for name, (size, _fid) in src_files.items():
            stats.setdefault("missing_list", []).append(
                f"/影视/{'/'.join(rel_tgt)}/{name}\t{size}"
            )
        return
    tgt_files = await list_dir(driver, tgt_dir)

    to_copy: list[tuple[str, int, str]] = []
    dups: list[tuple[str, int, str]] = []
    overwrite: list[tuple[str, int, str]] = []   # 源更大 → 覆盖目标
    drop_src: list[tuple[str, int, str]] = []    # 目标更大 → 删源不复制

    src_dir = _src_dir_id(idx, share_root, rel_src)
    for name, (size, fid) in src_files.items():
        t = tgt_files.get(name)
        if t is None:
            to_copy.append((name, size, fid))
        elif t[0] == size:
            dups.append((name, size, fid))
        else:
            conflicts.append(
                f"/影视/{'/'.join(rel_tgt)}/{name}  源={size}B 目标={t[0]}B"
            )
            if conflict == "keep-larger" and size > t[0]:
                overwrite.append((name, size, fid))
            else:
                drop_src.append((name, size, fid))

    if not (to_copy or dups or overwrite or drop_src):
        return

    stats["dirs"] += 1
    stats["files"] += len(to_copy) + len(dups) + len(overwrite) + len(drop_src)

    if dry_run:
        stats["missing"] += len(to_copy) + len(overwrite)
        stats["bytes"] += sum(s for _, s, _ in to_copy + overwrite)
        stats["dup"] += len(dups)
        stats["conflict"] += len(drop_src) + len(overwrite)
        for name, size, _fid in to_copy + overwrite:
            stats.setdefault("missing_list", []).append(
                f"/影视/{'/'.join(rel_tgt)}/{name}\t{size}"
            )
        return

    # ① 覆盖场景：先删目标那份小的（否则复制会落成 "xxx(1)"）
    if overwrite:
        old_ids = [tgt_files[name][1] for name, _, _ in overwrite]
        await _delete_all(driver, old_ids)
        left = await wait_gone(driver, tgt_dir, {tgt_files[n][1] for n, _, _ in overwrite})
        if left:
            failures.append(f"/影视/{'/'.join(rel_tgt)} 目标旧文件删不掉：{left}")

    # ② 复制
    pending = to_copy + overwrite
    expected = {name: size for name, size, _ in pending}
    if pending:
        await _copy_all(driver, [fid for _, _, fid in pending], tgt_dir)
        if not await wait_landing(driver, tgt_dir, expected):
            failures.append(
                f"/影视/{'/'.join(rel_tgt)} 复制未全部落地："
                f"{[n for n in expected if n not in await list_dir(driver, tgt_dir)]}"
            )

    # ③ 校验（全部比字节数；≥1MB 再比 gcid）
    cur = await list_dir(driver, tgt_dir)
    verified: list[str] = []
    for name, size, fid in pending:
        t = cur.get(name)
        if t is None:
            failures.append(f"/影视/{'/'.join(rel_tgt)}/{name} 未落地")
            continue
        if t[0] != size:
            failures.append(f"/影视/{'/'.join(rel_tgt)}/{name} 大小不符 {size}→{t[0]}")
            continue
        if size >= GCID_MIN and not skip_gcid:
            src_hash = await file_gcid(driver, fid)
            dst_hash = await file_gcid(driver, t[1])
            if not src_hash or src_hash != dst_hash:
                failures.append(f"/影视/{'/'.join(rel_tgt)}/{name} 内容哈希不符")
                continue
        verified.append(fid)
        stats["copied"] += 1
        stats["bytes"] += size

    # ④ 删源：校验通过的 + 重复的 + 目标更大的冲突项
    del_ids = verified + [fid for _, _, fid in dups] + [fid for _, _, fid in drop_src]
    del_names = {n for n, _, _ in to_copy + overwrite + dups + drop_src}
    if del_ids:
        await _delete_all(driver, del_ids)
        left = await wait_gone(driver, src_dir, del_names)
        if left:
            log(f"    [文件残留] {left[:5]}")
            await _delete_all(driver, [src_files[n][1] for n in left if n in src_files])
            await asyncio.sleep(3)
    stats["dup"] += len(dups)
    stats["conflict"] += len(drop_src) + len(overwrite)


def _src_dir_id(idx: AccountIndex, share_root: str, rel_src: list[str]) -> str:
    """分享树里某个相对路径对应的目录 id（取最后一级的 id）。"""
    cur = share_root
    for seg in rel_src:
        nxt = idx.find_child(cur, seg)
        if nxt is None:
            return ""
        cur = nxt
    return cur


async def prune_empty_dirs(
    driver,
    idx: AccountIndex,
    dir_id: str,
    rel: list[str],
    *,
    is_root: bool = False,
    keep_root: bool = False,
) -> int:
    """自底向上删空目录（**只删确实没有文件也没有子目录的**）。

    ``is_root`` + ``keep_root`` 控制分享根目录是否保留——平台会按需重建
    它，删掉也行，留着也行。
    """
    removed = 0
    for child in idx.children_of(dir_id):
        removed += await prune_empty_dirs(
            driver, idx, child, [*rel, idx.dir_name.get(child, "?")], keep_root=keep_root
        )
    if is_root and keep_root:
        return removed
    cur = await list_dir(driver, dir_id)
    if not cur and not idx.children_of(dir_id):
        try:
            await with_retry(_delete_batch, driver, [dir_id])
            ok = True
        except DriverError:
            ok = False
        if ok:
            idx.dir_name.pop(dir_id, None)
            idx.dir_parent.pop(dir_id, None)
            removed += 1
        await asyncio.sleep(0.3)
    return removed


async def main() -> None:
    ap = argparse.ArgumentParser(description="把光鸭「来自：分享」归置进「影视」媒体库")
    ap.add_argument("--driver-id", type=int, default=2, help="光鸭驱动 id（默认 2 = 主号光鸭盘）")
    ap.add_argument("--conflict", choices=("keep-larger", "keep"), default="keep-larger")
    ap.add_argument("--dry-run", action="store_true", help="只出统计，不动数据")
    ap.add_argument("--keep-share-root", action="store_true", help="保留空的分享根目录（默认删）")
    ap.add_argument("--no-gcid", action="store_true", help="跳过 gcid 校验（更快）")
    args = ap.parse_args()

    settings = get_settings()
    init_db(settings)

    async with session_scope() as db:
        row = (
            await db.execute(select(Driver).where(Driver.id == args.driver_id))
        ).scalar_one_or_none()
    if row is None:
        raise SystemExit(f"❌ 驱动 {args.driver_id} 不存在")
    log(f"驱动 {row.id} {row.name}（{row.type}）")

    async def on_refresh(cred) -> None:
        await persist_guangya_credential(row.id, cred)

    driver = build_driver(row.type, row.credential, row.device_type, on_token_refresh=on_refresh)

    idx = await load_index(driver)

    share_root = ""
    for name in SHARE_ROOT_NAMES:
        cand = idx.find_child("", name)
        if cand:
            share_root = cand
            log(f"分享根：{name} id={cand}")
            break
    if not share_root:
        raise SystemExit("❌ 账号根下没有「来自：分享」/「分享」目录")
    target_root = idx.find_child("", TARGET_ROOT_NAME)
    if not target_root:
        raise SystemExit(f"❌ 账号根下没有「{TARGET_ROOT_NAME}」目录")
    log(f"媒体库根：{TARGET_ROOT_NAME} id={target_root}")

    # 分享树里**含文件**的目录——直接从全账号索引取，不用逐目录列
    # （逐目录列 97 个目录要 97 次请求；索引里已经有了）
    share_dirs: list[tuple[str, list[str], dict[str, tuple[int, str]]]] = []
    for parent_id, files in idx.files_by_parent.items():
        rel = idx.path_under(share_root, parent_id)
        if rel is None:
            continue
        share_dirs.append((parent_id, rel, {n: (sz, fid) for n, sz, fid in files}))
    share_dirs.sort(key=lambda x: x[1])
    log(f"分享树：{len(share_dirs)} 个含文件的目录")

    stats = {
        "dirs": 0, "files": 0, "copied": 0, "dup": 0, "conflict": 0,
        "bytes": 0, "missing": 0,
    }
    conflicts: list[str] = []
    failures: list[str] = []
    t0 = time.monotonic()

    for _dir_id, rel, src_files in share_dirs:
        await process_source_dir(
            driver, idx, share_root, target_root, rel, src_files,
            args.conflict, args.dry_run, args.no_gcid, stats, conflicts, failures,
        )
        if not args.dry_run:
            log(
                f"  [目录 {stats['dirs']}] {'/'.join(rel)} → 累计复制 {stats['copied']}"
                f"（{stats['bytes'] / 1024**2:.1f}MB）重复 {stats['dup']}"
                f" 冲突 {stats['conflict']} 失败 {len(failures)}"
                f" | 用时 {int(time.monotonic() - t0)}s"
            )

    log("\n===== 汇总 =====")
    if args.dry_run:
        log(f"[干跑] 待复制 {stats['missing']} 个 / {stats['bytes'] / 1024**2:.1f} MB")
        log(f"[干跑] 同名同大小 {stats['dup']} 个")
        log(f"[干跑] 同名不同大小 {stats['conflict']} 个")
        log("[干跑] 待复制清单:")
        for item in stats.get("missing_list", []):
            log("   " + item)
    else:
        log(f"复制并校验通过 {stats['copied']} 个 / {stats['bytes'] / 1024**2:.1f} MB")
        log(f"同名同大小跳过复制 {stats['dup']} 个")
        log(f"同名不同大小 {stats['conflict']} 个")
        log(f"失败 {len(failures)} 个")
        if not args.keep_share_root:
            removed = await prune_empty_dirs(
                driver, idx, share_root, [SHARE_ROOT_NAMES[0]],
                is_root=True, keep_root=args.keep_share_root,
            )
            log(f"清空空目录 {removed} 个")

    if conflicts:
        log(f"\n冲突清单（共 {len(conflicts)} 条，前 40）:")
        for c in conflicts[:40]:
            log("   " + c)
    if failures:
        log(f"\n失败清单（共 {len(failures)} 条，前 40）:")
        for f in failures[:40]:
            log("   " + f)


if __name__ == "__main__":
    asyncio.run(main())
