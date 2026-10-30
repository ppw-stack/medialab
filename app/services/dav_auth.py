"""WebDAV 认证：HTTP Basic Auth + 失败锁定 + 验证结果缓存。

**为什么要独立于面板鉴权**：面板是单管理员（环境变量里的
``WEB_USERNAME``/``WEB_PASSWORD``），WebDAV 账号是「给播放器用的凭证」
——它们不应该能开面板，面板也不该暴露给拿到播放器的第三方。

**性能**：PBKDF2-HMAC-SHA256 600k 迭代实测约 **42 ms**。Infuse 扫一次库
要发几百个 PROPFIND —— 每请求验一次密码就是十几秒起。故缓存验证结果：

- key = ``(IP, Authorization 头哈希)``，TTL ``dav_auth_cache_sec``（默认 60s）
- **该 IP 一旦认证失败，立即清空它的全部缓存** —— 密码泄露后攻击者
  最多只能用同一个头多试 60 秒
- 缓存是**进程内**的（重启即失效，符合预期）

**失败锁定：两个维度，触发条件不同，互不误伤**

| 维度 | 触发条件 | 锁定范围 | 挡的攻击 |
|---|---|---|---|
| **按 IP** | 该 IP 累计失败达阈值 | 该 IP 的**所有**请求 | 单 IP 换着用户名喷洒 |
| **按用户名** | 该用户名累计失败达阈值**且来自 >= 2 个不同 IP** | 该用户名的**所有**请求 | 分布式爆破 |

用户名维度**必须**要求「多 IP」才触发，否则会出现误伤：一个 IP 连续试错
5 次就会把该用户名锁上，导致**其它 IP 上的正常用户也被拒**（同一个账号
在电视和手机上各用一次是很常见的）。

失败后**两个维度都记**，但触发条件不同 —— 这样单 IP 爆破靠 IP 维度
挡住，分布式爆破靠用户名维度挡住。

**阻塞问题**：PBKDF2 是 CPU 密集的同步计算，直接在 event loop 里跑会
卡住**整个服务**（含 ``/302`` 中转）。故 :meth:`DavAuthenticator.authenticate`
是 async 的，验密码走 ``asyncio.to_thread``。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field

from sqlalchemy import select

from app.core.config import AppSettings
from app.core.logging import get_logger
from app.core.timeutils import bj_now
from app.db.models import DavUser
from app.db.session import session_scope

logger = get_logger(__name__)

#: PBKDF2 迭代次数（与面板密码一致，见 :mod:`app.core.auth`）
PBKDF2_ITERATIONS = 600_000

#: 用户名维度锁定所需的**最少不同 IP 数**
#:
#: 1 = 单 IP 反复试 → 只靠 IP 维度锁，用户名不受影响（避免误伤其它设备）
#: >= 2 = 分布式爆破 → 用户名也锁
LOCK_MIN_DISTINCT_IPS = 2

#: 单个用户名最多记录多少个来源 IP（防内存无界增长）
MAX_TRACKED_IPS = 64

#: 认证成功
AUTH_OK = "ok"
#: 凭据错误（用户名不存在 / 密码不对）
AUTH_INVALID = "invalid"
#: 账号已停用
AUTH_DISABLED = "disabled"
#: 已被锁定（按 IP 或按用户名）
AUTH_LOCKED = "locked"
#: 没带 Authorization 头
AUTH_MISSING = "missing"
#: Authorization 头格式不对（不是 Basic / base64 坏了）
AUTH_MALFORMED = "malformed"


@dataclass
class AuthResult:
    """认证结果。"""

    status: str
    user_id: int = 0
    username: str = ""
    #: 锁定的原因：``ip`` / ``user`` —— 用于给客户端不同的提示
    locked_by: str = ""

    @property
    def ok(self) -> bool:
        return self.status == AUTH_OK


@dataclass
class _FailRecord:
    """单个维度的失败计数。

    Attributes:
        count: 累计失败次数。
        locked_until: 锁定截止时刻（0 = 未锁定）。
        last_failure: 最后一次失败的时刻（滑动窗口用）。
        ips: 尝试过的来源 IP 集合（**只用于用户名维度**）。
    """

    count: int = 0
    locked_until: float = 0.0
    last_failure: float = 0.0
    ips: set[str] = field(default_factory=set)


@dataclass
class _CacheEntry:
    """已验证的凭据缓存项。"""

    user_id: int
    username: str
    expires_at: float


class DavAuthenticator:
    """WebDAV 认证器（进程内单例）。

    **为什么不用面板的 session cookie**：WebDAV 客户端（Infuse / 爆米花 /
    文件管理器）不会维护 cookie —— 它们只认 Basic Auth。
    """

    def __init__(self) -> None:
        #: (IP, Authorization 头哈希) → 已验证凭据
        self._cache: dict[tuple[str, str], _CacheEntry] = {}
        #: IP → 失败记录（**不区分用户名**——挡住「一个 IP 换着用户名喷洒」）
        self._fail_by_ip: dict[str, _FailRecord] = {}
        #: 用户名 → 失败记录（**不区分 IP**——挡住「多个 IP 打同一个账号」）
        self._fail_by_user: dict[str, _FailRecord] = {}

    # -- 主入口 --

    async def authenticate(
        self,
        settings: AppSettings,
        authorization: str | None,
        remote_addr: str,
    ) -> AuthResult:
        """验证 Basic Auth 凭据。

        Args:
            settings: 应用配置。
            authorization: ``Authorization`` 头（``Basic base64(user:pass)``）。
            remote_addr: 来源 IP（用于失败计数与缓存隔离）。

        Returns:
            :class:`AuthResult` —— ``status`` 说明失败原因。
        """
        if not authorization:
            return AuthResult(AUTH_MISSING)

        creds = _parse_basic(authorization)
        if creds is None:
            # 格式不对（不是 Basic / base64 坏了）——**不计入失败计数**。
            # 客户端发来的畸形头不该消耗账号的失败配额。
            return AuthResult(AUTH_MALFORMED)

        username, password = creds
        ip = remote_addr or "unknown"

        # ① 锁定检查
        locked_by = self._is_locked(ip, username)
        if locked_by:
            return AuthResult(AUTH_LOCKED, username=username, locked_by=locked_by)

        # ② 缓存命中 → 直接放行
        cache_key = (ip, _auth_hash(authorization))
        cached = self._cache.get(cache_key)
        if cached is not None and cached.expires_at > time.time():
            return AuthResult(AUTH_OK, cached.user_id, cached.username)

        # ③ 查库 + 验密码
        async with session_scope() as db:
            row = (
                await db.execute(select(DavUser).where(DavUser.username == username))
            ).scalar_one_or_none()

            if row is None:
                # 用户不存在 —— **也要走一次哈希**避免时序差异暴露用户是否存在
                await asyncio.to_thread(_dummy_verify, password)
                self._record_failure(settings, ip, username)
                return AuthResult(AUTH_INVALID, username=username)

            if not row.is_active:
                # 账号停用：不计失败次数（不是用户输错了），但也不放行。
                return AuthResult(AUTH_DISABLED, username=username)

            # PBKDF2 是 CPU 密集的同步计算 —— **必须**丢线程池，
            # 否则会卡住 event loop，连带影响 /302 中转。
            ok = await asyncio.to_thread(verify_password, password, row.password_hash)
            if not ok:
                self._record_failure(settings, ip, username)
                return AuthResult(AUTH_INVALID, username=username)

            user_id = row.id
            row.last_login_at = bj_now()

        # ④ 成功：清失败计数 + 写缓存
        self._clear_failures(ip, username)
        self._cache[cache_key] = _CacheEntry(
            user_id=user_id,
            username=username,
            expires_at=time.time() + settings.dav_auth_cache_sec,
        )
        return AuthResult(AUTH_OK, user_id, username)

    # -- 失败计数 / 锁定 --

    def _is_locked(self, ip: str, username: str) -> str:
        """检查是否被锁。

        Returns:
            ``""`` = 未锁；``"ip"`` / ``"user"`` = 被锁的维度。
        """
        now = time.time()
        ip_record = self._fail_by_ip.get(ip)
        if ip_record is not None and ip_record.locked_until > now:
            return "ip"
        user_record = self._fail_by_user.get(username)
        if user_record is not None and user_record.locked_until > now:
            return "user"
        return ""

    def _record_failure(self, settings: AppSettings, ip: str, username: str) -> None:
        """记一次失败，并按阈值锁定。

        两个维度的**触发条件不同**（见模块文档）：

        - IP 维度：累计失败达阈值即锁
        - 用户名维度：累计失败达阈值**且来自 >= 2 个不同 IP** 才锁
        """
        now = time.time()
        threshold = settings.dav_max_failures

        # -- IP 维度 --
        ip_record = self._fail_by_ip.setdefault(ip, _FailRecord())
        ip_record.count += 1
        ip_record.last_failure = now
        if ip_record.count >= threshold:
            ip_record.locked_until = now + settings.dav_lock_sec
            ip_record.count = 0

        # -- 用户名维度 --
        user_record = self._fail_by_user.setdefault(username, _FailRecord())
        user_record.count += 1
        user_record.last_failure = now
        user_record.ips.add(ip)
        if len(user_record.ips) > MAX_TRACKED_IPS:
            # 只保留最近的若干个 IP（set 无序，取任意子集即可）
            user_record.ips = set(list(user_record.ips)[:MAX_TRACKED_IPS])
        if (
            user_record.count >= threshold
            and len(user_record.ips) >= LOCK_MIN_DISTINCT_IPS
        ):
            user_record.locked_until = now + settings.dav_lock_sec
            user_record.count = 0

        # **该 IP 的全部缓存立即作废** —— 认证失败说明持有的凭据可能已泄露
        self._purge_ip_cache(ip)

        logger.warning("WebDAV 认证失败：用户 %s，来源 %s", username, ip)

    def _clear_failures(self, ip: str, username: str) -> None:
        """认证成功 → 清零该 IP 与该用户名的失败计数。

        用户名维度的 ``ips`` 也清空 —— 该用户名已证明是**真用户**，
        不该再记着之前的来源 IP。
        """
        self._fail_by_ip.pop(ip, None)
        self._fail_by_user.pop(username, None)

    def _purge_ip_cache(self, ip: str) -> None:
        """清掉某 IP 的全部认证缓存。"""
        for key in [k for k in self._cache if k[0] == ip]:
            del self._cache[key]

    def purge_all_cache(self) -> int:
        """清掉**全部**认证缓存（改密码 / 停用账号时用）。

        账号被改密码或停用后，旧的凭据不应继续命中缓存。
        """
        count = len(self._cache)
        self._cache.clear()
        return count

    # -- 维护 --

    def purge_expired(self) -> int:
        """清理过期的缓存项，返回清理条数。"""
        now = time.time()
        expired = [k for k, v in self._cache.items() if v.expires_at <= now]
        for key in expired:
            del self._cache[key]
        return len(expired)

    def stats(self) -> dict[str, int]:
        """认证器状态（监控用）。"""
        return {
            "cached_credentials": len(self._cache),
            "failed_ip": len(self._fail_by_ip),
            "failed_user": len(self._fail_by_user),
        }


#: 进程级认证器
dav_authenticator = DavAuthenticator()


# --------------------------------------------------------------------------
# 密码哈希
# --------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """PBKDF2-HMAC-SHA256：盐 + 迭代（与面板密码同一套）。"""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return f"pbkdf2${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验明文密码与存储的 PBKDF2 哈希（常量时间比较）。"""
    if not stored or not stored.startswith("pbkdf2$"):
        return False
    try:
        algo, iters, salt_hex, dk_hex = stored.split("$")
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(dk_hex)
        got = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iters)
        )
    except (ValueError, TypeError):
        return False
    if algo != "pbkdf2":
        return False
    return hmac.compare_digest(got, expected)


def _dummy_verify(password: str) -> None:
    """用户不存在时也跑一次哈希——避免时序差异暴露「用户是否存在」。"""
    hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), b"\x00" * 16, PBKDF2_ITERATIONS
    )


def _parse_basic(authorization: str) -> tuple[str, str] | None:
    """解析 ``Basic base64(user:pass)``。格式不对返回 None。"""
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "basic":
        return None
    try:
        raw = base64.b64decode(parts[1], validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    if ":" not in raw:
        return None
    username, password = raw.split(":", 1)
    return username, password


def _auth_hash(authorization: str) -> str:
    """Authorization 头的哈希（缓存 key 用，不存明文凭据）。"""
    return hashlib.sha256(authorization.encode("utf-8")).hexdigest()[:32]
