"""流式收尾 + 工具调用加固的冒烟测试。

覆盖本次修复的两类线上故障：
  1. 上游流「没有 finish_reason / 没有 data: [DONE]」就结束 —— 客户端迭代后对残留
     空串做 json.loads("")，抛 JSONDecodeError: Expecting value: line 1 column 1 (char 0)。
  2. 工具 Schema 直接透传 OpenAI/Pydantic 关键字（$defs/$ref/$schema/patternProperties/
     prefixItems/const/if-then-else），被 Gemini 4.x 以 400 拒掉。
"""

import asyncio
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("STATE_DIR", tempfile.mkdtemp(prefix="v2o-test-"))
os.environ.setdefault("API_KEY", "test-key-1234")
sys.path.insert(0, os.path.join(ROOT, "app"))

from fastapi.responses import JSONResponse, StreamingResponse  # noqa: E402

import api_helpers  # noqa: E402
from models import OpenAIRequest  # noqa: E402
from schema_validation import sanitize_tool_schema  # noqa: E402

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))


class _State:
    express_key_manager = None


class _App:
    state = _State()


class FakeRequest:
    """最小可用的 FastAPI Request 替身。"""

    app = _App()

    async def is_disconnected(self):
        return False


def frames_to_text(frames):
    return [f if isinstance(f, str) else (f.decode("utf-8", "replace") if isinstance(f, bytes) else str(f))
            for f in frames]


def data_objects(frames):
    out = []
    for text in frames_to_text(frames):
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]" or not payload:
                continue
            out.append(json.loads(payload))
    return out


async def collect(agen):
    return [frame async for frame in agen]


# ---------------------------------------------------------------- 1) 收尾保护
async def test_terminate_tail():
    async def abrupt():
        yield 'data: {"choices":[{"delta":{"content":"你好"},"finish_reason":null}]}\n\n'

    frames = await collect(api_helpers._terminate_stream_safely(abrupt(), "gemini-4-argon"))
    text = "".join(frames_to_text(frames))
    check("abrupt stream gets finish_reason", '"finish_reason": "stop"' in text or '"finish_reason":"stop"' in text)
    check("abrupt stream ends with [DONE]", text.rstrip().endswith("data: [DONE]"))

    async def already_done():
        yield 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        yield "data: [DONE]\n\n"

    frames = await collect(api_helpers._terminate_stream_safely(already_done(), "m"))
    text = "".join(frames_to_text(frames))
    check("proper stream not double-terminated", text.count("data: [DONE]") == 1 and text.count('"finish_reason"') == 1)

    async def boom():
        yield 'data: {"choices":[{"delta":{"content":"半"},"finish_reason":null}]}\n\n'
        raise json.JSONDecodeError("Expecting value", "", 0)

    frames = await collect(api_helpers._terminate_stream_safely(boom(), "m"))
    objs = data_objects(frames)
    text = "".join(frames_to_text(frames))
    check("exception becomes error frame", any("error" in o for o in objs))
    check("exception stream ends with [DONE]", text.rstrip().endswith("data: [DONE]"))
    check("no raw traceback leaks", "Traceback" not in text)

    async def empty():
        if False:  # 一个字节都不产出就结束
            yield ""

    frames = await collect(api_helpers._terminate_stream_safely(empty(), "gemini-4-argon"))
    objs = data_objects(frames)
    text = "".join(frames_to_text(frames))
    check("empty stream yields readable error", any("上游流在产出任何内容前" in json.dumps(o, ensure_ascii=False) for o in objs))
    check("empty stream ends with [DONE]", text.rstrip().endswith("data: [DONE]"))


# ---------------------------------------------------------------- 2) Cookie 通道
class _FakeCookie:
    def __init__(self, inner):
        self._inner = inner

    def check_cookie_ready(self):
        return True, "", "demo-project", "", 200, ""

    async def execute_once(self, request_obj, fastapi_request):
        return self._inner()


