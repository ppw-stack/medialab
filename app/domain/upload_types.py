"""数据同步（本地目录 → 网盘）的领域常量。

分层约束：``domain`` 不依赖任何层，可被 db / services / api 安全导入。
"""

from __future__ import annotations

#: 上传任务 ``concurrency`` 列的**默认值** = 小文件通道的并发 worker 数。
#:
#: 语义（2026-11-07 双通道改造，此前该列「保留但不生效」）：
#: 大文件（≥1MB）通道**恒串行**（实测并发有害，见
#: ``docs/upload-bandwidth.md``）；小文件（<1MB 单 PUT）通道并发——
#: 其耗时大头是协议开销（取凭证 + 轮询落盘确认，~2-3s/个，传字节
#: 占比 <15%），并发把轮询等待摊掉。
#:
#: **为什么是 4**：受光鸭全局限速（5 req/s）与「服务端并发不稳」实测
#: 制约，收益在 2-4 档趋于平缓；4 是「泄洪速度 × 服务端风险」的平衡点。
#: 运行时还有两道保险（:class:`app.services.watch_manager._SmallGate`）：
#: 大文件上传中自动降为 1；连续失败 3 次熔断降为串行。
#:
#: 面板可配 1-8（`le=8`）；设回 1 = 小通道串行（与大文件同款行为）。
DEFAULT_UPLOAD_CONCURRENCY = 4

__all__ = ["DEFAULT_UPLOAD_CONCURRENCY"]
