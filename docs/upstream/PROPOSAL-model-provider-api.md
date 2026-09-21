# Upstream proposal: a model-provider hook

**Status:** proposed · **Target:** the brainstem (grail), via the release train, Canary first
**Author:** the `graft-astra-model` pack · **Retires:** this pack's graft, on the day this lands
**Host verified against:** 0.6.16 · **Proposed API version:** `MODEL_PROVIDER_API = 1`

## 1. What this asks for

Add a small, documented registry so a pack can serve a model the host cannot reach itself, instead
of patching `call_copilot` at runtime. Roughly forty lines in `brainstem.py`, three call sites, and
**no behaviour change whatsoever when no provider is registered**.

## 2. The problem, concretely

`_fetch_copilot_models()` keeps only models the Copilot API will serve over `/chat/completions`:

```python
endpoints = m.get("supported_endpoints")
if endpoints is not None and "/chat/completions" not in endpoints:
    skipped.append(mid); continue
```

That filter is **correct and must not be weakened** — it is what stops `gpt-5.5` and `*-codex`
from failing every turn with `unsupported_api_for_model`. But it also permanently hides models
that are real, entitled, and better than anything in the list. From the live catalog on a
0.6.16 install:

```json
{"id": "gpt-6-astra", "name": "GPT-6 Astra", "vendor": "OpenAI",
 "policy": {"state": "enabled"}, "model_picker_enabled": true, "preview": false,
 "supported_endpoints": ["/responses", "ws:/responses"],
 "capabilities": {"type": "chat", "limits": {"max_context_window_tokens": 1178000,
   "max_output_tokens": 128000, "max_prompt_tokens": 1050000},
   "supports": {"tool_calls": true, "parallel_tool_calls": true, "streaming": true,
                "structured_outputs": true, "vision": true,
                "reasoning_effort": ["low","medium","high","xhigh","max"]}}}
```

A 1.05M-token model the account is entitled to use, invisible to the brainstem, because it speaks
`/responses` instead of `/chat/completions`. Today the only way to reach it is to rebind
`call_copilot` from inside an agent — which works, and which nobody should have to do.

## 3. Non-goals

- Not relaxing the `supported_endpoints` filter. It stays exactly as it is.
- Not a general plugin system. One registry, one purpose: who can serve a model.
- Not changing the response shape. A provider returns what `call_copilot` already returns.

## 4. The API

```python
MODEL_PROVIDER_API = 1          # packs gate on this

def register_model_provider(name, claims, call, stream=None, source=None):
    """Register a transport that can serve models this host cannot reach itself.

    name   — unique, stable string; re-registering the same name replaces it
    claims — claims(raw_model_obj) -> bool, called on the RAW /models entry
             (policy, capabilities, supported_endpoints intact). Pure, no network.
    call   — call(messages, tools, model) -> (result, responded_model)
             Same contract as call_copilot: an OpenAI-shaped chat-completions dict
             with a non-empty "choices", plus the model id that answered.
    stream — optional stream(messages, tools, model) generator yielding
             ('delta', text) and finally ('done', {...}), exactly like
             call_copilot_stream. Raise StreamingUnsupported before the first
             delta to fall back to `call`. Omit if the transport cannot stream.
    source — free-text provenance for /health and support reports (e.g. a pack name).
    """
```

Semantics:

- **Claim order is registration order; first claim wins.** A duplicate `name` replaces rather than
  stacks, so a re-imported pack can never register twice.
- **Registration is pure.** No network, no token exchange, no disk. The host may call `claims()`
  on every catalog refresh.
- **Thread safety:** the registry is replaced by building a new list and assigning it in one
  statement — never mutated in place — so a concurrent reader never sees a torn list.
- A provider that raises is treated exactly like an API error from the normal path: the existing
  fallback cycling takes over.

## 5. The three integration points

**(a) Catalog — `_fetch_copilot_models()`.** A model rejected by the endpoint filter is offered to
the providers before it is dropped:

```python
if endpoints is not None and "/chat/completions" not in endpoints:
    provider = _provider_for(m)                 # claims() over the RAW object
    if provider is None:
        skipped.append(mid); continue
    new_models.append({"id": mid, "name": mname,
                       "available": _model_is_available(m),
                       "served_by": provider.name})
    continue
```

`served_by` is the whole honesty mechanism: `/health`, the model picker, and the support report can
all say which transport answers for a model, and a grafted or extended runtime stops being
indistinguishable from a pristine one.

**(b) Non-streaming — `call_copilot()`.** One branch at the top:

```python
provider = _provider_for_id(MODEL)
if provider is not None:
    return provider.call(messages, tools, MODEL)
```

**(c) Streaming — `call_copilot_stream()`.** The same, reusing the existing contract:

