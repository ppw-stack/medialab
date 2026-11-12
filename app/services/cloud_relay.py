"""光鸭 → 光鸭 **跨账号转存**引擎（一次性手动搬运）。

**原理**：A 账号**分享** → B 账号**保存**（``restore_share``）。

服务端内部复制——2026-11-12 生产实测：100MB 文件 **7 秒落盘**
（≈14 MB/s），不占本机带宽、不占本地磁盘、不吃云添加额度
（云添加每天只有 3 次/账号，不适合批量）。

**提交单元 = 同一源目录下的文件批次**（≤500 个/批，实测服务端上限）。
**冲突判定逐文件**：

- 同名 + 同大小 → 跳过（``skipped_existing``）
- 同名 + 不同大小 → 记冲突（``conflicts``），**不覆盖、不改名**

⚠️ **预检是必须的**：服务端保存同名文件时**不报错也不覆盖**，而是自动
改名 ``xxx(1)``（2026-11-12 实测）。不预检的话重跑一次就多一份垃圾副本，
反复重跑会把目标目录堆满。

**协议要点**（全部实测，详见 :mod:`app.adapters.drivers.guangya`）：

- 分享链路**必须匿名**（取令牌 / 列内容）；保存**必须带 Bearer**
- ``shareId`` 必须拼账号 id 后缀（``{shareId}_{uid}``）
- 建分享有**频率限制**——同账号连建会被判「分享已失效」（``201``），
  故有节流闸门 + 退避重试
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.adapters.drivers.base import CloudFile, DriverError
from app.adapters.drivers.guangya import (
    SHARE_CREATE_FILE_LIMIT,
    SHARE_SAVE_FILE_LIMIT,
    ShareInfo,
)
from app.core.logging import get_logger
from app.db.models import Driver
from app.db.session import session_scope
from app.services import naming
from app.services.driver_factory import build_driver
from app.services.migration import (
    DISC_DIRS,
    DISC_EXTS,
    FILTER_EXT,
    FILTER_LARGE,
    FILTER_SMALL,
    persist_guangya_credential,
)

if TYPE_CHECKING:  # pragma: no cover - 仅类型
    from app.services.relay_jobs import RelayJob

logger = get_logger(__name__)

#: 建分享的**最小间隔**（秒）——防「同账号连续建分享」被限流。
#:
#: 2026-11-12 实测：连着建 3 个分享（间隔 30~60s）有被判「已失效」的，
#: 间隔 90s 以上稳定。默认取 15s——**每个源目录都要建一次分享**（目录
#: 多时按 30s 要半小时），撞到 201 时会自动退避，故短间隔是安全的。
DEFAULT_SUBMIT_INTERVAL_SEC = 15.0

#: 单批最多保存多少个文件。
#:
#: ⚠️ **建分享的上限更严**（:data:`SHARE_CREATE_FILE_LIMIT` = 20）——
#: 一次建分享最多带 20 个文件，而一次保存能吃 500 个。故**瓶颈是建分享**，
#: 批次按建分享的上限切（2026-11-12 生产踩过：按 500 切 → 一遇到多文件
#: 的目录就报「分享目录文件数量超过限制」整轮失败）。
BATCH_SIZE = SHARE_CREATE_FILE_LIMIT

#: 单次「保存分享」的文件数上限（服务端 500）——留作上界校验。
SAVE_BATCH_LIMIT = SHARE_SAVE_FILE_LIMIT

#: 保存后校验落盘的轮询间隔 / 超时（秒）。
VERIFY_INTERVAL_SEC = 3.0
VERIFY_TIMEOUT_SEC = 300.0

#: 建分享撞限流（``201``）时的重试次数与退避上限。
MAX_SHARE_ATTEMPTS = 5
MAX_BACKOFF_SEC = 300.0

#: 遍历源树 / 准备目标目录 / 单批保存的**网络抖动**重试次数与退避基数。
#:
#: 生产容器 DNS 偶发 ``Temporary failure in name resolution``（已知问题）。
#: 转存是长跑任务（几十个目录、几十分钟），不重试的话一次抖动就毁掉
#: 整轮——2026-11-12 生产实测踩到过。
TRAVERSE_ATTEMPTS = 3
DIR_ATTEMPTS = 3
BATCH_ATTEMPTS = 3
RETRY_DELAY_SEC = 5.0


def _is_transient(exc: DriverError) -> bool:
    """是不是「可以重试」的瞬时故障（网络抖动）。

    只认 ``kind == "network"``——业务错误（限流 / 参数错 / 数量超限）
    各有各的处理方式，别混进重试。
    """
    return exc.kind == "network"


async def _sleep(seconds: float) -> None:
    """退避用的 sleep。

    **抽成函数是为了可测**——测试把它替换成瞬时版即可，不必真等。
    （直接 ``asyncio.sleep`` 无法单独替换：``cloud_relay.asyncio`` 就是全局
    asyncio 模块，改它会污染整个测试进程。）
    """
    await asyncio.sleep(seconds)


class RelayError(RuntimeError):
    """转存无法开始（配置错误 / 驱动不可用）。"""


class RelayBrowseError(Exception):
    """目录浏览失败（带 HTTP 状态码，供 API 层翻译）。"""

    def __init__(self, message: str, *, status_code: int = 400, kind: str = "invalid"):
        super().__init__(message)
        self.status_code = status_code
        self.kind = kind


async def browse_dirs(driver_id: int, parent_id: str = "") -> list[tuple[str, str]]:
    """列出光鸭目录下的子目录（按 fileId 逐层浏览）。

    返回 ``[(remote_id, name), ...]``——只含目录。

    Raises:
        RelayBrowseError: 存储源不存在（404）/ 类型不符（400）/ 上游失败（400）。
    """
    async with session_scope() as db:
        row = (
            await db.execute(select(Driver).where(Driver.id == driver_id))
        ).scalar_one_or_none()
    if row is None:
        raise RelayBrowseError(
            f"存储源不存在：{driver_id}", status_code=404, kind="not_found"
        )
    if row.type != "guangya":
        raise RelayBrowseError(
            f"转存只支持光鸭盘（当前：{row.type}）",
            status_code=400,
            kind="config",
        )

    async def _on_refresh(cred: Any) -> None:
        await persist_guangya_credential(driver_id, cred)

    driver: Any = build_driver(
        row.type,
        row.credential,
        row.device_type,
        on_token_refresh=_on_refresh,
    )
    try:
        entries = await driver.list_dir_by_cid(parent_id or "")
    except DriverError as exc:
        raise RelayBrowseError(str(exc), status_code=400, kind="upstream") from exc
    return [(e.remote_id, e.name) for e in entries if e.is_dir]


@dataclass
class _Group:
    """一个待处理的源目录：相对源根的路径 + 其下的文件。"""

    rel_dir: str
    files: list[CloudFile] = field(default_factory=list)


class _ShareGate:
    """建分享的节流闸门：保证两次建分享之间至少间隔 N 秒。

    **为什么要全局**（而不是每个 worker 各自 sleep）：worker 是并发的，
    各自 sleep 仍可能撞在同一时刻。闸门用一把锁串行化「排队 + 计时」。
    """

    def __init__(self, interval: float) -> None:
        self._interval = max(0.0, float(interval))
        self._next_at: float = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> float:
        """等到可以建分享为止，返回本次实际等待的秒数。"""
        if self._interval <= 0:
            return 0.0
        async with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_at - now)
            if delay > 0:
                await _sleep(delay)
            self._next_at = time.monotonic() + self._interval
            return delay


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


async def run_relay(job: RelayJob) -> None:
    """跑一轮转存（由 :mod:`app.services.relay_jobs` 在后台调用）。"""
    params = job.params

    async with session_scope() as db:
        source = (
            await db.execute(select(Driver).where(Driver.id == params.source_driver_id))
        ).scalar_one_or_none()
        target = (
            await db.execute(select(Driver).where(Driver.id == params.target_driver_id))
        ).scalar_one_or_none()

    if source is None or target is None:
        raise RelayError("存储源不存在")
    if source.type != "guangya" or target.type != "guangya":
        raise RelayError(
            f"源与目标都必须是光鸭盘（当前：{source.type} → {target.type}）"
        )
    if source.id == target.id:
        raise RelayError("源与目标不能是同一个光鸭账号")

    async def _on_src_refresh(cred: Any) -> None:
        await persist_guangya_credential(params.source_driver_id, cred)

    async def _on_tgt_refresh(cred: Any) -> None:
        await persist_guangya_credential(params.target_driver_id, cred)

    # ⚠️ 标注 Any：``build_driver`` 返回 :class:`CloudDriver` 协议，而
    # 光鸭特有的能力（``iter_tree_by_cid`` / ``ensure_dir`` /
    # ``list_entries`` / 分享那套）不在协议里——115 没有它们。
    # 与 :mod:`app.services.migration` 同一处理方式。
    src: Any = build_driver(
        "guangya",
        source.credential,
        source.device_type,
        on_token_refresh=_on_src_refresh,
    )
    tgt: Any = build_driver(
        "guangya",
        target.credential,
        target.device_type,
        on_token_refresh=_on_tgt_refresh,
    )

    job.source_name = source.name
    job.target_name = target.name
    job.log(f"源：{source.name}（{source.id}） → 目标：{target.name}（{target.id}）")

    # ---------- ① 遍历源树，按目录分组 ----------
    #
    # 遍历是**全量列表**（约 16 个请求），偶发 DNS 抖动要能重试——
    # 否则一次抖动就毁掉整轮（生产实测踩到）。
    groups: dict[str, _Group] = {}
    for attempt in range(TRAVERSE_ATTEMPTS):
        groups = {}
        job.discovered = 0
        try:
            async for entry in src.iter_tree_by_cid(
                params.source_file_id, parent_path="/", include_subdirs=True
            ):
                if job.state != "running":
                    return
                rel = entry.relative_dir or ""
                group = groups.get(rel)
                if group is None:
                    group = groups[rel] = _Group(rel_dir=rel)
                group.files.append(entry)
                job.discovered += 1
            break
        except DriverError as exc:
            if _is_transient(exc) and attempt + 1 < TRAVERSE_ATTEMPTS:
                job.log(
                    f"⚠️ 遍历源目录失败（{exc}），{RETRY_DELAY_SEC * (attempt + 1)}s 后重试"
                )
                await _sleep(RETRY_DELAY_SEC * (attempt + 1))
                continue
            raise

    job.dirs_walked = len(groups)
    job.total_dirs = len(groups)
    if not groups:
        job.log("源目录下没有文件，无需转存")
        return

    # ---------- ② 过滤 ----------
    video_exts = {
        e.strip().lower() for e in (source.video_exts or "").split(",") if e.strip()
    }
    min_bytes = max(params.min_size_mb, 0) * 1024 * 1024
    max_bytes = max(params.max_size_mb, 0) * 1024 * 1024

    def _skip_reason(entry: CloudFile) -> str | None:
        """判断该文件是否跳过，返回原因（``None`` = 要转存）。"""
        name = entry.name
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""

        # 隐藏备份文件（115 客户端残留）——不是正片
        if naming.is_hidden_backup(name):
            return FILTER_EXT
        if video_exts and ext not in video_exts:
            return FILTER_EXT
        if params.exclude_disc:
            if ext in DISC_EXTS:
                return FILTER_EXT
            parts = {p.lower() for p in entry.path.split("/") if p}
            if parts & DISC_DIRS:
                return FILTER_EXT
        if min_bytes > 0 and entry.size < min_bytes:
            return FILTER_SMALL
        if max_bytes > 0 and entry.size > max_bytes:
            return FILTER_LARGE
        return None

    # ---------- ③ 逐目录处理 ----------
    gate = _ShareGate(params.submit_interval_sec)
    for rel in sorted(groups.keys()):
        if job.state != "running":
            return
        group = groups[rel]
        job.current_path = rel or "/"

        pending: list[CloudFile] = []
        for entry in group.files:
            reason = _skip_reason(entry)
            if reason is not None:
                job.count_filtered(reason)
                continue
            pending.append(entry)
        if not pending:
            continue

        # ---- 目标侧建目录 + 预检（网络抖动要能重试）----
        target_dir = ""
        existing: dict[str, tuple[int, str]] = {}
        for attempt in range(DIR_ATTEMPTS):
            try:
                # 逐层建目录（幂等：同名目录复用，不会建出 xxx(1)）
                target_dir = await tgt.ensure_dir(rel, params.target_file_id)
                existing = await tgt.list_entries(target_dir)
                break
            except DriverError as exc:
                if _is_transient(exc) and attempt + 1 < DIR_ATTEMPTS:
                    job.log(
                        f"⚠️ 准备目标目录失败（{exc}），"
                        f"{RETRY_DELAY_SEC * (attempt + 1)}s 后重试"
                    )
                    await _sleep(RETRY_DELAY_SEC * (attempt + 1))
                    continue
                # 预检都失败 → 该目录的文件全部记失败，继续下一个目录
                for entry in pending:
                    job.record_failed(entry, f"{type(exc).__name__}: {exc}")
                job.log(f"⚠️ 目标目录准备失败，已跳过：{rel or '/'}（{exc}）")
                break
        else:
            for entry in pending:
                job.record_failed(entry, "目标目录准备失败（重试耗尽）")
            continue
        if not target_dir:
            continue

        # 预检目标目录 —— 逐文件判冲突（同名同大小跳过 / 不同大小记冲突）
        to_save: list[CloudFile] = []
        for entry in pending:
            hit = existing.get(entry.name)
            if hit is None:
                to_save.append(entry)
            elif hit[0] == entry.size:
                job.skipped_existing += 1
                job.log(f"⏭️ 已存在，跳过：{entry.path}")
            else:
                job.record_conflict(entry, hit[0])
                job.log(
                    f"⚠️ 同名但大小不同，跳过：{entry.path}"
                    f"（源 {entry.size:,} / 目标 {hit[0]:,} 字节）"
                )
        if not to_save:
            job.processed_dirs += 1
            continue

        # 分批转存（建分享上限 20；遇「数量超限」自动把批次减半）
        job.processed_dirs += 1
        try:
            await _transfer_directory(
                job,
                src=src,
                tgt=tgt,
                entries=to_save,
                target_dir=target_dir,
                gate=gate,
                delete_source=params.delete_source,
            )
        except DriverError as exc:
            # 单个目录失败**不炸整轮**——逐文件记失败，继续下一个目录。
            #
            # ⚠️ 这里不能重复计数：``_transfer_batch`` 内部已记过的失败
            # （落盘校验不过等）是**逐文件**记的且**不抛异常**，所以能走到
            # 这里说明是**整批失败**，把该目录的文件统一记一次。
            for entry in to_save:
                job.record_failed(entry, f"{type(exc).__name__}: {exc}")
            job.log(f"⚠️ 目录转存失败，已跳过：{rel or '/'}（{exc}）")

    job.current_path = ""


async def _transfer_directory(
    job: RelayJob,
    *,
    src: Any,
    tgt: Any,
    entries: list[CloudFile],
    target_dir: str,
    gate: _ShareGate,
    delete_source: bool,
) -> None:
    """把一个目录下的文件分批转存。

    **遇「分享数量超限」（``220``）自动把批次减半重试**——服务端的上限
    可能随内容而异（实测一次建分享最多 20 个文件），别写死。
    """
    batch = min(BATCH_SIZE, max(len(entries), 1))
    index = 0
    attempts = 0
    while index < len(entries):
        if job.state != "running":
            return
        chunk = entries[index : index + batch]
        try:
            await _transfer_batch(
                job,
                src=src,
                tgt=tgt,
                entries=chunk,
                target_dir=target_dir,
                gate=gate,
                delete_source=delete_source,
            )
        except DriverError as exc:
            if exc.kind == "limit" and batch > 1:
                batch = max(1, batch // 2)
                attempts = 0
                job.log(f"⚠️ 分享数量超限，批次降到 {batch} 个文件后重试")
                continue  # index 不变 → 用更小的批次重新切
            if _is_transient(exc) and attempts + 1 < BATCH_ATTEMPTS:
                attempts += 1
                job.log(
                    f"⚠️ 网络抖动（{exc}），"
                    f"{RETRY_DELAY_SEC * attempts}s 后重试本批"
                )
                await _sleep(RETRY_DELAY_SEC * attempts)
                continue  # index 不变 → 重试同一批
            raise
        attempts = 0
        index += len(chunk)


async def _transfer_batch(
    job: RelayJob,
    *,
    src: Any,
    tgt: Any,
    entries: list[CloudFile],
    target_dir: str,
    gate: _ShareGate,
    delete_source: bool,
) -> None:
    """转存一批文件：分享 → 保存 → 校验落盘 → 清理分享（→ 可选清理源）。"""
    ids = [e.remote_id for e in entries]
    title = f"relay-{job.id}-{len(ids)}"

    waited = await gate.wait()
    if waited >= 1:
        job.log(f"⏱️ 错开建分享，等待 {waited:.0f}s")

    share = await _create_share_retry(job, src, ids, title)
    job.log(
        f"🔗 已建分享 {share.share_id}（{len(ids)} 个文件，"
        f"{sum(e.size for e in entries) / 1024**3:.2f} GB）"
    )

    try:
        # ⚠️ 匿名调用（带 Bearer 一律「分享已失效」）
        token = await src.share_access_token(share.share_id, share.code)

        # 列分享内容——确认服务端确实带上了这些文件（否则会只保存一部分）
        listed = await src.share_files_list(token)
        listed_ids = {str(item.get("fileId") or "") for item in listed}
        missing = [e for e in entries if e.remote_id not in listed_ids]
        if missing:
            for entry in missing:
                job.record_failed(entry, "分享内容里没有这个文件（服务端未收录）")
            ok_entries = [e for e in entries if e.remote_id in listed_ids]
            if not ok_entries:
                return
            entries = ok_entries
            ids = [e.remote_id for e in entries]

        # 保存（带 Bearer —— 匿名会「无效 token」）
        task_id = await tgt.restore_share(token, ids, target_dir)
        job.log(f"⬆️ 已提交保存：taskId={task_id}")

        done, missing_after = await _verify_saved(tgt, target_dir, entries)
        for entry in done:
            job.success += 1
            job.transferred_bytes += entry.size
        for entry in missing_after:
            job.record_failed(entry, "保存后未在目标目录出现同名同大小文件")
        if done:
            job.log(f"✅ 完成 {len(done)} 个（{sum(e.size for e in done) / 1024**3:.2f} GB）")

        # 可选：清理源（**只清理已校验成功的**）
        if delete_source and done:
            await _delete_source_files(job, src, done)
    finally:
        try:
            await src.delete_share(share.share_id)
        except Exception as exc:
            job.log(f"⚠️ 删除分享 {share.share_id} 失败：{exc}")


async def _create_share_retry(
    job: RelayJob, src: Any, file_ids: list[str], title: str
) -> ShareInfo:
    """建分享；撞到限流（``201``）就退避重试。

    ``201 分享已失效`` 的成因是「同账号短时间内连续建分享」被限流——
    等一会儿再试就好，故可重试。而 ``112 参数错误`` 是调用方拼错了参数，
    **重试没有意义**，直接抛。
    """
    delay = max(job.params.submit_interval_sec, 5.0)
    last: DriverError | None = None
    for attempt in range(MAX_SHARE_ATTEMPTS):
        try:
            share: ShareInfo = await src.create_share(file_ids, title)
            return share
        except DriverError as exc:
            if exc.kind != "rate_limit":
                raise
            last = exc
            job.log(f"⚠️ 建分享被限流（第 {attempt + 1} 次），{delay:.0f}s 后重试")
            await _sleep(delay)
            delay = min(delay * 2, MAX_BACKOFF_SEC)
    raise last or DriverError("建分享反复被限流", kind="rate_limit")


async def _verify_saved(
    tgt: Any, target_dir: str, entries: list[CloudFile]
) -> tuple[list[CloudFile], list[CloudFile]]:
    """轮询目标目录，返回 ``(已落盘, 未落盘)``。

    保存是**异步**的（服务端在复制），返回 taskId 时文件可能还没到。
    完成判定走「目标目录里出现同名同大小」——与服务端的进度字段无关。

    ⚠️ 服务端同名会**自动改名** ``xxx(1)``，故同名查不到即判未落盘
    （那正是我们要拦下的垃圾副本）。
    """
    deadline = time.monotonic() + VERIFY_TIMEOUT_SEC
    done: list[CloudFile] = []
    missing: list[CloudFile] = []
    while True:
        current = await tgt.list_entries(target_dir)
        done = []
        missing = []
        for entry in entries:
            hit = current.get(entry.name)
            if hit is not None and hit[0] == entry.size:
                done.append(entry)
            else:
                missing.append(entry)
        if not missing or time.monotonic() >= deadline:
            break
        await _sleep(VERIFY_INTERVAL_SEC)
    return done, missing


async def _delete_source_files(job: RelayJob, src: Any, entries: list[CloudFile]) -> None:
    """清理源端文件（**仅在显式开启「搬完清理源」时调用**）。

    逐个删（进回收站，非永久删除），并**记审计日志**——删数据不可逆，
    必须能回答「删了什么」。

    单个失败不中断其余条目。
    """
    for entry in entries:
        try:
            await src.delete_file(entry.remote_id)
            logger.warning(
                "转存清理源：删除 %s（%d 字节）", entry.path, entry.size
            )
            job.log(f"🗑️ 已清理源：{entry.path}")
        except Exception as exc:
            logger.warning("转存清理源失败 %s：%s", entry.path, exc)
            job.log(f"⚠️ 清理源失败：{entry.path}（{exc}）")
