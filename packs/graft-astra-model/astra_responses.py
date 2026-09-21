"""A minimal Copilot Responses-API client — stdlib only, no pack dependencies.

`gpt-6-astra` advertises `supported_endpoints: ["/responses", "ws:/responses"]`
and nothing else, so brainstem's `/chat/completions` driver can never reach it.
That filter in `_fetch_copilot_models()` is CORRECT — it is what stops gpt-5.5
and the *-codex models from failing with `unsupported_api_for_model`. This module
routes around the filter instead of weakening it: it speaks `/responses` and
hands back something chat-completions shaped, so nothing upstream has to change.

Every shape below was verified against the live enterprise endpoint on
2026-09-20 (see docs/W2-REPORT.md for the verbatim probe):

    A  plain typed input .......................... 200
    B  top-level `instructions` + assistant turn ... 200
    C  FLATTENED tool schema ...................... 200, returns a function_call
    D  NESTED (chat-style) tool schema ............. 400 "Missing required
                                                    parameter: 'tools[0].name'"
    E  function_call + function_call_output ........ 200

D is why the translation in `to_responses_body` is mandatory rather than
cosmetic: handing the Responses API a chat-completions tool array is a hard 400.

No credential is stored, logged, or printed here. The host's own token is
preferred; the fallback only READS what the host already holds.
"""

import json
import os
import time
import urllib.error
import urllib.request

ASTRA_MODEL_ID = "gpt-6-astra"

#: What gets injected into the live AVAILABLE_MODELS. `supported_endpoints` is
#: load-bearing: it is the routing table the graft reads, and it is exactly the
#: field the grail's own filter looks at, so the two stay consistent.
ASTRA_ENTRY = {
    "id": ASTRA_MODEL_ID,
    "name": "GPT-6 Astra",
    "available": True,
    "supported_endpoints": ["/responses", "ws:/responses"],
    "grafted_by": "astra-model",
}

EDITOR_VERSION = "vscode/1.95.0"
INTEGRATION_ID = "vscode-chat"
GH_TOKEN_EXCHANGE = "https://api.github.com/copilot_internal/v2/token"

#: Lazy token cache. SPEC 4.3: a graft's module body runs on EVERY chat turn, so
#: no token work may happen at import — it happens here, on the first call.
_cache = {"token": None, "endpoint": None, "expires_at": 0.0}


class ResponsesError(Exception):
    def __init__(self, status, detail):
        Exception.__init__(self, "responses %s: %s" % (status, str(detail)[:300]))
        self.status = status
        self.detail = detail


# ── credentials (read the host's, never invent a second scheme) ──────────────

def _gh_oauth_token(main=None):
    paths = [os.path.join(os.path.expanduser("~"), ".brainstem", "state", ".copilot_token")]
    try:
        base = os.path.dirname(os.path.abspath(getattr(main, "__file__", "") or ""))
        if base:
            paths.append(os.path.join(base, ".copilot_token"))
    except Exception:
        pass
    for p in paths:
        try:
            raw = open(p, encoding="utf-8").read().strip()
        except Exception:
            continue
        if not raw:
            continue
        try:
            tok = json.loads(raw).get("access_token") if raw.startswith("{") else raw
        except Exception:
            tok = None
        if tok:
            return tok
    return (os.environ.get("GITHUB_TOKEN") or "").strip() or None