async def _run_cookie_stream(inner_factory):
    req = OpenAIRequest(model="gemini-4-argon", messages=[{"role": "user", "content": "hi"}], stream=True)
    return await api_helpers.execute_unified_completion(
        request_obj=req,
        fastapi_request=FakeRequest(),
        primary_channel="cookie",
        express_upstream=type("E", (), {"prepare_express_context": lambda self, r: (None, None)})(),
        cookie_upstream=_FakeCookie(inner_factory),
    )


async def test_cookie_stream_without_done():
    async def body():
        yield 'data: {"choices":[{"delta":{"content":"工具"},"finish_reason":null}]}\n\n'

    resp = await _run_cookie_stream(lambda: StreamingResponse(body(), media_type="text/event-stream"))
    frames = [f async for f in resp.body_iterator]
    text = "".join(frames_to_text(frames))
    check("cookie stream without DONE ends with [DONE]", text.rstrip().endswith("data: [DONE]"))
    check("cookie stream without DONE gets finish", '"finish_reason": "stop"' in text)


async def test_cookie_json_response():
    resp = await _run_cookie_stream(lambda: JSONResponse(
        status_code=401, content={"error": {"message": "cookie expired, please refresh"}}))
    frames = [f async for f in resp.body_iterator]
    text = "".join(frames_to_text(frames))
    objs = data_objects(frames)
    check("json response converted to error frame", any("cookie expired" in json.dumps(o, ensure_ascii=False) for o in objs))
    check("json response stream ends with [DONE]", text.rstrip().endswith("data: [DONE]"))
    check("no AttributeError text", "body_iterator" not in text and "AttributeError" not in text)


# ---------------------------------------------------------------- 3) 工具 Schema
async def test_tool_schema():
    hairy = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "additionalProperties": False,
        "patternProperties": {"^x-": {"type": "string"}},
        "properties": {
            "city": {"type": ["string", "null"], "description": "城市"},
            "mode": {"const": "fast"},
            "pet": {"$ref": "#/$defs/Pet"},
            "tags": {"type": "array", "prefixItems": [{"type": "string"}], "items": {"type": "string"}},
            "when": {"if": {"type": "string"}, "then": {"type": "number"}},
        },
        "required": ["city", 123],
        "$defs": {"Pet": {"type": "object", "properties": {"name": {"type": "string"}}}},
    }
    clean = sanitize_tool_schema(hairy)
    dumped = json.dumps(clean, ensure_ascii=False)
    for banned in ("$schema", "$defs", "$ref", "patternProperties", "additionalProperties",
                   "prefixItems", "const", "if", "then"):
        check(f"schema drops {banned}", banned not in dumped)
    check("schema keeps object type", clean.get("type") == "object")
    check("schema nullable from type list", clean["properties"]["city"].get("nullable") is True)
    check("schema const becomes enum", clean["properties"]["mode"].get("enum") == ["fast"])
    check("schema resolves $ref", clean["properties"]["pet"].get("properties", {}).get("name", {}).get("type") == "string")
    check("schema keeps valid required only", clean.get("required") == ["city"])

    # 真实 OpenAIChat 工具声明 → 出站 config
    req = OpenAIRequest(
        model="gemini-4-argon",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {
            "name": "get_weather",
            "description": "查天气",
            "parameters": hairy,
        }}],
    )
    cfg = api_helpers.create_generation_config(req)
    decl = cfg["tools"][0]["function_declarations"][0]
    check("declaration name kept", decl["name"] == "get_weather")
    check("declaration schema sanitized", "$ref" not in json.dumps(decl["parameters"]))

    # tool_choice=none 不应再下发 tools
    req_none = OpenAIRequest(
        model="gemini-4-argon",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}],
        tool_choice="none",
    )
    check("tool_choice=none drops tools", "tools" not in api_helpers.create_generation_config(req_none))


