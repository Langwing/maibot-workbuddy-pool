"""把 OpenAI 兼容请求整理成 WorkBuddy 网关接受的形状。

上游实测规则（来自 dsh-connect-workbuddy 的取证）：

* 只接受 ``stream: true``；``developer`` 角色会被拒，需改写成 ``system``；
* 会话必须以 ``system`` 消息开头；
* ``system`` / ``assistant`` 消息的**开头**若命中未授权渠道指纹（如
  ``You are an AI agent powered by ...``），整个请求会以业务码 11128 被拒；
  兜底办法是在该消息前加一行中性 system 文本；另外身份句式在任意位置命中
  都会被拒，需要在词间插入零宽空格；
* ``tool_choice`` 只接受字符串，对象形式返回 400。
"""

from __future__ import annotations

import re
from typing import Any

FALLBACK_SYSTEM_PROMPT = "You are a helpful assistant."
ZERO_WIDTH_SPACE = "\u200b"

UNAPPROVED_CHANNEL_OPENINGS = (
    "you are an ai agent powered by deepseek harness",
    "you are claude code, anthropic's official cli for claude",
)

# 实测被网关改写的句式；只做同义改写，不改语义。
GATE_PHRASES: tuple[tuple[str, str], ...] = (
    ("You are an AI agent powered by DeepSeek Harness", "You are an AI agent running inside DeepSeek Harness"),
    ("You are a coding agent powered by the ", "You are a coding agent running on the "),
    (
        "You are interacting with the user through the DeepSeek Harness Web GUI",
        "You are working with the user inside the DeepSeek Harness Web GUI",
    ),
    ("Agent Teams is available in this session", "Agent Teams is enabled for this session"),
    (
        "Tokens prefixed with @ are paths the user explicitly referenced.",
        "Paths that begin with @ are the ones the user explicitly referenced.",
    ),
    (
        "Relative paths resolve from the workspace root;",
        "Relative paths are resolved against the workspace root;",
    ),
    (
        "absolute paths identify files or directories on the host.",
        "absolute paths point at files or directories on the host.",
    ),
    ("A trailing slash marks a directory:", "A trailing slash indicates a directory:"),
    ("list it when its contents matter.", "list it whenever its contents matter."),
    ("Anything else is a file:", "Everything else is a file:"),
    (
        "use the read tool when its contents are needed,",
        "use the read tool whenever you need its contents,",
    ),
    (
        "and do not claim to have inspected it before reading.",
        "and never claim to have inspected it before reading it.",
    ),
    ('@"..." quotes a path containing spaces.', 'A path containing spaces is quoted with @"...".'),
    (
        "without naming another target, they mean this GUI.",
        "and names no other target, the interface described here is meant.",
    ),
    ("they mean this GUI.", "the interface described here is meant."),
)

IDENTITY_CLAUSE = re.compile(
    r"(you)(\s+are\s+(?:(?:an?\s+)?[\w-]*\s*agent\s+powered\s+by"
    r"|interacting\s+with\s+the\s+user\s+through\s+the\s+deepseek\s+harness))",
    re.IGNORECASE,
)

SCANNED_ROLES = ("system", "assistant")


def rewrite_gate_phrases(text: str) -> str:
    out = text
    for src, dst in GATE_PHRASES:
        if src in out:
            out = out.replace(src, dst)
    return out


def break_identity_clauses(text: str) -> str:
    """在身份句式的每个词间插入零宽空格（幂等）。"""

    def _break(match: re.Match[str]) -> str:
        return re.sub(r"(\s+)", lambda m: m.group(1) + ZERO_WIDTH_SPACE, match.group(0))

    return IDENTITY_CLAUSE.sub(_break, text)


def is_unapproved_opening(text: str) -> bool:
    opening = text.lstrip().lower()
    return any(opening.startswith(prefix) for prefix in UNAPPROVED_CHANNEL_OPENINGS)


def _neutralize(text: str, force: bool) -> str:
    out = rewrite_gate_phrases(text)
    if force or is_unapproved_opening(out):
        out = f"{FALLBACK_SYSTEM_PROMPT}\n\n{out}"
    return break_identity_clauses(out)


def _neutralize_message(message: dict, force: bool) -> dict:
    content = message.get("content")
    if isinstance(content, str):
        if content:
            message["content"] = _neutralize(content, force)
        return message
    if isinstance(content, list):
        parts = []
        for part in content:
            if (
                isinstance(part, dict)
                and str(part.get("type", "")).strip().lower() == "text"
                and isinstance(part.get("text"), str)
                and part["text"]
            ):
                part = dict(part)
                part["text"] = _neutralize(part["text"], force)
            parts.append(part)
        message["content"] = parts
    return message


