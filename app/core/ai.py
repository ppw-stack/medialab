"""AI 读码：把图形验证码交给视觉模型识别（FR-3.6 站点签到）。

用途：hdsky 的每日签到要求图形验证码（`/showup.php` 的 `imagehash` +
`imagestring`）。本模块负责把验证码图片交给**视觉大模型**读出字符。

设计要点（全部来自 2026-10-22 实测，见 AGENTS.md FR-3.6 契约）：

1. **必须先放大**。站点出的图只有 150×40 像素，6 个字符挤在 40px 高度里，
   直接丢给模型几乎必错（实测 minimax-cn/MiniMax-M3 原图 0/8）。放大 6 倍
   后同一模型 8/8。放大是**无损**的（不做阈值 / 不裁剪 / 不去噪），
   跨模板比二值化预处理更稳。
2. **取 `message.content` 而非 `reasoning_content`**。MiniMax-M3 系列
   默认开启 thinking，两者是分开的字段；读 `reasoning_content` 会拿到一大段
   思考过程而不是答案。
3. **`max_completion_tokens` 要给够**。thinking 会消耗 token（实测 6 个
   字符的验证码能烧掉 500+ token），给太小会返回空 content。
4. **`thinking.type="disabled"` 会报 400**。该模型系列要求 adaptive
   thinking，不能关——所以只能靠"给够 token + 短提示词"来控制。

.. warning::

   **合规边界**（AGENTS.md FR-3.6）：允许对**自有账号**做签到自动化
   （含 AI 读图形验证码），**不允许**伪造/破解站点鉴权凭据。
   本模块只读验证码图片，不触碰任何站点的加密签名。
"""

from __future__ import annotations

import base64
import io
import re

import httpx
from PIL import Image

from app.core.config import AppSettings
from app.core.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "AICaptchaReader",
    "AIClientError",
    "get_ai_reader",
    "set_ai_reader",
    "upscale_for_vision",
]


class AIClientError(RuntimeError):
    """AI 读码失败（网络 / 鉴权 / 响应异常 / 输出不可用）。

    由 :class:`AICaptchaReader` 抛出，调用方（站点适配器）负责降级。
    """


#: 验证码放大倍数。
#:
#: 站点原图 150×40，模型直接读几乎必错。实测 6 倍是准确率峰值（8/8）；
#: 4/8 倍偏小，8 倍略过冲（7/8）。
CAPTCHA_UPSCALE = 6

#: 读码提示词。
#:
#: **必须短**。啰嗦的提示词会让模型反复推理，thinking 更容易超预算被网关
#: 质量校验拒掉（实测 502 "reasoning truncated"）。要强调的形状区分
#: （7 不是 Z 等）已由"放大后笔画清晰"保证，不需要在提示词里啰嗦。
_CAPTCHA_PROMPT = (
    "这是一张验证码图片，包含 6 个大写字母或数字字符。"
    "请按从左到右的顺序输出这 6 个字符，只输出字符本身。"
)

#: 字符清洗：只保留字母数字，统一大写。
_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]")


def upscale_for_vision(data: bytes, factor: int = CAPTCHA_UPSCALE) -> bytes:
    """把验证码图片无损放大 N 倍，返回 PNG 字节。

    只做缩放，**不**做二值化 / 裁剪 / 去噪——实测那几步反而会损伤笔画
    （去噪的连通域面积阈值会把细笔画整片删掉）。
    """
    try:
        with Image.open(io.BytesIO(data)) as img:
            enlarged = img.resize(
                (img.width * factor, img.height * factor),
                Image.Resampling.LANCZOS,
            )
            buf = io.BytesIO()
            enlarged.save(buf, format="PNG")
            return buf.getvalue()
    except Exception as exc:
        raise AIClientError(f"验证码图片放大失败：{exc}") from exc


