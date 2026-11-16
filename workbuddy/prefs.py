"""每模型能力开关：图片输入与思考档位。

图片能力**不由上游目录推断**：上游的 ``supportsImages`` 是平台声明（纯文本模型也常为
true），无法据此核实模型自身能力，所以这里只保存用户手动勾选的结果。
思考档位在真实调用时转成上游的 ``reasoning_effort``，``off`` 表示关掉思考。

保存为 ``<插件数据目录>/model_prefs.json``。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("_maibot_plugin_local_workbuddy_pool.prefs")

PREFS_FILENAME = "model_prefs.json"

REASONING_LEVELS = ("off", "low", "medium", "high", "xhigh", "max")


class ModelPrefs:
    """模型级开关的持久化。"""

    def __init__(self, data_dir: str | Path) -> None:
        self._path = Path(data_dir) / PREFS_FILENAME
        self._doc = self._load()
        self._seeded = self._doc.pop("_seeded", True)

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"visual": [], "reasoning": {}, "_seeded": False}
        if not isinstance(raw, dict):
            return {"visual": [], "reasoning": {}, "_seeded": False}
        visual = raw.get("visual")
        reasoning = raw.get("reasoning")
        return {
            "visual": [item for item in visual if isinstance(item, str)]
            if isinstance(visual, list)
            else [],
            "reasoning": dict(reasoning) if isinstance(reasoning, dict) else {},
            "_seeded": "visual" in raw,
        }

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._doc, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as exc:
            log.warning("保存模型开关失败：%s", exc)

    # ------------------------------------------------------------------ 读取
    def needs_seed(self) -> bool:
        """还没有任何手动勾选记录（首次升级到手动模式）。"""
        return not self._seeded

    def visual_ids(self) -> set[str]:
        return set(self._doc.get("visual") or [])

    def reasoning_for(self, model: str) -> str | None:
        value = (self._doc.get("reasoning") or {}).get(model)
        return value if value in REASONING_LEVELS else None

    def visual_map(self, models: list[dict[str, Any]]) -> dict[str, bool]:
        """同步用的目标视觉能力：完全以手动勾选为准。"""
        checked = self.visual_ids()
        return {str(model["id"]): str(model["id"]) in checked for model in models if model.get("id")}

    def state(self, models: list[dict[str, Any]]) -> list[dict[str, Any]]:
        checked = self.visual_ids()
        reasoning_map = self._doc.get("reasoning") or {}
        rows = []
        for model in models:
            model_id = model.get("id")
            if not isinstance(model_id, str):
                continue
            upstream_reasoning = model.get("reasoning")
            default_effort = ""
            if isinstance(upstream_reasoning, dict):
                default_effort = str(upstream_reasoning.get("default_effort") or "")
            rows.append(
                {
                    "id": model_id,
                    "name": model.get("name") or model_id,
                    "context_window": model.get("context_window", 0),
                    "max_tokens": model.get("max_tokens", 0),
                    "credit_multiplier": model.get("credit_multiplier"),
                    "upstream_reasoning": upstream_reasoning,
                    "default_reasoning": default_effort,
                    "promos": model.get("promos") or [],
                    "visual": model_id in checked,
                    "reasoning": reasoning_map.get(model_id) or "",
                }
            )
        return rows

    # ------------------------------------------------------------------ 写入
    def set(self, visual: list[str] | None = None, reasoning: dict[str, str] | None = None) -> None:
        if visual is not None:
            self._doc["visual"] = sorted({item for item in visual if isinstance(item, str) and item})
            self._seeded = True
        if reasoning is not None:
            cleaned: dict[str, str] = {}
            for model_id, level in reasoning.items():
                if not isinstance(model_id, str):
                    continue
                if level in REASONING_LEVELS:
                    cleaned[model_id] = level
            self._doc["reasoning"] = cleaned
        self._save()
