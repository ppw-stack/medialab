"""媒体文件后缀清单的**单一真源**（domain 层，不依赖任何层）。

## 为什么在这里

三个默认后缀清单被两处消费：

- ``app/db/models.py`` —— ``drivers`` 表三列的**列默认值**（新建行生效）
- 前端配置抽屉 —— 存量行未配置时的**预填值**（保存后落库）

它们此前散落三处且互不一致：``models.py`` 的 ``DEFAULT_VIDEO_EXTS``、
``naming.py`` 的两个 ``DEFAULT_*_EXTENSIONS``（frozenset 形态、零引用）、
前端 placeholder 的手写字符串——改一处漏两处。收敛到这里后，加新网盘 /
调整默认清单只改这一个文件。

## 语义约定

- 一律**逗号分隔、无点前缀、小写**（与 ``parse_ext_list`` 的输入格式一致；
  ``parse_ext_list`` 本身会做 ``lstrip(".")`` 与 ``lower()`` 归一，但真源
  直接写成规范形态，避免依赖归一）
- **清空 = 关闭该类文件的同步**（空清单不匹配任何后缀，全部计入
  ``skipped_unknown``）——这是合法配置，读取方不做兜底改写
"""

from __future__ import annotations

__all__ = [
    "DEFAULT_DATA_EXTS",
    "DEFAULT_IMAGE_EXTS",
    "DEFAULT_VIDEO_EXTS",
]

#: 默认视频后缀——扫描依据（**生成 .strm**）。
#:
#: 含 ``iso``：115 的原盘 iso 走直链 + 内挂播放（实测可用）。
DEFAULT_VIDEO_EXTS = "mkv,mp4,avi,mov,wmv,flv,m4v,ts,m2ts,rmvb,rm,webm,iso"

#: 默认媒体图片后缀——海报 / 剧照等（**附件同步**：下载到本地，
#: 与 strm 同目录）。``jpeg`` 与 ``jpg`` 都保留：两个扩展名都常见。
DEFAULT_IMAGE_EXTS = "jpg,jpeg,png,webp"

#: 默认媒体数据后缀——nfo 元数据 + 常见字幕格式（**附件同步**）。
DEFAULT_DATA_EXTS = "nfo,ass,ssa,srt"
