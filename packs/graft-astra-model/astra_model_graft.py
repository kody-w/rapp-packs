"""graft-astra-model — make gpt-6-astra a first-class model of the RUNNING brainstem.

The problem: `gpt-6-astra` advertises `supported_endpoints: ["/responses",
"ws:/responses"]`. brainstem's `_fetch_copilot_models()` drops every model without
`/chat/completions`, so Astra is invisible in the picker and unreachable by /chat.
That filter is correct and this graft does not touch it — it routes around it.

Three anchored steps, each independent, none of which edits a byte of the grail:

  1. CATALOG   inject the entry into the live `AVAILABLE_MODELS` so the picker and
               `/models/set` accept it.
  2. REFETCH   wrap `_fetch_copilot_models` so a later catalog refresh — which
               REBINDS `AVAILABLE_MODELS` wholesale — does not drop the entry.
  3. ROUTE     wrap `call_copilot`, the one function every /chat round goes
               through, so a responses-only model is translated to
               POST {endpoint}/responses and the reply translated back.
  4. STREAM    (optional) make `call_copilot_stream` raise StreamingUnsupported
               immediately for a responses-only model, so /chat/stream falls back
               onto the wrapped `call_copilot` instead of burning a 400.

Why `call_copilot` is the seam: see docs/W2-REPORT.md. Short version — it is the
narrowest waist in the request path, its contract is two arguments and a
`(result, model_id)` tuple, and `chat()` resolves the name from module globals on
every call, so rebinding it takes effect with no restart and no re-entrancy games.

SPEC 4.3: nothing expensive happens at import. The token exchange lives inside the
call path, because this module's body is re-executed on every single chat turn.
"""

import importlib.util
import os
import sys

GRAFT_ID = "astra-model"
VERSION = "1.0.0"
HOSTS = ("brainstem",)
REQUIRES_BRAINSTEM = ">=0.1.0"

_HERE = os.path.dirname(os.path.abspath(__file__))