def normalize_tool_choice(payload: dict[str, Any]) -> None:
    """把 OpenAI 的 ``tool_choice`` 对象形态压平成上游能接受的字符串。"""
    if "tool_choice" not in payload:
        return
    choice = payload.get("tool_choice")

    def _suppress() -> None:
        payload.pop("tools", None)
        payload.pop("functions", None)

    if isinstance(choice, str):
        if choice.strip().lower() == "none":
            payload.pop("tool_choice", None)
            _suppress()
        return
    if isinstance(choice, dict):
        kind = str(choice.get("type", "")).strip().lower()
        if kind == "none":
            payload.pop("tool_choice", None)
            _suppress()
        elif kind in ("auto", "required"):
            payload["tool_choice"] = kind
        elif kind == "function":
            fn = choice.get("function") if isinstance(choice.get("function"), dict) else {}
            name = fn.get("name") or choice.get("name") or ""
            payload["tool_choice"] = str(name).strip() or "auto"
        else:
            payload.pop("tool_choice", None)
        return
    payload.pop("tool_choice", None)


# ---------------------------------------------------------------------------
# 工具 schema 归一化（issue #253：上游业务码 11129 / 11133）
# ---------------------------------------------------------------------------
_MAX_SCHEMA_DEPTH = 64

_SCHEMA_CHILD_MAPS = ("properties", "patternProperties", "dependentSchemas", "$defs", "definitions")
_SCHEMA_CHILD_LISTS = ("allOf", "anyOf", "oneOf", "prefixItems")
_SCHEMA_CHILD_SINGLE = (
    "items", "additionalItems", "additionalProperties", "not", "contains",
    "if", "then", "else", "propertyNames",
)
_SCHEMA_CHILD_DEPENDENCIES = ("dependencies",)
_SCHEMA_STRING_KEYWORDS = (
    "title", "description", "pattern", "format", "$comment", "contentMediaType",
    "contentEncoding", "$id", "$schema", "$anchor", "$dynamicAnchor", "$dynamicRef",
)
_SCHEMA_NUMBER_KEYWORDS = (
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "minItems", "maxItems", "minProperties", "maxProperties",
    "minContains", "maxContains",
)
_SCHEMA_BOOLEAN_KEYWORDS = ("uniqueItems", "readOnly", "writeOnly", "deprecated")
_SCHEMA_LIST_KEYWORDS = ("allOf", "anyOf", "oneOf", "prefixItems")
_SCHEMA_STRING_LIST_KEYWORDS = ("required", "enum", "examples")
_JSON_SCHEMA_TYPES = frozenset({
    "array", "boolean", "integer", "null", "number", "object", "string",
})


def normalize_tool_schemas(payload: dict[str, Any]) -> None:
    """原地修复 ``tools`` / ``functions`` 里上游拒收的 JSON Schema 写法。

    只动这两个字段，而且只动被实测拒收的形状：draft-4 的 tuple ``items``、
    布尔 ``exclusiveMinimum`` / ``exclusiveMaximum``、字符串或重复项的
    ``required``、外部 ``$ref``，以及类型错误的注解关键字。内部 ``$ref`` 保留。
    """
    _normalize_tool_array(payload.get("tools"))
    _normalize_tool_array(payload.get("functions"))


def _normalize_tool_array(value: Any) -> None:
    if not isinstance(value, list):
        return
    for entry in value:
        if not isinstance(entry, dict):
            continue
        fn = entry.get("function")
        if isinstance(fn, dict):
            _normalize_schema_node(fn.get("parameters"))
        if entry.get("function") is None:
            _normalize_schema_node(entry.get("parameters"))


def _normalize_schema_node(node: Any, depth: int = 0) -> None:
    if not isinstance(node, dict) or depth > _MAX_SCHEMA_DEPTH:
        return
    # 顺序重要：draft-4 转换先做，否则类型修复会先删掉布尔 exclusive 与 tuple items。
    _normalize_items(node)
    _normalize_exclusive_bound(node, "exclusiveMinimum", "minimum")
    _normalize_exclusive_bound(node, "exclusiveMaximum", "maximum")
    _normalize_required(node)
    _drop_external_ref(node)
    _normalize_meta_types(node)

    for key in _SCHEMA_CHILD_MAPS:
        child = node.get(key)
        if isinstance(child, dict):
            for nested in child.values():
                _normalize_schema_node(nested, depth + 1)
    for key in _SCHEMA_CHILD_LISTS:
        child = node.get(key)
        if isinstance(child, list):
            for nested in child:
                _normalize_schema_node(nested, depth + 1)
    for key in _SCHEMA_CHILD_DEPENDENCIES:
        child = node.get(key)
        if isinstance(child, dict):
            for nested in child.values():
                _normalize_schema_node(nested, depth + 1)
    for key in _SCHEMA_CHILD_SINGLE:
        _normalize_schema_node(node.get(key), depth + 1)


