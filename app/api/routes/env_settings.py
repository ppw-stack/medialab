"""环境变量配置 API（字符串型 DB 持久化设置）。

端点（prefix 挂在 ``/api/v1`` 下）：

- ``GET /env-settings``：查询当前值 + 元数据（label/note/placeholder）
- ``PUT /env-settings``：修改一组配置（DB 先、内存后，跨重启保持）

与 :mod:`app.api.routes.scheduler` 的 runtime-params 端点同构——
本端点服务「字符串型」配置（辅种扫描目录 / 未来其它 env→DB 字符串设置），
runtime-params 服务「整型」作业运行参数。两者不混用同一表，避免
``app_runtime_params.value_str`` 这种「为未来扩展」但实际从没用过的
列承担新职责（破坏现有契约）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.auth import require_session
from app.core.logging import get_logger
from app.services import env_settings as env_settings_service
from app.services.env_settings import (
    ENV_SETTING_SPECS,
    EnvSettingSpec,
    get_env_settings,
    validate,
)

logger = get_logger(__name__)
router = APIRouter(
    dependencies=[Depends(require_session)], tags=["env-settings"]
)


def _spec_to_dict(spec: EnvSettingSpec, value: str) -> dict[str, object]:
    """把 spec + 当前值转成 JSON 友好结构。"""
    return {
        "key": spec.key,
        "label": spec.label,
        "note": spec.note,
        "placeholder": spec.placeholder,
        "max_length": spec.max_length,
        "value": value,
    }


class EnvSettingsOut(BaseModel):
    """环境变量配置响应（specs 内嵌当前值，前端一次拿全）。"""

    items: list[dict[str, object]]


class EnvSettingsUpdateIn(BaseModel):
    """环境变量配置更新请求（部分更新）。

    字段必须与 :data:`app.services.env_settings.ENV_SETTING_SPECS` 一致
    ——之前 :mod:`runtime_params` 因手写字段列表漏了一个参数，被 pydantic
    静默丢弃（前端显示「已保存」，实际没存进去）。下面的
    ``_assert_model_covers_specs()`` 在导入时校验两者一致，新增
    ENV_SETTING_SPECS 条目而忘了加字段会立刻报错。

    **隐藏项**（``spec.hidden=True``，如离线下载的三项）不在此模型里
    ——它们有各自的专用端点（能带目录浏览器、密钥不当明文展示），
    放进通用 textarea 列表反而不好用。导入时校验只覆盖非隐藏项。
    """

    cross_seed_scan_dirs: str | None = None


def _assert_model_covers_specs() -> None:
    """校验更新模型覆盖了全部**非隐藏**的已注册配置（导入时执行）。"""
    spec_keys = {s.key for s in ENV_SETTING_SPECS if not s.hidden}
    model_keys = set(EnvSettingsUpdateIn.model_fields)
    missing = spec_keys - model_keys
    if missing:
        raise RuntimeError(
            "EnvSettingsUpdateIn 缺少已注册配置："
            f"{sorted(missing)}（新增 ENV_SETTING_SPECS 后必须同步加字段）"
        )


_assert_model_covers_specs()


@router.get("/env-settings", summary="查询环境变量配置")
async def get_env_settings_view() -> EnvSettingsOut:
    """返回当前所有**可见**环境变量配置 + 元数据（label/note/placeholder）。

    隐藏项（``spec.hidden``）不在此列表——它们有自己的专用 UI。
    """
    mgr = get_env_settings()
    return EnvSettingsOut(
        items=[
            _spec_to_dict(spec, mgr.get(spec.key))
            for spec in ENV_SETTING_SPECS
            if not spec.hidden
        ],
    )


@router.put("/env-settings", summary="修改环境变量配置")
async def update_env_settings(payload: EnvSettingsUpdateIn) -> EnvSettingsOut:
    """运行时修改字符串型配置（DB 持久化）。下次作业立即生效。

    仅更新 body 中显式给出的字段（partial update）。
    DB 写失败时 API 返 500：内存未变但事务回滚，与
    :mod:`runtime_params` 同一处理。
    """
    updates = {
        k: v for k, v in payload.model_dump().items() if v is not None
    }
    if not updates:
        raise HTTPException(status_code=400, detail="未提供任何要修改的配置")
    try:
        validate(updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        await env_settings_service.apply_env_settings(updates)
    except ValueError as exc:
        # apply 内部会再 validate 一遍（防御），错误转 400
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    mgr = get_env_settings()
    return EnvSettingsOut(
        items=[
            _spec_to_dict(spec, mgr.get(spec.key))
            for spec in ENV_SETTING_SPECS
        ],
    )
