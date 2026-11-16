"""把上游模型目录同步进 ``model_config.toml``。

同步范围严格限定在**本插件自己的 provider**（默认 ``WorkBuddy``）名下：

* 目录新增的模型 → 追加；
* 目录里仍存在、但 ``visual`` 与上游不一致的模型 → 更新该字段；
* 目录里已消失的模型 → 从 provider 名下移除；
* 其他 provider 的模型一律不碰。

上游读取出现区域错误时（目录可能不完整），本次同步不做删除，避免误删。
写前备份到插件数据目录的 ``backups/``，WebUI 侧仍需二次确认。
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import tomlkit

log = logging.getLogger("_maibot_plugin_local_workbuddy_pool.sync")

BACKUP_KEEP = 10


def resolve_model_config_path(configured: str, data_dir: Path) -> Path:
    """解析 model_config.toml 路径；配置为空时按数据目录向上推断。"""
    text = (configured or "").strip()
    if text:
        return Path(text)
    # data_dir 通常是 <实例>/data/plugins/<plugin_id>
    for parent in list(data_dir.parents)[:4]:
        candidate = parent / "config" / "model_config.toml"
        if candidate.exists():
            return candidate
    for fallback in ("/MaiMBot/config/model_config.toml", "/MaiBot/config/model_config.toml"):
        if Path(fallback).exists():
            return Path(fallback)
    return Path("/MaiMBot/config/model_config.toml")


def _load(path: Path):
    if not path.exists():
        raise FileNotFoundError(f"找不到模型配置文件：{path}")
    return tomlkit.parse(path.read_text(encoding="utf-8"))


def _find_provider(doc, provider_name: str):
    providers = doc.get("api_providers")
    if providers is None:
        return None
    for item in providers:
        if isinstance(item, dict) and item.get("name") == provider_name:
            return item
    return None


def _provider_rows(doc, provider_name: str) -> list[dict[str, Any]]:
    """该 provider 名下的模型行（含 AoT 下标）。"""
    rows: list[dict[str, Any]] = []
    items = doc.get("models")
    if items is None:
        return rows
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        if item.get("api_provider") != provider_name:
            continue
        identifier = item.get("model_identifier")
        if not isinstance(identifier, str) or not identifier:
            continue
        rows.append(
            {
                "index": index,
                "id": identifier,
                "name": item.get("name") if isinstance(item.get("name"), str) else identifier,
                "visual": bool(item.get("visual")),
            }
        )
    return rows


def _model_table(model: dict[str, Any], provider_name: str, display_name: str, visual: bool):
    table = tomlkit.table()
    table["model_identifier"] = model["id"]
    table["model_type"] = "llm"
    table["name"] = display_name
    table["api_provider"] = provider_name
    table["price_in"] = 0.0
    table["price_out"] = 0.0
    table["cache_price_in"] = 0.0
    table["price_periods"] = []
    table["send_temperature"] = True
    table["visual"] = bool(visual)
    table["force_stream_mode"] = True
    table["extra_params"] = tomlkit.inline_table()
    return table


def _target_visual(model_id: str, visual_ids: set[str] | None) -> bool:
    """目标视觉能力：只认手动勾选，不按上游字段推断。"""
    return model_id in (visual_ids or set())


def visual_ids_in_config(path: Path, provider_name: str) -> list[str]:
    """读取当前配置里该 provider 名下 ``visual=true`` 的模型 id（用于首次迁移种子）。"""
    try:
        doc = _load(path)
    except (FileNotFoundError, OSError, ValueError):
        return []
    return [row["id"] for row in _provider_rows(doc, provider_name) if row["visual"]]


def plan(
    path: Path,
    provider_name: str,
    models: list[dict[str, Any]],
    *,
    visual_ids: set[str] | None = None,
    server_base_url: str = "",
    allow_remove: bool = True,
) -> dict[str, Any]:
    """只读计划：要追加、要更新视觉能力、要移除的模型，以及 provider 字段差异。"""
    doc = _load(path)
    provider = _find_provider(doc, provider_name)
    rows = _provider_rows(doc, provider_name)
    by_id = {row["id"]: row for row in rows}
    models_by_id = {str(model.get("id")): model for model in models if model.get("id")}
    names = {str(item.get("name")) for item in (doc.get("models") or []) if isinstance(item, dict)}

    to_add: list[dict[str, Any]] = []
    for model in models:
        model_id = model.get("id")
        if not isinstance(model_id, str) or not model_id or model_id in by_id:
            continue
        display_name = f"WorkBuddy {model.get('name') or model_id}"
        if display_name in names:
            display_name = f"{display_name} ({model_id})"
        to_add.append(
            {
                "id": model_id,
                "name": display_name,
                "visual": _target_visual(model_id, visual_ids),
                "context_window": model.get("context_window", 0),
                "max_tokens": model.get("max_tokens", 0),
                "credit_multiplier": model.get("credit_multiplier"),
            }
        )
        names.add(display_name)

    to_update: list[dict[str, Any]] = []
    for model_id, row in by_id.items():
        if model_id not in models_by_id:
            continue
        target = _target_visual(model_id, visual_ids)
        if row["visual"] != target:
            to_update.append(
                {
                    "id": model_id,
                    "name": row["name"],
                    "visual": target,
                    "current_visual": row["visual"],
                }
            )

    stale = [row for row in rows if row["id"] not in models_by_id]
    to_remove = [{"id": row["id"], "name": row["name"]} for row in stale] if allow_remove else []

    provider_patch: list[str] = []
    if provider is None:
        provider_patch = ["create_provider"]
    else:
        if provider.get("client_type") != "local.workbuddy-pool":
            provider_patch.append("client_type")
        if server_base_url and provider.get("base_url") != server_base_url:
            provider_patch.append("base_url")
        if provider.get("model_list_endpoint") != "/models":
            provider_patch.append("model_list_endpoint")

    return {
        "path": str(path),
        "provider": provider_name,
        "provider_exists": provider is not None,
        "provider_patch": provider_patch,
        "to_add": to_add,
        "add_count": len(to_add),
        "to_update": to_update,
        "update_count": len(to_update),
        "to_remove": to_remove,
        "remove_count": len(to_remove),
        "skipped_remove_count": len(stale) if not allow_remove else 0,
        "existing_count": len(by_id),
        "allow_remove": allow_remove,
        "server_base_url": server_base_url,
    }


def _backup(path: Path, backup_dir: Path) -> str:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"model_config-{stamp}.toml"
    shutil.copy2(path, target)
    backups = sorted(backup_dir.glob("model_config-*.toml"))
    for stale in backups[:-BACKUP_KEEP]:
        try:
            stale.unlink()
        except OSError:
            pass
    return str(target)


def apply(
    path: Path,
    provider_name: str,
    models: list[dict[str, Any]],
    *,
    visual_ids: set[str] | None = None,
    server_base_url: str = "",
    allow_remove: bool = True,
    backup_dir: Path | None = None,
) -> dict[str, Any]:
    """执行同步：备份 → 增/改/删 provider 名下模型 → 修 provider 的列表字段。"""
    result = plan(
        path,
        provider_name,
        models,
        visual_ids=visual_ids,
        server_base_url=server_base_url,
        allow_remove=allow_remove,
    )
    touched = result["add_count"] or result["update_count"] or result["remove_count"]
    if not touched and not result["provider_patch"]:
        result["changed"] = False
        result["backup"] = ""
        return result

    doc = _load(path)
    provider = _find_provider(doc, provider_name)
    if provider is None:
        providers = doc.get("api_providers")
        if providers is None:
            providers = tomlkit.aot()
            doc["api_providers"] = providers
        provider = tomlkit.table()
        provider["name"] = provider_name
        provider["base_url"] = server_base_url or ""
        provider["api_key"] = "workbuddy-plugin"
        provider["client_type"] = "local.workbuddy-pool"
        provider["max_retry"] = 0
        provider["timeout"] = 180
        provider["retry_interval"] = 3
        provider["model_list_endpoint"] = "/models"
        provider["default_headers"] = tomlkit.table()
        provider["default_query"] = tomlkit.table()
        providers.append(provider)
    else:
        provider["client_type"] = "local.workbuddy-pool"
        if server_base_url:
            provider["base_url"] = server_base_url
        provider["model_list_endpoint"] = "/models"

    items = doc.get("models")
    # 先删：按 AoT 下标倒序，避免删除后下标漂移。
    if result["remove_count"] and items is not None:
        remove_ids = {entry["id"] for entry in result["to_remove"]}
        for index in range(len(items) - 1, -1, -1):
            item = items[index]
            if (
                isinstance(item, dict)
                and item.get("api_provider") == provider_name
                and item.get("model_identifier") in remove_ids
            ):
                del items[index]

    # 再改：只动 visual，其他字段保持原样。
    if result["update_count"] and items is not None:
        update_map = {entry["id"]: entry["visual"] for entry in result["to_update"]}
        for item in items:
            if not isinstance(item, dict) or item.get("api_provider") != provider_name:
                continue
            identifier = item.get("model_identifier")
            if identifier in update_map:
                item["visual"] = bool(update_map[identifier])

    # 最后追加缺失模型。
    if result["to_add"]:
        if items is None:
            items = tomlkit.aot()
            doc["models"] = items
        by_id = {model.get("id"): model for model in models}
        for entry in result["to_add"]:
            source = by_id.get(entry["id"]) or {"id": entry["id"], "name": entry["id"]}
            items.append(
                _model_table(source, provider_name, entry["name"], bool(entry["visual"]))
            )

    backup = _backup(path, backup_dir) if backup_dir else ""
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(tomlkit.dumps(doc), encoding="utf-8")
    tmp.replace(path)
    result["changed"] = True
    result["backup"] = backup
    log.info(
        "模型同步完成：新增 %d、更新 %d、移除 %d，provider 补丁 %s，备份 %s",
        result["add_count"],
        result["update_count"],
        result["remove_count"],
        result["provider_patch"] or "无",
        backup or "未备份",
    )
    return result