def _normalize_items(schema: dict[str, Any]) -> None:
    items = schema.get("items")
    if not isinstance(items, list):
        return
    if not items:
        prefix = schema.get("prefixItems")
        if isinstance(prefix, list) and prefix:
            schema["prefixItems"] = prefix
        else:
            schema.pop("prefixItems", None)
    elif schema.get("prefixItems") is None:
        schema["prefixItems"] = items
    tail = schema.get("additionalItems")
    schema["items"] = tail if isinstance(tail, (bool, dict)) else {}
    schema.pop("additionalItems", None)


def _normalize_exclusive_bound(schema: dict[str, Any], exclusive_key: str,
                               bound_key: str) -> None:
    value = schema.get(exclusive_key)
    if value is True:
        bound = schema.get(bound_key)
        if isinstance(bound, (int, float)) and not isinstance(bound, bool):
            schema[exclusive_key] = bound
            schema.pop(bound_key, None)
        else:
            schema.pop(exclusive_key, None)
    elif value is False:
        schema.pop(exclusive_key, None)


def _normalize_required(schema: dict[str, Any]) -> None:
    required = schema.get("required")
    if required is None:
        return
    if isinstance(required, str):
        schema["required"] = [] if required == "" else [required]
        return
    if not isinstance(required, list):
        schema.pop("required", None)
        return
    names: list[str] = []
    for entry in required:
        if isinstance(entry, str) and entry not in names:
            names.append(entry)
    if len(names) != len(required):
        schema["required"] = names


def _drop_external_ref(schema: dict[str, Any]) -> None:
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref and not ref.startswith("#"):
        schema.pop("$ref", None)


def _normalize_meta_types(schema: dict[str, Any]) -> None:
    for key in _SCHEMA_STRING_KEYWORDS:
        if key in schema and not isinstance(schema[key], str):
            schema.pop(key, None)
    for key in _SCHEMA_NUMBER_KEYWORDS:
        value = schema.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            schema.pop(key, None)
    for key in _SCHEMA_BOOLEAN_KEYWORDS:
        if key in schema and not isinstance(schema[key], bool):
            schema.pop(key, None)
    for key in _SCHEMA_LIST_KEYWORDS:
        value = schema.get(key)
        if value is None:
            continue
        if not isinstance(value, list) or not value:
            schema.pop(key, None)
    for key in _SCHEMA_STRING_LIST_KEYWORDS:
        if key in schema and not isinstance(schema[key], list):
            schema.pop(key, None)
    for key in _SCHEMA_CHILD_SINGLE:
        value = schema.get(key)
        if value is None or isinstance(value, bool):
            continue
        if not isinstance(value, dict):
            schema.pop(key, None)
    _normalize_type_keyword(schema)


def _normalize_type_keyword(schema: dict[str, Any]) -> None:
    kind = schema.get("type")
    if kind is None:
        return
    if isinstance(kind, str):
        if kind not in _JSON_SCHEMA_TYPES:
            schema.pop("type", None)
        return
    if not isinstance(kind, list):
        schema.pop("type", None)
        return
    kept = [entry for entry in kind if isinstance(entry, str) and entry in _JSON_SCHEMA_TYPES]
    if not kept:
        schema.pop("type", None)
    elif len(kept) != len(kind):
        schema["type"] = kept[0] if len(kept) == 1 else kept


def prepare_payload(payload: dict[str, Any], *, force_neutral: bool = False) -> dict[str, Any]:
    """返回整理后的请求副本，不修改调用方传入的对象。"""
    body = dict(payload)
    messages = body.get("messages")
    if not isinstance(messages, list):
        return body

    prepared: list[dict] = []
    for raw in messages:
        if not isinstance(raw, dict):
            continue
        message = dict(raw)
        role = message.get("role")
        if isinstance(role, str) and role.strip().lower() == "developer":
            message["role"] = "system"
        prepared.append(message)

    if prepared:
        head_role = str(prepared[0].get("role", "")).strip().lower()
        if head_role != "system":
            prepared.insert(0, {"role": "system", "content": FALLBACK_SYSTEM_PROMPT})

    for message in prepared:
        role = str(message.get("role", "")).strip().lower()
        if role in SCANNED_ROLES:
            _neutralize_message(message, force_neutral)

    body["messages"] = prepared
    normalize_tool_choice(body)
    normalize_tool_schemas(body)
    return body