class AICaptchaReader:
    """用视觉大模型读图形验证码。

    走 **OpenAI 兼容协议**（`POST {base}/chat/completions`），
    国内可达的 provider（MiniMax / DeepSeek / Qwen-VL 等）都吃这个格式。

    **未配置 `ai_api_base` / `ai_api_key` / `ai_model` 时 `available` 为
    False**，调用方应据此降级（如返回 `UNSUPPORTED`），不要硬调。
    """

    __slots__ = ("_base", "_client", "_key", "_model", "_timeout")

    def __init__(self, settings: AppSettings, client: httpx.AsyncClient | None = None):
        """构造读码器。

        :param client: 复用外部 HTTP 客户端（测试注入用）；默认自建一个。
        """
        self._base = (settings.ai_api_base or "").rstrip("/")
        key = settings.ai_api_key
        self._key = key.get_secret_value() if key is not None else ""
        self._model = settings.ai_model or ""
        self._timeout = settings.ai_timeout_seconds
        self._client = client

    @property
    def available(self) -> bool:
        """是否已配置齐全（端点 + key + 模型）。未配置时调用方应降级。"""
        return bool(self._base and self._key and self._model)

    async def read_captcha(self, image: bytes, *, length: int = 6) -> str:
        """读出图形验证码中的字符。

        :param image: 验证码原始 PNG 字节。
        :param length: 期望的字符数（hdsky 固定 6 位）。
        :returns: `length` 位大写字母数字串。
        :raises AIClientError: 未配置 / 网络失败 / 输出不可用。

        .. note::

            **不做大小写归一化之外的任何猜测**：站点侧大小写敏感
            （小写答案会被判 `invalid_imagehash`），所以只统一大写，
            不在字符集之间做形近替换。读错由调用方重试兜底
            （每次重试换一张新验证码）。
        """
        if not self.available:
            raise AIClientError("未配置 AI 读码（AI_API_BASE / AI_API_KEY / AI_MODEL）")

        enlarged = upscale_for_vision(image)
        data_url = "data:image/png;base64," + base64.b64encode(enlarged).decode("ascii")
        body: dict[str, object] = {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _CAPTCHA_PROMPT},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            # thinking 会吃掉大量 token，给太小 content 为空；实测 512 够用
            "max_completion_tokens": 512,
        }

        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            resp = await client.post(
                f"{self._base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
        except httpx.HTTPError as exc:
            raise AIClientError(f"AI 读码请求失败：{exc}") from exc
        finally:
            if self._client is None:
                await client.aclose()

        if resp.status_code != 200:
            raise AIClientError(f"AI 读码 HTTP {resp.status_code}")

        try:
            payload = resp.json()
            content = payload["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise AIClientError(f"AI 读码响应结构异常：{exc}") from exc

        # 只取 content——reasoning_content 是 thinking 过程，不是答案
        cleaned = _NON_ALNUM_RE.sub("", str(content or "")).upper()[:length]
        if len(cleaned) < length:
            raise AIClientError(f"AI 读码输出不可用：{content!r}")
        return cleaned


# ------------------------------------------------------------------ 进程级单例
#
# 站点适配器（``app.adapters``）不能 import ``app.services``（分层约束：
# adapters → core 单向），而签到契约 ``checkin()`` 刻意保持**零参数**
# （与 ``get_user_snapshot()`` 一致）。所以读码器走 core 级全局访问器，
# 与 :func:`app.services.factory.get_factory` / ``runtime_params`` 的
# 全局单例约定一致。
_ai_reader: AICaptchaReader | None = None


def get_ai_reader() -> AICaptchaReader:
    """取进程级读码器（首次调用时按当前 settings 懒创建）。"""
    global _ai_reader
    if _ai_reader is None:
        from app.core.config import get_settings

        _ai_reader = AICaptchaReader(get_settings())
    return _ai_reader


def set_ai_reader(reader: AICaptchaReader | None) -> None:
    """替换/清空读码器（测试注入假读码器用；传 ``None`` 复位）。"""
    global _ai_reader
    _ai_reader = reader