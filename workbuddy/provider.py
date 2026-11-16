"""MaiBot LLM Provider：把 Host 的上下文转成 OpenAI 消息并交给账号池。"""

from __future__ import annotations

import json
import logging
import secrets
from collections import OrderedDict
from typing import Any

from maibot_sdk import LLMProviderBase

from .pool import WorkBuddyService

log = logging.getLogger("_maibot_plugin_local_workbuddy_pool.provider")


def _parts_to_openai(parts: list[dict]) -> Any:
    """把 ContextItem.parts 转成 OpenAI 的 content（纯文本时用字符串）。"""
    texts: list[str] = []
    rich: list[dict] = []
    has_image = False
    for part in parts or []:
        ptype = part.get("type")
        if ptype == "text":
            text = part.get("text") or ""
            texts.append(text)
            rich.append({"type": "text", "text": text})
        elif ptype == "image":
            has_image = True
            fmt = (part.get("image_format") or "png").lstrip(".")
            b64 = part.get("image_base64") or ""
            rich.append(
                {"type": "image_url", "image_url": {"url": f"data:image/{fmt};base64,{b64}"}}
            )
        elif ptype == "refusal":
            texts.append(part.get("refusal") or "")
    if has_image:
        return rich
    return "".join(texts)


def _text_of(item: dict) -> str:
    return "".join(
        part.get("text") or ""
        for part in item.get("parts") or []
        if part.get("type") == "text"
    )


def context_items_to_messages(items: list[dict]) -> list[dict]:
    """把序列化后的 Context Items 转成 OpenAI chat messages。"""
    messages: list[dict] = []

    def attach_tool_call(call: dict) -> None:
        payload = {
            "id": call.get("call_id") or f"call_{secrets.token_hex(8)}",
            "type": "function",
            "function": {
                "name": call.get("func_name") or "",
                "arguments": json.dumps(call.get("args") or {}, ensure_ascii=False),
            },
        }
        if messages and messages[-1].get("role") == "assistant":
            messages[-1].setdefault("tool_calls", []).append(payload)
        else:
            messages.append({"role": "assistant", "content": "", "tool_calls": [payload]})

    for item in items or []:
        itype = item.get("item_type")
        if itype == "SystemMessageItem":
            messages.append({"role": "system", "content": _parts_to_openai(item.get("parts"))})
        elif itype == "UserMessageItem":
            messages.append({"role": "user", "content": _parts_to_openai(item.get("parts"))})
        elif itype == "AssistantMessageItem":
            messages.append({"role": "assistant", "content": _text_of(item)})
        elif itype == "FunctionCallItem":
            attach_tool_call(item.get("tool_call") or {})
        elif itype == "FunctionCallOutputItem":
            content = item.get("output")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item.get("call_id") or "",
                    "content": content,
                }
            )
        # ReasoningItem 与活动记录对上游无意义，跳过。
    return messages


_EXPRESSION_HINT = (
    "重要：除本次完全不需要语气倾向的情况外，请务必填写本对象，"
    "并至少给出 reply_act 与 tone；能判断时再补充 prefer、avoid、scene。"
)


def _strengthen_reply_schema(tool: dict[str, Any]) -> dict[str, Any]:
    """加强 reply 工具的 expression_intent 约束。

    宿主把 expression_intent 标成可选，模型经常整段省略，回复模型就拿不到语气/
    避免事项。这里只改发给上游的 schema 描述与子字段必填，不动宿主代码。
    """
    fn = tool.get("function")
    if not isinstance(fn, dict) or fn.get("name") != "reply":
        return tool
    try:
        tool = json.loads(json.dumps(tool, ensure_ascii=False))
    except (TypeError, ValueError):
        return tool
    fn = tool.get("function") or {}
    params = fn.get("parameters")
    if not isinstance(params, dict):
        return tool
    props = params.get("properties")
    if not isinstance(props, dict):
        return tool

    intent = props.get("expression_intent")
    if isinstance(intent, dict):
        desc = str(intent.get("description") or "").strip()
        if _EXPRESSION_HINT not in desc:
            intent["description"] = f"{desc}\n{_EXPRESSION_HINT}".strip()
        sub = intent.get("properties")
        if isinstance(sub, dict) and sub:
            required = [key for key in ("reply_act", "tone") if key in sub]
            if required:
                intent["required"] = required

    style = props.get("reply_style")
    if isinstance(style, dict):
        desc = str(style.get("description") or "").strip()
        if "建议每次填写" not in desc:
            style["description"] = f"{desc} 建议每次填写。".strip()
    return tool


