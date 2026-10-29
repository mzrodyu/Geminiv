"""Defensive validation for client-supplied JSON Schemas."""

import json
from typing import Any


MAX_SCHEMA_DEPTH = 32
MAX_SCHEMA_NODES = 10_000
MAX_SCHEMA_SERIALIZED_BYTES = 1_000_000
_JSON_SCHEMA_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}
_SCHEMA_MAP_KEYS = {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
_SCHEMA_SINGLE_KEYS = {"items", "additionalProperties", "not", "contains", "propertyNames", "if", "then", "else"}
_SCHEMA_LIST_KEYS = {"oneOf", "anyOf", "allOf", "prefixItems"}


class SchemaValidationError(ValueError):
    """A client schema is malformed or exceeds defensive complexity limits."""


def validate_json_schema(schema: Any, *, label: str = "schema") -> None:
    if not isinstance(schema, dict):
        raise SchemaValidationError(f"{label} must be a JSON object")
    try:
        serialized = json.dumps(schema, ensure_ascii=False, separators=(",", ":"), check_circular=True)
    except (TypeError, ValueError, RecursionError) as exc:
        raise SchemaValidationError(f"{label} is not a valid JSON Schema object: {exc}") from None
    if len(serialized.encode("utf-8")) > MAX_SCHEMA_SERIALIZED_BYTES:
        raise SchemaValidationError(
            f"{label} exceeds the {MAX_SCHEMA_SERIALIZED_BYTES}-byte serialized-size limit")

    nodes = 0
    active: set[int] = set()

    def visit(node: Any, depth: int, path: str) -> None:
        nonlocal nodes
        if depth > MAX_SCHEMA_DEPTH:
            raise SchemaValidationError(f"{label} exceeds maximum depth {MAX_SCHEMA_DEPTH} at {path}")
        nodes += 1
        if nodes > MAX_SCHEMA_NODES:
            raise SchemaValidationError(f"{label} exceeds maximum node count {MAX_SCHEMA_NODES}")
        if not isinstance(node, dict):
            raise SchemaValidationError(f"{path} must be a JSON object")
        ident = id(node)
        if ident in active:
            raise SchemaValidationError(f"{label} contains a circular reference at {path}")
        active.add(ident)
        try:
            raw_type = node.get("type")
            types = raw_type if isinstance(raw_type, list) else [raw_type]
            if raw_type is not None:
                if not types or any(not isinstance(t, str) or t not in _JSON_SCHEMA_TYPES for t in types):
                    raise SchemaValidationError(f"{path}.type contains an unsupported JSON Schema type")
            required = node.get("required")
            if required is not None and (not isinstance(required, list)
                                         or any(not isinstance(name, str) for name in required)):
                raise SchemaValidationError(f"{path}.required must be an array of strings")

            for key in _SCHEMA_MAP_KEYS:
                value = node.get(key)
                if value is None:
                    continue
                if not isinstance(value, dict):
                    raise SchemaValidationError(f"{path}.{key} must be an object")
                for name, child in value.items():
                    if not isinstance(name, str):
                        raise SchemaValidationError(f"{path}.{key} keys must be strings")
                    visit(child, depth + 1, f"{path}.{key}.{name}")

            for key in _SCHEMA_SINGLE_KEYS:
                value = node.get(key)
                if value is None or isinstance(value, bool):
                    continue
                if key == "items" and isinstance(value, list):
                    for index, child in enumerate(value):
                        visit(child, depth + 1, f"{path}.{key}[{index}]")
                else:
                    visit(value, depth + 1, f"{path}.{key}")

            for key in _SCHEMA_LIST_KEYS:
                value = node.get(key)
                if value is None:
                    continue
                if not isinstance(value, list):
                    raise SchemaValidationError(f"{path}.{key} must be an array")
                for index, child in enumerate(value):
                    visit(child, depth + 1, f"{path}.{key}[{index}]")
        finally:
            active.remove(ident)

    visit(schema, 0, label)


def validate_request_schemas(request: Any) -> None:
    for index, tool in enumerate(getattr(request, "tools", None) or []):
        if not isinstance(tool, dict) or (tool.get("type") != "function" and "function" not in tool):
            continue
        function = tool.get("function")
        if not isinstance(function, dict):
            raise SchemaValidationError(f"tools[{index}].function must be an object")
        parameters = function.get("parameters")
        if parameters is not None:
            validate_json_schema(parameters, label=f"tools[{index}].function.parameters")

    response_format = getattr(request, "response_format", None)
    if isinstance(response_format, dict) and response_format.get("type") == "json_schema":
        json_schema = response_format.get("json_schema")
        if not isinstance(json_schema, dict):
            raise SchemaValidationError("response_format.json_schema must be an object")
        schema = json_schema.get("schema")
        validate_json_schema(schema, label="response_format.json_schema.schema")


# ============================================================
# 工具 Schema 净化：把 OpenAI / Pydantic 风格的 JSON Schema 压成
# Gemini function_declarations 能吃下的 OpenAPI Schema 子集。
# 直接原样下发会触发 400 INVALID_ARGUMENT（Unknown name "$defs"/"$ref"/
# "patternProperties"/"if"/"prefixItems"/"const" ...），这是工具调用最常见的坑。
# ============================================================

# Gemini 官方支持的字段（不在这张表里的键一律丢弃）
_GEMINI_SCHEMA_KEYS = {
    "type", "format", "description", "nullable", "enum", "items", "properties",
    "required", "minItems", "maxItems", "minimum", "maximum", "minLength",
    "maxLength", "pattern", "title", "default", "propertyOrdering", "anyOf",
}

_MAX_SANITIZE_DEPTH = 24


def _resolve_local_ref(node: Any, root: Any) -> Any:
    """把 "#/$defs/Foo" / "#/definitions/Foo" 这类本地引用就地展开。"""
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if not isinstance(ref, str):
        return node
    if not ref.startswith("#/"):
        return {k: v for k, v in node.items() if k != "$ref"}
    cursor = root
    for seg in ref[2:].split("/"):
        seg = seg.replace("~1", "/").replace("~0", "~")
        if isinstance(cursor, dict) and seg in cursor:
            cursor = cursor[seg]
        else:
            return {k: v for k, v in node.items() if k != "$ref"}
    merged = dict(cursor) if isinstance(cursor, dict) else {}
    # 同级的 description / default 等覆盖被引用定义上的同名字段
    merged.update({k: v for k, v in node.items() if k != "$ref"})
    return merged


def sanitize_tool_schema(schema: Any, _root: Any = None, _depth: int = 0) -> dict:
    """返回只含 Gemini 可接受字段的 schema（无法识别的关键字安全丢弃）。"""
    if not isinstance(schema, dict):
        return {}
    if _depth > _MAX_SANITIZE_DEPTH:
        return {}
    root = _root if isinstance(_root, dict) else schema

    node = _resolve_local_ref(schema, root)

    # allOf：合并成一个对象；oneOf / anyOf：只保留非 null 分支
    for combiner in ("allOf", "oneOf", "anyOf"):
        branches = node.get(combiner)
        if not isinstance(branches, list) or not branches:
            continue
        resolved = [_resolve_local_ref(b, root) for b in branches]
        resolved = [b for b in resolved if isinstance(b, dict)]
        non_null = [b for b in resolved if b.get("type") != "null"]
        nullable = len(non_null) != len(resolved)
        rest = {k: v for k, v in node.items() if k not in ("allOf", "oneOf", "anyOf")}
        if combiner == "allOf":
            merged: dict = {}
            for branch in non_null:
                merged.update(sanitize_tool_schema(branch, root, _depth + 1))
            node = {**rest, **merged}
        elif len(non_null) == 1:
            node = {**sanitize_tool_schema(non_null[0], root, _depth + 1), **rest}
        else:
            node = {**rest, "anyOf": [sanitize_tool_schema(b, root, _depth + 1) for b in non_null]}
        if nullable:
            node["nullable"] = True

    out: dict = {}
    for key, value in node.items():
        if key not in _GEMINI_SCHEMA_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            cleaned_props = {}
            for name, child in value.items():
                if isinstance(name, str):
                    cleaned_props[name] = sanitize_tool_schema(child, root, _depth + 1)
            out["properties"] = cleaned_props
        elif key == "items":
            if isinstance(value, list):
                out["items"] = sanitize_tool_schema(value[0], root, _depth + 1) if value else {}
            else:
                out["items"] = sanitize_tool_schema(value, root, _depth + 1)
        elif key == "anyOf" and isinstance(value, list):
            out["anyOf"] = [sanitize_tool_schema(b, root, _depth + 1) for b in value if isinstance(b, dict)]
        elif key == "required" and isinstance(value, list):
            names = [n for n in value if isinstance(n, str)]
            if names:
                out["required"] = names
        else:
            out[key] = value

    # 客户端常用的 const → enum（Gemini 只认 enum）
    if "const" in node and "enum" not in out:
        out["enum"] = [node["const"]]

    raw_type = out.get("type")
    if isinstance(raw_type, list):
        non_null_types = [t for t in raw_type if t != "null"]
        if len(non_null_types) != len(raw_type):
            out["nullable"] = True
        if len(non_null_types) == 1:
            out["type"] = non_null_types[0]
        elif non_null_types:
            out["type"] = non_null_types
        else:
            out.pop("type", None)

    # 有 properties 却没写 type 的对象，补上 type=object（Gemini 必需）
    if out.get("properties") and "type" not in out and "anyOf" not in out:
        out["type"] = "object"
    return out