def _exchange(gh_token, timeout=20):
    req = urllib.request.Request(GH_TOKEN_EXCHANGE, headers={
        "Authorization": "token " + gh_token,
        "Editor-Version": EDITOR_VERSION,
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    endpoint = (data.get("endpoints") or {}).get("api")
    if not data.get("token") or not endpoint:
        raise ResponsesError(0, "token exchange returned no token/endpoint")
    return data["token"], endpoint, float(data.get("expires_at") or (time.time() + 1500))


def resolve_token(main=None, force=False):
    """(copilot_token, endpoint). Prefers the HOST's own resolution so there is
    exactly one credential path on the machine and one refresh policy."""
    if main is not None and callable(getattr(main, "get_copilot_token", None)):
        try:
            token, endpoint = main.get_copilot_token()
            if token and endpoint:
                return token, endpoint
        except Exception:
            pass  # fall through to our own read-only resolution
    if not force and _cache["token"] and time.time() < _cache["expires_at"] - 60:
        return _cache["token"], _cache["endpoint"]
    gh = _gh_oauth_token(main)
    if not gh:
        raise ResponsesError(0, "no GitHub token available for the Responses route")
    token, endpoint, exp = _exchange(gh)
    _cache.update({"token": token, "endpoint": endpoint, "expires_at": exp})
    return token, endpoint


# ── chat-completions  ->  responses ──────────────────────────────────────────

def _text_of(content):
    """Flatten a chat message's content into plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict):
                t = part.get("text")
                if isinstance(t, str):
                    out.append(t)
        return "".join(out)
    return str(content)


def to_responses_body(messages, tools=None, model=ASTRA_MODEL_ID,
                      max_output_tokens=4000, effort="low"):
    """Translate an OpenAI chat-completions request into a Responses request."""
    instructions = []
    items = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role in ("system", "developer"):
            t = _text_of(m.get("content"))
            if t:
                instructions.append(t)
            continue
        if role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": m.get("tool_call_id") or m.get("id") or "call_unknown",
                "output": _text_of(m.get("content")) or "",
            })
            continue
        if role == "assistant":
            t = _text_of(m.get("content"))
            if t:
                items.append({"role": "assistant",
                              "content": [{"type": "output_text", "text": t}]})
            for tc in (m.get("tool_calls") or []):
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                items.append({
                    "type": "function_call",
                    "call_id": tc.get("id") or "call_unknown",
                    "name": fn.get("name") or "unknown",
                    "arguments": fn.get("arguments") or "{}",
                })
            continue
        # everything else is a user turn
        t = _text_of(m.get("content"))
        if t:
            items.append({"role": "user", "content": [{"type": "input_text", "text": t}]})

    body = {
        "model": model,
        "input": items,
        "reasoning": {"effort": effort},
        "max_output_tokens": max_output_tokens,
        "stream": False,
    }
    if instructions:
        body["instructions"] = "\n\n".join(instructions)
    flat = []
    for t in (tools or []):
        if not isinstance(t, dict):
            continue
        fn = t.get("function") or {}
        name = fn.get("name") or t.get("name")
        if not name:
            continue
        # FLATTENED — nested chat-style tools are a hard 400 (probe D).
        flat.append({
            "type": "function",
            "name": name,
            "description": fn.get("description") or t.get("description") or "",
            "parameters": fn.get("parameters") or t.get("parameters")
            or {"type": "object", "properties": {}},
        })
    if flat:
        body["tools"] = flat
    return body


def flatten_body(messages, model=ASTRA_MODEL_ID, max_output_tokens=4000, effort="low"):
    """The degraded payload: the whole conversation as one user turn, no tools.

    This is the exact shape docs/ASTRA-RESPONSES.md verified, so it is the floor
    the ladder falls back to when a richer body is rejected."""
    lines = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        t = _text_of(m.get("content"))
        if t:
            lines.append("%s: %s" % (m.get("role") or "user", t))
    return {
        "model": model,
        "input": [{"role": "user", "content": [{"type": "input_text",
                                                "text": "\n\n".join(lines) or "hello"}]}],
        "reasoning": {"effort": effort},
        "max_output_tokens": max_output_tokens,
        "stream": False,
    }


# ── responses -> chat-completions ────────────────────────────────────────────

def to_chat_result(data, model=ASTRA_MODEL_ID):
    """Rebuild the shape brainstem's callers already understand."""
    if not isinstance(data, dict):
        raise ResponsesError(200, "non-dict response")
    texts = []
    tool_calls = []
    for item in (data.get("output") or []):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call":
            tool_calls.append({
                "id": item.get("call_id") or item.get("id") or "call_unknown",
                "type": "function",
                "function": {"name": item.get("name") or "unknown",
                             "arguments": item.get("arguments") or "{}"},
            })
            continue
        for c in (item.get("content") or []):
            # Reasoning items carry no output_text. Skip them, never fail on them.
            if isinstance(c, dict) and c.get("type") == "output_text" and c.get("text"):
                texts.append(c["text"])
    text = "".join(texts)
    if not text and not tool_calls:
        raise ResponsesError(200, "no output_text and no tool calls in: %s"
                             % json.dumps(data)[:300])
    msg = {"role": "assistant", "content": text or None}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    result = {
        "id": data.get("id") or "",
        "object": "chat.completion",
        "model": data.get("model") or model,
        "choices": [{"index": 0, "message": msg,
                     "finish_reason": "tool_calls" if tool_calls else "stop"}],
        "_rapp_route": "responses",
    }
    usage = data.get("copilot_usage")
    if isinstance(usage, dict):
        result["usage"] = {"total_nano_aiu": usage.get("total_nano_aiu")}
    return result


# ── transport ────────────────────────────────────────────────────────────────

def post_responses(body, token, endpoint, timeout=180):
    req = urllib.request.Request(
        endpoint.rstrip("/") + "/responses",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Editor-Version": EDITOR_VERSION,
            "Copilot-Integration-Id": INTEGRATION_ID,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:600]
        except Exception:
            detail = ""
        raise ResponsesError(e.code, detail)
    except Exception as e:
        raise ResponsesError(0, repr(e))


def complete(messages, tools=None, model=ASTRA_MODEL_ID, main=None, on_event=None):
    """One non-streaming turn. Returns (chat_completions_shaped_result, model_id).

    The ladder, in order:
      1. the rich body (instructions + typed items + flattened tools)
      2. on 401, refresh the token once and retry — a cached Copilot token can be
         rejected server side before its local expiry, exactly as call_copilot
         already handles for /chat/completions
      3. on any other 4xx, retry ONCE with the degraded body (one user turn, no
         tools), because a working answer beats a correct-looking failure
    """
    def _emit(kind, detail):
        if callable(on_event):
            try:
                on_event(kind, detail)
            except Exception:
                pass

    token, endpoint = resolve_token(main)
    body = to_responses_body(messages, tools=tools, model=model)
    try:
        data = post_responses(body, token, endpoint)
    except ResponsesError as e:
        if e.status == 401:
            _emit("token_refresh", e.detail)
            try:
                if callable(getattr(main, "_invalidate_copilot_token", None)):
                    main._invalidate_copilot_token()
            except Exception:
                pass
            token, endpoint = resolve_token(main, force=True)
            data = post_responses(body, token, endpoint)
        elif 400 <= (e.status or 0) < 500:
            _emit("degraded", e.detail)
            data = post_responses(flatten_body(messages, model=model), token, endpoint)
        else:
            raise
    return to_chat_result(data, model=model), (data.get("model") or model)