def tool_options_to_openai(options: list[dict] | None) -> list[dict] | None:
    if not options:
        return None
    tools: list[dict] = []
    for opt in options:
        if not isinstance(opt, dict):
            continue
        if opt.get("type") == "function" and isinstance(opt.get("function"), dict):
            tools.append(_strengthen_reply_schema(opt))
            continue
        name = opt.get("name") or opt.get("func_name") or opt.get("function_name")
        if not name:
            continue
        params = opt.get("parameters") or opt.get("schema") or {"type": "object", "properties": {}}
        tools.append(
            _strengthen_reply_schema(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": opt.get("description") or "",
                        "parameters": params,
                    },
                }
            )
        )
    return tools or None


def _arguments_dict(raw: Any) -> dict[str, Any]:
    """把上游的 tool_call.arguments 转成宿主需要的字典。

    MaiBot 的插件 Provider 协议按 Context 快照解析，``function.arguments`` 必须是
    对象；如果给的是 OpenAI 的 JSON 字符串，宿主会当成 ``{}``，导致工具调用拿不到
    任何参数（reply 工具因此一直报缺少 reply_reference）。
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            log.warning("WorkBuddy: tool_call 参数不是合法 JSON，已按空对象处理: %s", raw[:200])
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


_REPLY_HINTS: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_REPLY_HINTS_MAX = 64
_HINT_MARK = "【本次表达倾向】"


def _format_intent(intent: dict[str, Any]) -> str:
    labels = (
        ("scene", "场景"),
        ("reply_act", "回复目的"),
        ("tone", "语气"),
        ("prefer", "偏好"),
        ("avoid", "避免"),
    )
    lines: list[str] = []
    for key, label in labels:
        value = intent.get(key)
        if isinstance(value, (list, tuple)):
            text = "、".join(str(item).strip() for item in value if str(item).strip())
        elif value is None:
            text = ""
        else:
            text = str(value).strip()
        if text:
            lines.append(f"- {label}：{text}")
    return "\n".join(lines)


def _remember_reply_args(tool_calls: list[dict[str, Any]]) -> None:
    """记住规划模型给出的表达倾向，等回复模型请求到来时补给它。

    新版宿主只把 ``chat_anlyze`` 塞进回复模型提示词，``expression_intent`` 里的
    语气/避免等信息只喂给了表达选择器，回复模型拿不到。这里按 chat_anlyze 原文
    做键缓存，回复请求出现同一段文字时再把倾向补进去。
    """
    for call in tool_calls:
        fn = call.get("function") or {}
        if fn.get("name") != "reply":
            continue
        args = fn.get("arguments")
        if not isinstance(args, dict):
            continue
        key = str(args.get("chat_anlyze") or "").strip()
        intent = args.get("expression_intent")
        if not key or not isinstance(intent, dict) or not intent:
            continue
        _REPLY_HINTS[key] = intent
        _REPLY_HINTS.move_to_end(key)
        while len(_REPLY_HINTS) > _REPLY_HINTS_MAX:
            _REPLY_HINTS.popitem(last=False)


def _inject_expression_hints(messages: list[dict[str, Any]]) -> bool:
    """在回复请求里补充规划模型给出的表达倾向。"""
    if not _REPLY_HINTS:
        return False
    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str) or _HINT_MARK in content:
            continue
        for key, intent in reversed(list(_REPLY_HINTS.items())):
            if not key or key not in content:
                continue
            block = _format_intent(intent)
            if not block:
                continue
            message["content"] = (
                content
                + f"\n\n{_HINT_MARK}（规划模型给出的表达倾向，回复时据此把握分寸）\n"
                + block
            )
            return True
    return False


class WorkBuddyProvider(LLMProviderBase):
    """把 Host 的 response 请求转发给 WorkBuddy 账号池。"""

    def __init__(self, service: WorkBuddyService, default_model: str, *, prefs=None) -> None:
        self._service = service
        self._default_model = default_model
        self._prefs = prefs

    async def get_response(self, request: dict[str, Any]) -> dict[str, Any]:
        api_provider = request.get("api_provider") or {}
        model_info = request.get("model_info") or {}
        model = (
            model_info.get("model_identifier")
            or api_provider.get("model")
            or self._default_model
        )
        extra = request.get("extra_params") or {}
        messages = context_items_to_messages(request.get("context_items") or [])
        if not messages:
            raise ValueError("WorkBuddy: 请求里没有可发送的 context_items")

        log.info(
            "WorkBuddy 调用: model=%s messages=%d tools=%d",
            model,
            len(messages),
            len(request.get("tool_options") or []),
        )

        payload: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        max_tokens = request.get("max_tokens") or extra.get("max_tokens")
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        temperature = request.get("temperature")
        if temperature is not None:
            payload["temperature"] = float(temperature)
        tools = tool_options_to_openai(request.get("tool_options"))
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = request.get("tool_choice") or "auto"
        fmt = request.get("response_format")
        if isinstance(fmt, dict) and fmt:
            payload["response_format"] = fmt
        for key in ("top_p", "presence_penalty", "frequency_penalty", "stop"):
            if key in extra and extra[key] is not None:
                payload[key] = extra[key]
        # 模型级思考开关：off 会明确要求上游关掉推理，其余档位原样透传。
        if self._prefs is not None:
            level = self._prefs.reasoning_for(model)
            if level:
                payload["reasoning_effort"] = level

        if _inject_expression_hints(messages):
            log.info("WorkBuddy: 已把规划模型的表达倾向补充进回复请求")

        data = await self._service.complete(payload)
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        usage = data.get("usage") or {}

        result: dict[str, Any] = {
            "content": message.get("content") or "",
            "usage": {
                "model_name": model,
                "provider_name": "WorkBuddy",
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
            },
            "raw_data": data,
        }
        reasoning = message.get("reasoning_content") or message.get("reasoning")
        if reasoning:
            result["reasoning_content"] = reasoning

        tool_calls = message.get("tool_calls")
        if tool_calls:
            cleaned: list[dict[str, Any]] = []
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                name = str(fn.get("name") or "").strip()
                if not name:
                    log.warning(
                        "WorkBuddy: 上游返回的 tool_call 缺少 function.name，已跳过: %s",
                        str(tc)[:200],
                    )
                    continue
                cleaned.append(
                    {
                        "id": tc.get("id") or f"call_{secrets.token_hex(8)}",
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": _arguments_dict(fn.get("arguments")),
                        },
                    }
                )
            if cleaned:
                result["tool_calls"] = cleaned
                _remember_reply_args(cleaned)
        if not result["content"] and not result.get("tool_calls"):
            log.warning(
                "WorkBuddy: 上游返回空内容 model=%s finish=%s usage=%s reasoning_len=%d "
                "max_tokens=%s response_format=%s messages=%d",
                model,
                choice.get("finish_reason"),
                usage,
                len(reasoning or ""),
                payload.get("max_tokens"),
                bool(payload.get("response_format")),
                len(payload.get("messages") or []),
            )
        return result