# ---------------------------------------------------------------- 4) 工具结果回传
async def test_tool_call_replay():
    frames = await collect(api_helpers._chunk_openai_response_dict_for_sse(
        openai_response_dict={
            "id": "chatcmpl-test", "created": 1, "model": "gemini-4-argon",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None,
                "tool_calls": [
                    {"type": "function", "function": {"name": "get_weather", "arguments": ""}},
                    {"type": "function", "function": {"name": "no_args"}},
                ],
            }}],
        },
    ))
    text = "".join(frames_to_text(frames))
    check("empty arguments normalized to {}", '"arguments": "{}"' in text)
    check("missing id gets fallback", '"id": "call_1"' in text)
    check("fake stream ends with [DONE]", text.rstrip().endswith("data: [DONE]"))

    # 客户端回传的 assistant.tool_calls.arguments 为空串时不能炸
    from message_processing import create_gemini_prompt
    req = OpenAIRequest(
        model="gemini-4-argon",
        messages=[
            {"role": "user", "content": "天气"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": ""}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "晴 25℃"},
        ],
    )
    try:
        prompt = await asyncio.to_thread(create_gemini_prompt, req.messages, "gemini-4-argon")
        ok = bool(prompt)
        extra = ""
    except Exception as e:  # noqa: BLE001
        ok = False
        extra = f"{type(e).__name__}: {e}"
    check("empty tool arguments replay safe", ok, extra)


async def test_sse_strict_compat():
    """严格兼容模式：线上只出现 data: 行，最笨的逐行 json.loads 客户端也不会炸。"""
    from resumable import frame_with_id
    from runtime_state import app_state

    body_frame = 'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":null}]}\n\n'
    keepalive = ('data: {"id":"chatcmpl-keepalive","object":"chat.completion.chunk","created":0,'
                 '"model":"m","choices":[{"delta":{"content":""},"index":0,"finish_reason":null}]}\n\n')

    def naive_parse(body: str):
        """模拟「逐行 json.loads」的客户端（只会跳过空行和 [DONE]）。"""
        for line in body.splitlines():
            s = line.strip()
            if not s:
                continue
            if not s.startswith("data: "):
                raise json.JSONDecodeError("Expecting value", s, 0)
            payload = s[len("data: "):]
            if payload == "[DONE]":
                continue
            json.loads(payload)

    app_state.update_settings({"sse_strict_compat": True})
    strict_wire = "".join([frame_with_id(0, keepalive), frame_with_id(1, body_frame), frame_with_id(2, "data: [DONE]\n\n")])
    check("strict wire has no id: line", "id: " not in strict_wire)
    check("strict wire has no SSE comment", ": keep-alive" not in strict_wire)
    err = None
    try:
        naive_parse(strict_wire)
    except Exception as e:  # noqa: BLE001
        err = e
    check("naive client can parse strict stream", err is None, repr(err))

    # 关掉严格模式（= 旧行为）：复现用户上报的那条错误
    app_state.update_settings({"sse_strict_compat": False})
    legacy_wire = frame_with_id(0, body_frame)
    check("legacy wire has id: line", legacy_wire.startswith("id: "))
    err2 = None
    try:
        naive_parse(legacy_wire)
    except Exception as e:  # noqa: BLE001
        err2 = e
    check("legacy stream reproduces reported JSONDecodeError",
          isinstance(err2, json.JSONDecodeError) and "line 1 column 1" in str(err2),
          repr(err2))

    # 收尾保护产出的流也必须全是 data 行
    async def one_chunk():
        yield body_frame

    frames = await collect(api_helpers._terminate_stream_safely(one_chunk(), "m"))
    wire = "".join(frames_to_text(frames))
    non_data = [ln for ln in wire.splitlines() if ln.strip() and not ln.strip().startswith("data:")]
    check("terminate wrapper emits only data lines", not non_data, str(non_data[:3]))

    app_state.update_settings({"sse_strict_compat": True})


async def main():
    await test_terminate_tail()
    await test_cookie_stream_without_done()
    await test_cookie_json_response()
    await test_tool_schema()
    await test_tool_call_replay()
    await test_sse_strict_compat()
    failed = 0
    print("\n==== RESULT ====")
    for name, ok, extra in results:
        print(("PASS  " if ok else "FAIL  ") + name + (f"   << {extra}" if (extra and not ok) else ""))
        failed += 0 if ok else 1
    print(f"\n{len(results) - failed}/{len(results)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
