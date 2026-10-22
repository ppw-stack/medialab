"""AI 读码客户端单元测试（FR-3.6）。

锁住两条实测得来的关键行为：

1. **必须放大**——站点验证码原图只有 150×40，同一模型直接读 0/8、
   放大 6 倍后 8/8。忘了放大（或放大倍数写错）会让读码静默失效。
2. **取 ``content`` 而非 ``reasoning_content``**——MiniMax 系列默认开启
   thinking，两者字段分离，取错就拿到一大段思考过程。

``no_network`` autouse fixture（conftest）用 respx 接管 httpx，
本文件所有 HTTP 都被 mock，不会误发真实请求。
"""

from __future__ import annotations

import base64
import io

import httpx
import pytest
import respx
from app.core.ai import AICaptchaReader, upscale_for_vision
from app.core.config import AppSettings
from PIL import Image


def _settings(**kw: object) -> AppSettings:
    base = {
        "ai_api_base": "https://ai.example.com/v1",
        "ai_api_key": "sk-test",
        "ai_model": "some-vision-model",
        "ai_timeout_seconds": 30.0,
    }
    base.update(kw)
    return AppSettings(**base)  # type: ignore[arg-type]


def _captcha(width: int = 150, height: int = 40) -> bytes:
    """造一张测试验证码图（白底黑块，尺寸与站点一致）。"""
    img = Image.new("RGB", (width, height), "white")
    for x in range(10, width - 10, 12):
        for y in range(8, height - 8, 6):
            img.putpixel((x, y), (0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ------------------------------------------------------------------ 放大


def test_upscale_is_lossless_enough_to_preserve_strokes() -> None:
    """放大 6 倍后尺寸正确，且深色笔画仍然存在（未被阈值吃掉）。"""
    src = _captcha()
    out = upscale_for_vision(src, factor=6)

    with Image.open(io.BytesIO(src)) as before, Image.open(io.BytesIO(out)) as after:
        assert after.width == before.width * 6
        assert after.height == before.height * 6
        assert after.mode == before.mode
        # 纯放大不做二值化——深色笔画必须还在
        assert after.convert("L").getextrema()[0] < 60


def test_upscale_rejects_broken_image() -> None:
    """坏图片要转成 AIClientError（调用方据此降级，而不是抛裸异常）。"""
    from app.core.ai import AIClientError

    with pytest.raises(AIClientError):
        upscale_for_vision(b"not-a-png")


# ------------------------------------------------------------------ 可用性


def test_reader_unavailable_when_not_configured() -> None:
    """三项配置全空 → available=False（调用方降级为 UNSUPPORTED）。"""
    assert not AICaptchaReader(_settings(ai_api_base="")).available
    assert not AICaptchaReader(_settings(ai_api_key=None)).available
    assert not AICaptchaReader(_settings(ai_model="")).available


def test_reader_available_when_fully_configured() -> None:
    assert AICaptchaReader(_settings()).available


async def test_read_captcha_refuses_when_not_configured() -> None:
    """未配置时直接抛错，不发任何 HTTP 请求。"""
    from app.core.ai import AIClientError

    reader = AICaptchaReader(_settings(ai_model=""))
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.route().mock(side_effect=AssertionError("未配置时不该发请求"))
        with pytest.raises(AIClientError, match="未配置"):
            await reader.read_captcha(_captcha())


# ------------------------------------------------------------------ 请求


def _ok_response(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content, "reasoning_content": "很长的思考过程…"}}]
        },
    )


