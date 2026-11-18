"""HTTP and prompt-cache support for a local llama.cpp server."""
import json
import urllib.error
import urllib.parse
import urllib.request

from . import config


class LlamaCppError(RuntimeError):
    pass


_cache_restore_attempted = set()


def route_for(model=None):
    if config.LLAMA_CPP_FAST_URL and model == config.FAST_MODEL:
        return {
            "url": config.LLAMA_CPP_FAST_URL,
            "model": config.LLAMA_CPP_FAST_MODEL,
            "slot": config.LLAMA_CPP_FAST_SLOT,
            "cache_dir": config.LLAMA_CPP_FAST_CACHE_DIR,
            "cache_file": config.LLAMA_CPP_FAST_CACHE_FILE,
            "api_key": config.LLAMA_CPP_FAST_API_KEY,
        }
    return {
        "url": config.LLAMA_CPP_URL,
        "model": config.LLAMA_CPP_MODEL,
        "slot": config.LLAMA_CPP_SLOT,
        "cache_dir": config.LLAMA_CPP_CACHE_DIR,
        "cache_file": config.LLAMA_CPP_CACHE_FILE,
        "api_key": config.LLAMA_CPP_API_KEY,
    }


def model_name(model=None):
    return route_for(model)["model"]


def endpoint(path, model=None):
    return route_for(model)["url"] + path


def headers(model=None):
    result = {"Content-Type": "application/json"}
    api_key = route_for(model)["api_key"]
    if api_key:
        result["Authorization"] = "Bearer " + api_key
    return result


def _messages(messages):
    result = []
    pending_calls = {}
    for message in messages:
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            calls = []
            for index, call in enumerate(message.get("tool_calls") or []):
                function = dict(call.get("function") or {})
                name = str(function.get("name") or "")
                call_id = str(call.get("id") or f"sage-call-{len(result)}-{index}")
                arguments = function.get("arguments", {})
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False)
                function["arguments"] = arguments
                calls.append({"id": call_id, "type": "function", "function": function})
                pending_calls.setdefault(name, []).append(call_id)
            result.append({"role": "assistant", "content": message.get("content") or "", "tool_calls": calls})
        elif role == "tool":
            name = str(message.get("tool_name") or "")
            call_id = message.get("tool_call_id")
            if not call_id and pending_calls.get(name):
                call_id = pending_calls[name].pop(0)
            if not call_id:
                call_id = f"sage-tool-{len(result)}"
            result.append({"role": "tool", "tool_call_id": str(call_id), "content": str(message.get("content") or "")})
        else:
            item = {"role": role, "content": message.get("content") or ""}
            if message.get("name"):
                item["name"] = message["name"]
            result.append(item)
    return result


def request_body(data):
    options = data.get("options") or {}
    route = route_for(data.get("model"))
    body = {
        "model": route["model"],
        "messages": _messages(data.get("messages") or []),
        "stream": bool(data.get("stream")),
        "id_slot": route["slot"],
        "extra_params": {"cache_prompt": True},
        "chat_template_kwargs": {"enable_thinking": bool(data.get("think", False))},
    }
    if options.get("num_predict") is not None:
        body["max_tokens"] = int(options["num_predict"])
    if data.get("tools"):
        body["tools"] = data["tools"]
    if data.get("stop"):
        body["stop"] = data["stop"]
    if data.get("format"):
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "sage_response", "strict": True, "schema": data["format"]},
        }
    if data.get("stream"):
        body["stream_options"] = {"include_usage": True}

    model = str(data.get("model") or "")
    if model == config.CODING_MODEL:
        body["temperature"], body["top_p"] = 0.2, 0.9
    elif model == config.REASONING_MODEL:
        body["temperature"], body["top_p"] = 0.6, 0.95
    else:
        body["temperature"], body["top_p"] = 0.6, 0.9
    for key in ("temperature", "top_p", "seed"):
        if options.get(key) is not None:
            body[key] = options[key]
    return body


def _cache_request(action, model=None, timeout=30):
    route = route_for(model)
    path = f"/slots/{route['slot']}?action={action}"
    data = json.dumps({"filename": route["cache_file"]}).encode()
    request = urllib.request.Request(endpoint(path, model), data=data, headers=headers(model), method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read())
    if result.get("error"):
        raise LlamaCppError(f"llama.cpp cache {action} failed: {result['error']}")
    return result


def restore_cache(model=None):
    route = route_for(model)
    key = (route["url"], route["slot"], str(route["cache_dir"]), route["cache_file"])
    if key in _cache_restore_attempted:
        return
    _cache_restore_attempted.add(key)
    cache_file = route["cache_dir"] / route["cache_file"]
    if not cache_file.is_file():
        return
    try:
        _cache_request("restore", model)
    except (urllib.error.HTTPError, LlamaCppError, ValueError) as exc:
        detail=exc.read().decode("utf-8","replace") if isinstance(exc,urllib.error.HTTPError) else str(exc)
        code=f" ({exc.code})" if isinstance(exc,urllib.error.HTTPError) else ""
        print(f"Sage: could not restore llama.cpp prompt cache{code}: {detail[:300]}", flush=True)


def save_cache(model=None):
    try:
        _cache_request("save", model, timeout=120)
    except (urllib.error.HTTPError, urllib.error.URLError, LlamaCppError, OSError, ValueError) as exc:
        print(f"Sage: llama.cpp reply succeeded, but saving its prompt cache failed: {exc}", flush=True)


def response_message(data):
    if data.get("error"):
        raise LlamaCppError(f"llama.cpp returned an error: {data['error']}")
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise LlamaCppError("llama.cpp returned no chat completion choice")
    source = choices[0].get("message") or {}
    message = {"role": "assistant", "content": source.get("content") or ""}
    if source.get("tool_calls"):
        message["tool_calls"] = source["tool_calls"]
    thinking = source.get("reasoning_content") or source.get("reasoning")
    if thinking:
        message["thinking"] = thinking
    return message


def speed_data(data):
    usage = data.get("usage") or {}
    timings = data.get("timings") or {}
    prompt_tokens = usage.get("prompt_tokens", timings.get("prompt_n"))
    cached_tokens = timings.get("cache_n", 0)
    evaluated = max(0, int(prompt_tokens or 0) - int(cached_tokens or 0)) if prompt_tokens is not None else None
    generated = usage.get("completion_tokens", timings.get("predicted_n"))
    prompt_ms = timings.get("prompt_ms")
    generated_ms = timings.get("predicted_ms")
    return {
        "eval_count": generated,
        "eval_duration": float(generated_ms or 0) * 1_000_000,
        "prompt_eval_count": evaluated,
        "prompt_eval_duration": float(prompt_ms or 0) * 1_000_000,
    }