def _sibling(basename):
    path = os.path.join(_HERE, basename)
    if not os.path.exists(path):
        return None
    stable = "rapp_pack_" + basename.replace(".", "_")
    if stable in sys.modules:
        return sys.modules[stable]
    try:
        spec = importlib.util.spec_from_file_location(stable, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return sys.modules.setdefault(stable, mod)
    except Exception:
        return None


def _rt():
    rt = sys.modules.get("rapp_graft_runtime")
    return rt if rt is not None else _sibling("graft_runtime.py")


def _responses_only(main, model_id):
    """Is this model routed to /responses? Read from the live catalog, so this is
    a routing TABLE rather than a hardcoded model name — any future entry that
    declares no /chat/completions route gets the same treatment."""
    try:
        for m in (getattr(main, "AVAILABLE_MODELS", None) or []):
            if isinstance(m, dict) and m.get("id") == model_id:
                eps = m.get("supported_endpoints")
                if isinstance(eps, list):
                    return "/chat/completions" not in eps
                return False
    except Exception:
        pass
    return False


def _entry_index(models, model_id):
    for i, m in enumerate(models):
        if isinstance(m, dict) and m.get("id") == model_id:
            return i
    return -1


def apply(main):
    """Return None on success, or a REASON STRING to go inert. Never raises."""
    rt = _rt()
    ar = _sibling("astra_responses.py")
    if rt is None:
        return "graft_runtime is unavailable; refusing to patch blind"
    if ar is None:
        return "astra_responses.py is missing from the pack directory"

    # ── 1. CATALOG ───────────────────────────────────────────────────────────
    models, why = rt.anchor_list(main, "AVAILABLE_MODELS")
    if models is None:
        return why  # primary anchor gone -> fully inert, nothing else is worth doing
    if _entry_index(models, ar.ASTRA_MODEL_ID) < 0:
        models.append(dict(ar.ASTRA_ENTRY))

    # ── 2. REFETCH ───────────────────────────────────────────────────────────
    # _fetch_copilot_models does `AVAILABLE_MODELS = new_models` — a REBIND, not a
    # mutation — so the entry above would vanish on the next successful catalog
    # fetch (which happens again whenever a login clears the _models_fetched latch).
    fetch, fwhy = rt.anchor_callable(main, "_fetch_copilot_models", GRAFT_ID)
    if fetch is not None:
        def _fetch_wrapper():
            out = fetch()
            try:
                live = getattr(main, "AVAILABLE_MODELS", None)
                if isinstance(live, list) and _entry_index(live, ar.ASTRA_MODEL_ID) < 0:
                    live.append(dict(ar.ASTRA_ENTRY))
            except Exception:
                pass
            return out
        setattr(main, "_fetch_copilot_models", rt.mark(_fetch_wrapper, fetch, GRAFT_ID))
    elif "already wrapped" not in fwhy:
        rt.log("graft.anchor_skipped", graft=GRAFT_ID, anchor="_fetch_copilot_models",
               reason=fwhy)

    # ── 3. ROUTE ─────────────────────────────────────────────────────────────
    original, cwhy = rt.anchor_callable(main, "call_copilot", GRAFT_ID,
                                        params=("messages", "tools"))
    if original is None:
        if "already wrapped" in cwhy:
            return None  # a concurrent thread won the race; exactly one wrap stands
        # Without the routing seam the catalog entry is a trap: the picker would
        # offer a model that cannot answer. Undo step 1 and go inert.
        try:
            i = _entry_index(models, ar.ASTRA_MODEL_ID)
            if i >= 0 and models[i].get("grafted_by") == GRAFT_ID:
                models.pop(i)
        except Exception:
            pass
        rt.unwrap(main, "_fetch_copilot_models", GRAFT_ID)
        return cwhy

    def _call_copilot(messages, tools=None):
        model = getattr(main, "MODEL", "") or ""
        if not _responses_only(main, model):
            return original(messages, tools=tools)
        st = rt.state(main)
        try:
            st["counters"]["astra_responses_calls"] = \
                int(st["counters"].get("astra_responses_calls", 0)) + 1
        except Exception:
            pass
        print("[graft:astra-model] routing model=%s via POST /responses "
              "(tools=%d)" % (model, len(tools or [])))

        def _on_event(kind, detail):
            rt.log("astra.%s" % kind, graft=GRAFT_ID, model=model, detail=str(detail)[:400])
            print("[graft:astra-model] %s: %s" % (kind, str(detail)[:200]))

        try:
            return ar.complete(messages, tools=tools, model=model, main=main,
                               on_event=_on_event)
        except ar.ResponsesError as e:
            # Deliberately NOT falling back to original(): this model has no
            # /chat/completions route at all, so that path would 400 and then let
            # the grail's fallback loop answer as a DIFFERENT model under this
            # model's name. A clean error beats a silent misattribution.
            rt.log("astra.route_failed", graft=GRAFT_ID, model=model,
                   status=getattr(e, "status", 0), detail=str(getattr(e, "detail", e))[:400])
            raise RuntimeError("[graft:astra-model] %s could not be served over "
                               "/responses: %s" % (model, str(getattr(e, "detail", e))[:300]))

    setattr(main, "call_copilot", rt.mark(_call_copilot, original, GRAFT_ID))

    # ── 4. STREAM (optional) ─────────────────────────────────────────────────
    unsupported = getattr(main, "StreamingUnsupported", None)
    stream, swhy = rt.anchor_callable(main, "call_copilot_stream", GRAFT_ID,
                                      params=("messages", "tools"))
    if stream is not None and isinstance(unsupported, type):
        def _stream_wrapper(messages, tools=None, model=None):
            use = model or getattr(main, "MODEL", "") or ""
            if _responses_only(main, use):
                # Raised as the generator starts, before any delta, which is the
                # contract callers already fall back on.
                raise unsupported(400, "responses-only model; use the non-streaming path", use)
            for chunk in stream(messages, tools=tools, model=model):
                yield chunk
        setattr(main, "call_copilot_stream", rt.mark(_stream_wrapper, stream, GRAFT_ID))
    elif stream is None and "already wrapped" not in swhy:
        rt.log("graft.anchor_skipped", graft=GRAFT_ID, anchor="call_copilot_stream",
               reason=swhy)

    return None


def verify(main):
    """SPEC 4.1 — reads the same object marker the engine's host marker is paired
    with. True only when the graft is genuinely in effect right now."""
    try:
        rt = _rt()
        ar = _sibling("astra_responses.py")
        if rt is None or ar is None:
            return False
        models = getattr(main, "AVAILABLE_MODELS", None) or []
        if _entry_index(models, ar.ASTRA_MODEL_ID) < 0:
            return False
        return rt.is_wrapped_by(getattr(main, "call_copilot", None), GRAFT_ID)
    except Exception:
        return False


def revert(main):
    """Best-effort restore. Each unwrap only removes OUR frame, and only when it
    is the top one, so a graft stacked above us is never clobbered."""
    out = {}
    try:
        rt = _rt()
        ar = _sibling("astra_responses.py")
        if rt is None or ar is None:
            return {"error": "runtime unavailable"}
        for name in ("call_copilot", "_fetch_copilot_models", "call_copilot_stream"):
            out[name] = rt.unwrap(main, name, GRAFT_ID)
        models = getattr(main, "AVAILABLE_MODELS", None)
        if isinstance(models, list):
            i = _entry_index(models, ar.ASTRA_MODEL_ID)
            if i >= 0 and models[i].get("grafted_by") == GRAFT_ID:
                models.pop(i)
                out["catalog_entry_removed"] = True
        rt.clear_host_mark(main, GRAFT_ID)
    except Exception as e:
        out["error"] = repr(e)
    return out


def wrap_report(main):
    """The counter SPEC 4.2 asks for: how many frames THIS graft installed on each
    name. Anything but 1 after fifty turns is the defect the suite hunts."""
    try:
        rt = _rt()
        ar = _sibling("astra_responses.py")
        models = getattr(main, "AVAILABLE_MODELS", None) or []
        return {
            "call_copilot": rt.wrap_count(getattr(main, "call_copilot", None), GRAFT_ID),
            "_fetch_copilot_models": rt.wrap_count(
                getattr(main, "_fetch_copilot_models", None), GRAFT_ID),
            "call_copilot_stream": rt.wrap_count(
                getattr(main, "call_copilot_stream", None), GRAFT_ID),
            "catalog_entries": sum(
                1 for m in models
                if isinstance(m, dict) and m.get("id") == ar.ASTRA_MODEL_ID),
        }
    except Exception as e:
        return {"error": repr(e)}