@respx.mock
async def test_read_captcha_sends_openai_compatible_request() -> None:
    """请求体形状：OpenAI 兼容 + image_url data URI + 放大后的图。"""
    route = respx.post("https://ai.example.com/v1/chat/completions").mock(
        return_value=_ok_response("27D5PN")
    )

    reader = AICaptchaReader(_settings())
    assert await reader.read_captcha(_captcha()) == "27D5PN"

    sent = route.calls[0].request
    assert sent.headers["Authorization"] == "Bearer sk-test"
    payload = __import__("json").loads(sent.content)
    assert payload["model"] == "some-vision-model"
    # thinking 会吃掉大半 token，给太小 content 为空
    assert payload["max_completion_tokens"] >= 512
    parts = payload["messages"][0]["content"]
    assert parts[0]["type"] == "text"
    assert parts[1]["type"] == "image_url"
    url = parts[1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    # 送出去的是放大后的图（150*6=900 宽），不是原图
    sent_png = base64.b64decode(url.split(",", 1)[1])
    with Image.open(io.BytesIO(sent_png)) as img:
        assert img.width == 900


@respx.mock
async def test_read_captcha_reads_content_not_reasoning() -> None:
    """必须取 ``content``；``reasoning_content`` 是 thinking 过程，取错就废了。"""
    respx.post("https://ai.example.com/v1/chat/completions").mock(
        return_value=_ok_response("AB12CD")
    )
    reader = AICaptchaReader(_settings())
    assert await reader.read_captcha(_captcha()) == "AB12CD"


# ------------------------------------------------------------------ 输出清洗


@pytest.mark.parametrize(
    ("raw", "expect"),
    [
        ("27D5PN", "27D5PN"),
        (" 27D5PN ", "27D5PN"),
        ("**27D5PN**", "27D5PN"),          # 模型爱加 markdown 强调
        ("验证码是：27D5PN", "27D5PN"),   # 模型爱解释
        ("27D5PN\n", "27D5PN"),
        ("27d5pn", "27D5PN"),              # 站点大小写敏感 → 统一大写
    ],
)
@respx.mock
async def test_read_captcha_cleans_output(raw: str, expect: str) -> None:
    respx.post("https://ai.example.com/v1/chat/completions").mock(
        return_value=_ok_response(raw)
    )
    reader = AICaptchaReader(_settings())
    assert await reader.read_captcha(_captcha()) == expect


@respx.mock
async def test_read_captcha_truncates_overlong_output() -> None:
    """多输出字符时截到期望长度（站点只要前 6 位）。"""
    respx.post("https://ai.example.com/v1/chat/completions").mock(
        return_value=_ok_response("27D5PNEXTRA")
    )
    reader = AICaptchaReader(_settings())
    assert await reader.read_captcha(_captcha()) == "27D5PN"


@respx.mock
async def test_read_captcha_rejects_too_short_output() -> None:
    """输出不足 6 位 → 抛错（调用方换验证码重试，不拿残缺答案去提交）。"""
    from app.core.ai import AIClientError

    respx.post("https://ai.example.com/v1/chat/completions").mock(
        return_value=_ok_response("27D5")
    )
    reader = AICaptchaReader(_settings())
    with pytest.raises(AIClientError, match="输出不可用"):
        await reader.read_captcha(_captcha())


@respx.mock
async def test_read_captcha_rejects_empty_content() -> None:
    """thinking 吃光 token 时 content 为空 → 抛错（不是静默返回空串）。"""
    from app.core.ai import AIClientError

    respx.post("https://ai.example.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": "", "reasoning_content": "…"}}]},
        )
    )
    reader = AICaptchaReader(_settings())
    with pytest.raises(AIClientError):
        await reader.read_captcha(_captcha())


# ------------------------------------------------------------------ 错误


@respx.mock
async def test_read_captcha_raises_on_http_error() -> None:
    from app.core.ai import AIClientError

    respx.post("https://ai.example.com/v1/chat/completions").mock(
        return_value=httpx.Response(502, text="reasoning truncated")
    )
    reader = AICaptchaReader(_settings())
    with pytest.raises(AIClientError, match="HTTP 502"):
        await reader.read_captcha(_captcha())


@respx.mock
async def test_read_captcha_raises_on_network_error() -> None:
    from app.core.ai import AIClientError

    respx.post("https://ai.example.com/v1/chat/completions").mock(
        side_effect=httpx.ConnectError("boom")
    )
    reader = AICaptchaReader(_settings())
    with pytest.raises(AIClientError, match="请求失败"):
        await reader.read_captcha(_captcha())


@respx.mock
async def test_read_captcha_raises_on_malformed_response() -> None:
    from app.core.ai import AIClientError

    respx.post("https://ai.example.com/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"unexpected": "shape"})
    )
    reader = AICaptchaReader(_settings())
    with pytest.raises(AIClientError, match="响应结构异常"):
        await reader.read_captcha(_captcha())