"""二维码渲染（多个驱动共用）。

抽出来的原因：登录流程需要「服务端给一个 URL，本地渲染成 PNG」。

**为什么必须本地渲染**：

- 服务端返回的 PNG 可能内嵌它自己的域名，扫码 APP 会拒扫
- 服务端返回的图往往是低分辨率的，扫码体验差

本地渲染的内容就是 APP 期望的 URL，扫出来的结果可控。
"""

from __future__ import annotations

import io

from app.core.logging import get_logger

logger = get_logger(__name__)


def render_qr_png(scan_url: str) -> bytes:
    """把 URL 渲染成 QR PNG 字节。

    Args:
        scan_url: 要编码进二维码的内容（APP 期望的授权 URL）。

    Returns:
        PNG 字节。

    Raises:
        RuntimeError: ``qrcode`` 依赖缺失。
    """
    try:
        import qrcode
    except ImportError as exc:  # pragma: no cover - 依赖缺失
        raise RuntimeError("qrcode 未安装：pip install 'qrcode[pil]'") from exc

    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(scan_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
