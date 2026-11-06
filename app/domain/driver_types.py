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
的依赖）。当前只有 115 与光鸭盘满足。

domain 层不 import 任何 app 包（见 ``tests/unit/test_layering.py``），
故此处只有**字符串常量**，不引用适配层。
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "PROBEABLE_DRIVER_TYPES",
    "is_probeable",
]


#: 支持测连（登录态 + 容量 + 直链）的驱动类型。
#:
#: **新增驱动类型时**若它实现了 ``check_login`` / ``get_space_info`` /
#: ``get_direct_link`` 三个能力，在这里加一行即可——``probe_driver``
#: 与 ``probe_all_enabled`` 都会自动跟上。
PROBEABLE_DRIVER_TYPES: Final = frozenset({"p115", "guangya"})


def is_probeable(driver_type: str) -> bool:
    """该驱动类型是否支持测连。"""
    return driver_type in PROBEABLE_DRIVER_TYPES
