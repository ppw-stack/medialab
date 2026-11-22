"""存储源驱动类型的**单一真源**（domain 层，不依赖任何层）。

## 为什么在这里

「哪些驱动类型支持测连」这个判定此前**硬编码了两处**：

- ``app/services/driver_service.py`` 的 ``probe_driver``（白名单校验）
- ``app/services/driver_service.py`` 的 ``probe_all_enabled``（启动测连的查询条件）

加第三个驱动类型时若漏改其中一处，**不会报错**——那个驱动只是静默地
不被自动测连（或测连时抛「暂不支持探测」）。这与本项目已有的
``UPLOADABLE_DRIVER_TYPES``（上传能力白名单，``app/adapters/drivers/upload_base.py``）
是同一类问题，故同样收敛到 domain 层的**单一真源**。

## 判据

**支持测连** = 驱动实现了 ``check_login()`` / ``get_space_info()`` /
``get_direct_link()`` 三个能力（:func:`app.services.account_status.probe_account`
的依赖）。当前只有光鸭盘满足。

domain 层不 import 任何 app 包（见 ``tests/unit/test_layering.py``），
故此处只有**字符串常量**，不引用适配层。
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "MEDIA_CID_LABELS",
    "PROBEABLE_DRIVER_TYPES",
    "ROOT_DIR_IDS",
    "is_probeable",
    "media_cid_label",
    "root_dir_id",
]


#: 支持测连（登录态 + 容量 + 直链）的驱动类型。
#:
#: **新增驱动类型时**若它实现了 ``check_login`` / ``get_space_info`` /
#: ``get_direct_link`` 三个能力，在这里加一行即可——``probe_driver``
#: 与 ``probe_all_enabled`` 都会自动跟上。
PROBEABLE_DRIVER_TYPES: Final = frozenset({"guangya"})


def is_probeable(driver_type: str) -> bool:
    """该驱动类型是否支持测连。"""
    return driver_type in PROBEABLE_DRIVER_TYPES


#: 各驱动类型的**账号根目录 id**（目录浏览的起点）。
#:
#: 光鸭：根目录的 fileId 就是**空串**（``list_dir_by_cid("")`` 列的就是根）。
#:
#: 为什么需要它：浏览「媒体库目录」时 ``media_cid`` **还没配**，起点必须是
#: 账号根。各网盘的根 id 写法不同，传错列不出东西——故收敛到这里，
#: 前端不必知道差异，``parent_id`` 留空即由后端解析。
ROOT_DIR_IDS: Final[dict[str, str]] = {
    "guangya": "",
}


def root_dir_id(driver_type: str) -> str | None:
    """该驱动类型的账号根目录 id；``None`` = 未知类型（调用方决定兜底）。"""
    return ROOT_DIR_IDS.get(driver_type)


#: 各驱动类型的**媒体库目录 id**的展示名（配置项 label 与报错文案共用）。
#:
#: 为什么放 domain：同一个概念在不同网盘叫法不同——文案写死某一家会让
#: 其它网盘的用户以为存错了东西。业务代码只管取 label，不写
#: ``if type == "guangya"``。
MEDIA_CID_LABELS: Final[dict[str, str]] = {
    "guangya": "光鸭媒体库 fileId",
}


def media_cid_label(driver_type: str) -> str:
    """该驱动类型的媒体库目录 id 叫什么（用于界面文案）。"""
    return MEDIA_CID_LABELS.get(driver_type, "媒体库目录 id")