```python
provider = _provider_for_id(use_model)
if provider is not None:
    if provider.stream is None:
        raise StreamingUnsupported(use_model)   # caller already falls back to call_copilot
    yield from provider.stream(messages, tools, use_model)
    return
```

`StreamingUnsupported` already exists for precisely this case, so a non-streaming provider needs no
new machinery anywhere.

## 6. The detail that is easy to miss

`call_copilot`'s error path cycles through other available models on 400/429/5xx:

```python
fallback_ids = [m["id"] for m in AVAILABLE_MODELS
                if m["id"] != MODEL and m.get("available", True)]
```

Once provider-served models are in `AVAILABLE_MODELS`, that loop would POST them to
`/chat/completions` — exactly the failure the filter exists to prevent, now reintroduced through
the back door. Two lines fix it, and either is acceptable:

```python
# simplest: never cycle onto a provider-served model
fallback_ids = [m["id"] for m in AVAILABLE_MODELS
                if m["id"] != MODEL and m.get("available", True) and not m.get("served_by")]
```

or route each fallback attempt through `_provider_for_id()` the same way the main path does. The
first is recommended: a fallback should be the boring, universally-served option, and
`_SAFETY_NET_MODEL` (`gpt-4o`) already plays that role.

`/models/set` and the sticky-model path need no change — a provider-served model is an ordinary
entry in `AVAILABLE_MODELS` and validates like any other.

## 7. Risk

With no provider registered, `_provider_for()` returns `None` at every site and the code paths are
the ones that exist today. The blast radius of the change when unused is a dictionary lookup per
call. The filter that protects every user is untouched; provider-served models are strictly
**added** to a list they were previously absent from.

## 8. Evidence this is the right shape

The `graft-astra-model` pack implements exactly this contract by rebinding `call_copilot` at
runtime, and was exercised against a real brainstem 0.6.16 on a scratch port:

| observed | result |
|---|---|
| model picker | 25 → 26 entries, `gpt-6-astra` selectable, `/models/set` accepts it |
| a real `/chat` turn | answered, receipt `model: gpt-6-astra`, log `routing model=gpt-6-astra via POST /responses` |
| 50 consecutive turns | wrap depth histogram `{1: 50}`, counted inside the process |
| 48 concurrent sweeps | `{1: 48}` |
| the wire format | nested chat-style tool arrays hard-400 on `/responses`; the flattened `function_call` / `function_call_output` form round-trips |
| the grail tree | byte-identical before and after |

The hook is not speculative API design: it is the shape that a working implementation already has,
with the patching removed.

## 9. What upstream should require before merging

1. No provider registered → the full existing test suite passes unchanged.
2. A fake provider claiming a synthetic model → it appears in `/health` with `served_by`, answers a
   turn, and never reaches `/chat/completions`.
3. A provider that raises → the existing fallback cycling produces an answer.
4. Fallback cycling never selects a provider-served model (the §6 case, as an explicit test).
5. A provider without `stream` → `StreamingUnsupported`, caller falls back cleanly.
6. Two providers claiming one model → first registration wins, deterministically.
7. Re-registering the same `name` 50 times → exactly one provider (the loader re-imports agent
   modules on every `/chat` turn, so this is the normal case, not an edge case).

## 10. Migration and retirement

The day this ships in a release, `graft-astra-model` stops being a graft: the pack drops its
`__main__` patching and calls `register_model_provider()` instead, gated on `MODEL_PROVIDER_API`.
The graft file is deleted, not deprecated. That retirement is the condition this proposal was
written to satisfy — a graft is a hook proposal with running code attached, and this is the hook.

## 11. Alternatives considered

- **Relax the endpoint filter.** Rejected: it would reintroduce the failure the filter exists to
  prevent, for every user, to serve one model.
- **Patch `requests.post`.** Rejected: blast radius across every HTTP call in the process.
- **Wrap `chat()`.** Rejected: re-implements the tool loop, and drifts from upstream immediately.
- **A sidecar process.** Viable for a capability, but it cannot make a model appear in the host's
  own picker or serve the primary chat path, which is the point here.
- **Leave it as a permanent graft.** Rejected on principle: internals are not userspace, so the
  first refactor of `call_copilot` breaks every installed copy with no warning.

## 12. Open questions for the maintainer

1. Is `served_by` acceptable in `/health` and the support report? The extension story only stays
   honest if a modified runtime is visibly modified.
2. Should provider registration be restricted to first-party packs, or is any loaded agent allowed
   to register? The security review argued grafts are privileged host updates; a registry makes
   that a policy the host can enforce rather than a convention.
3. Does `MODEL_PROVIDER_API` belong in `/health` so a pack can gate without importing `__main__`?
