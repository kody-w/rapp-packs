"""graft_agent — the graft engine: discovery, application, isolation, reporting.

This file is an ordinary brainstem agent (it matches the *_agent.py loader glob).
It is also the loader for every graft on the host: on each import it discovers
`*_graft.py` beside itself, applies the ones that are eligible, and exposes the
result as the `Grafts` agent (status / apply / revert / log).

Three properties the rest of the channel depends on:

  1. It can never stop the brainstem from booting. Every stage is inside
     try/except; the only escape hatch left is a graft that WEDGES the process
     (hangs, segfaults, os._exit) — see the `graft.applying` line written before
     every apply(), which is what makes such a culprit nameable afterwards.
  2. It is re-entrant. brainstem's loader re-executes this file on EVERY /chat and
     /health, so all durable state lives on the host module (SPEC 4.1) and every
     decision is taken under a process-wide lock, because the server is threaded.
  3. In safe mode it defines no agent class at all, so /health is byte-identical
     to the pure grail.
"""

import glob
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
GRAFT_GLOB = "*_graft.py"
ENGINE_VERSION = "1.0.0"


def _now():
    try:
        return datetime.now(timezone.utc).isoformat()
    except Exception:
        return ""


def _load_sibling(basename, stable_name=None):
    """Import a module that sits next to this file, BY PATH.

    brainstem's loader puts the brainstem dir on sys.path but not AGENTS_PATH, so
    a plain `import graft_runtime` fails on any install where the two differ.
    When `stable_name` is given the module is published under it and the value
    sys.modules RETURNS is used — so every thread and every re-import converges on
    ONE object (and therefore one lock).
    """
    path = os.path.join(_HERE, basename)
    if not os.path.exists(path):
        return None
    try:
        if stable_name and stable_name in sys.modules:
            return sys.modules[stable_name]
        name = stable_name or ("rapp_sib_" + basename.replace(".", "_"))
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if stable_name:
            return sys.modules.setdefault(stable_name, mod)
        return mod
    except Exception as e:
        print("[graft-engine] could not load %s: %r" % (basename, e))
        return None


rt = _load_sibling("graft_runtime.py", stable_name="rapp_graft_runtime")

# BasicAgent: the brainstem shims `agents.basic_agent` when AGENTS_PATH is the
# default, but a relocated AGENTS_PATH gets neither import. Try both, then fall
# back to a local definition — named exactly BasicAgent, which the loader skips.
try:
    from basic_agent import BasicAgent  # type: ignore
except Exception:
    _ba = _load_sibling("basic_agent.py")
    if _ba is not None and hasattr(_ba, "BasicAgent"):
        BasicAgent = _ba.BasicAgent
    else:
        class BasicAgent(object):
            def __init__(self, name=None, metadata=None):
                if name is not None:
                    self.name = name
                if metadata is not None:
                    self.metadata = metadata

            def system_context(self):
                return None

            def to_tool(self):
                return {"type": "function", "function": {
                    "name": self.name,
                    "description": self.metadata.get("description", ""),
                    "parameters": self.metadata.get("parameters", {"type": "object", "properties": {}}),
                }}

main = sys.modules.get("__main__")
_counter = [0]


def _log_once(st, key, event, **fields):
    """grafts.log must not grow by a line per chat turn — _apply_all runs on every
    message. One line per (graft, event) per process."""
    try:
        if st["logged"].get(key):
            return
        st["logged"][key] = True
        rt.log(event, **fields)
    except Exception:
        pass


def _import_graft(path):
    _counter[0] += 1
    name = "rapp_graft_%s_%d" % (os.path.basename(path).replace(".", "_"), _counter[0])
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def discover():
    try:
        return sorted(glob.glob(os.path.join(_HERE, GRAFT_GLOB)))
    except Exception:
        return []


def _apply_one(main, st, path, only=None):
    """Apply one graft. Returns a short verdict string. Never raises."""
    gid = os.path.basename(path)[:-3]
    try:
        mod = _import_graft(path)
        gid = getattr(mod, "GRAFT_ID", None) or gid
        version = str(getattr(mod, "VERSION", "1.0.0"))

        # `only` may name the GRAFT_ID or the filename stem; both resolve here,
        # after the import, so a graft is never filtered on a guessed name.
        if only and only not in (gid, os.path.basename(path)[:-3]):
            return "skipped (not selected)"

        # ── SPEC 4.1: the two markers. The host marker says "some version of this
        # graft is applied"; verify() reads the object marker and says "this exact
        # object is still wrapped". Disagreement means the grail moved under us.
        if rt.host_marked(main, gid):
            try:
                in_effect = bool(mod.verify(main))
            except Exception:
                in_effect = False
            if in_effect:
                return "skipped (already applied)"
            _log_once(st, gid + ":remark", "graft.remark", graft=gid,
                      detail="host marker survived but verify() says not in effect - "
                             "re-applying against the fresh objects")
            rt.clear_host_mark(main, gid)
            st["applied"].pop(gid, None)

        if gid in st["failed"]:
            return "skipped (failed earlier this process)"

        # ── SPEC 10: the host gate runs BEFORE apply() touches anything.
        hosts = getattr(mod, "HOSTS", ("brainstem",))
        if isinstance(hosts, str):
            hosts = (hosts,)
        here = rt.host_id(main)
        if here not in tuple(hosts):
            reason = "host mismatch: running on %r, graft declares %s" % (here, list(hosts))
            st["inert"][gid] = reason
            _log_once(st, gid + ":inert", "graft.inert", graft=gid, reason=reason)
            return "inert (%s)" % reason

        # ── SPEC 7: an out-of-range host is the same event as a missing anchor.
        req = getattr(mod, "REQUIRES_BRAINSTEM", "")
        ok, why = rt.version_ok(rt.host_version(main), req)
        if not ok:
            st["inert"][gid] = why
            _log_once(st, gid + ":inert", "graft.inert", graft=gid, reason=why)
            return "inert (%s)" % why

        if not callable(getattr(mod, "apply", None)):
            reason = "graft exposes no apply()"
            st["inert"][gid] = reason
            _log_once(st, gid + ":inert", "graft.inert", graft=gid, reason=reason)
            return "inert (%s)" % reason

        # SPEC 6: written BEFORE apply(), so a graft that wedges the process
        # (hang / segfault / os._exit) leaves an unterminated line naming itself.
        # try/except cannot catch those shapes; this line is what can.
        _log_once(st, gid + ":applying", "graft.applying", graft=gid, file=path)

        result = mod.apply(main)

        if isinstance(result, str) and result.strip():
            # An anchored refusal: the graft looked, did not like what it saw, and
            # declined. Inert with a reason, never a half-applied patch.
            st["inert"][gid] = result.strip()
            st["applied"].pop(gid, None)
            rt.clear_host_mark(main, gid)
            _log_once(st, gid + ":inert", "graft.inert", graft=gid, reason=result.strip())
            return "inert (%s)" % result.strip()

        st["applied"][gid] = {"file": path, "version": version, "ts": _now()}
        st["inert"].pop(gid, None)
        st["failed"].pop(gid, None)
        rt.set_host_mark(main, gid, version)
        _log_once(st, gid + ":applied", "graft.applied", graft=gid, file=path, version=version)
        return "applied"

    except Exception as e:
        st["failed"][gid] = {"error": repr(e)[:300], "ts": _now()}
        st["applied"].pop(gid, None)
        rt.clear_host_mark(main, gid)
        rt.record_failure(gid, e)
        return "FAILED (%r)" % (e,)


def _apply_all(main, only=None, retry_failed=False):
    """Apply every discovered graft. Isolated per graft; never raises.

    Held under the runtime's process-wide lock: the brainstem serves with
    threaded=True, so load_agents() — and therefore this function — runs
    concurrently on several worker threads. Without the lock two threads can both
    read "not applied" and both wrap the same function.
    """
    verdicts = {}
    if rt is None:
        return verdicts
    try:
        with rt.ENGINE_LOCK:
            st = rt.state(main)
            for path in discover():
                gid_guess = os.path.basename(path)[:-3]
                if retry_failed:
                    # An operator asking for `apply` is asking us to try again, so
                    # clear the failed latch and the log-once guards for the
                    # graft(s) in scope — otherwise a fixed graft could not come
                    # back without a restart.
                    for key in (gid_guess, only):
                        if not key:
                            continue
                        st["failed"].pop(key, None)
                        st["inert"].pop(key, None)
                        for suffix in (":applying", ":inert", ":applied", ":remark"):
                            st["logged"].pop(key + suffix, None)
                try:
                    verdicts[gid_guess] = _apply_one(main, st, path, only=only)
                except Exception as e:  # belt and suspenders; _apply_one catches
                    verdicts[gid_guess] = "ENGINE ERROR %r" % (e,)
    except Exception as e:
        print("[graft-engine] apply sweep failed: %r" % (e,))
    return verdicts


def _report(main):
    """Everything the `Grafts` agent knows, as a plain dict."""
    st = rt.state(main)
    files = discover()
    grafts = {}
    for path in files:
        gid = os.path.basename(path)[:-3]
        entry = {"file": path}
        try:
            mod = _import_graft(path)
            gid = getattr(mod, "GRAFT_ID", None) or gid
            entry["declares_hosts"] = list(getattr(mod, "HOSTS", ("brainstem",)))
            entry["requires_brainstem"] = getattr(mod, "REQUIRES_BRAINSTEM", "")
            try:
                entry["verify"] = bool(mod.verify(main))
            except Exception as e:
                entry["verify"] = "error: %r" % (e,)
            if callable(getattr(mod, "wrap_report", None)):
                try:
                    entry["wraps"] = mod.wrap_report(main)
                except Exception as e:
                    entry["wraps"] = "error: %r" % (e,)
        except Exception as e:
            entry["import_error"] = repr(e)[:300]
        if gid in st["applied"]:
            entry["state"] = "applied"
            entry["applied"] = st["applied"][gid]
        elif gid in st["inert"]:
            entry["state"] = "inert"
            entry["reason"] = st["inert"][gid]
        elif gid in st["failed"]:
            entry["state"] = "failed"
            entry["error"] = st["failed"][gid]
        else:
            entry["state"] = "unknown"
        grafts[gid] = entry

    # SPEC 6: a graft whose `applying` line was written but which never reached a
    # terminator is the shape try/except cannot catch. Name it.
    unterminated = []
    for key in list(st["logged"].keys()):
        if key.endswith(":applying"):
            g = key[: -len(":applying")]
            if g not in st["applied"] and g not in st["inert"] and g not in st["failed"]:
                unterminated.append(g)

    return {
        "engine": ENGINE_VERSION,
        "runtime": getattr(rt, "GRAFT_RUNTIME_VERSION", "?"),
        "safe_mode": rt.safe_mode(),
        "packs_dir": rt.packs_dir(),
        "grafts_log": rt.grafts_log_path(),
        "host": rt.host_id(main),
        "host_version": rt.host_version(main),
        "host_marker": dict(rt.host_marks(main)),
        "counters": dict(st.get("counters", {})),
        "unterminated_apply": unterminated,
        "grafts": grafts,
    }


def _revert(main, only=None):
    out = {}
    st = rt.state(main)
    with rt.ENGINE_LOCK:
        for path in discover():
            gid = os.path.basename(path)[:-3]
            try:
                mod = _import_graft(path)
                gid = getattr(mod, "GRAFT_ID", None) or gid
                if only and only != gid:
                    continue
                if gid not in st["applied"] and not rt.host_marked(main, gid):
                    out[gid] = "not applied"
                    continue
                out[gid] = mod.revert(main) if callable(getattr(mod, "revert", None)) else "no revert()"
                st["applied"].pop(gid, None)
                rt.clear_host_mark(main, gid)
                st["logged"].pop(gid + ":applied", None)
                st["logged"].pop(gid + ":applying", None)
                rt.log("graft.reverted", graft=gid, detail=str(out[gid])[:300])
            except Exception as e:
                out[gid] = "revert failed: %r" % (e,)
    return out


# ── module tail ──────────────────────────────────────────────────────────────
# In safe mode NOTHING happens and NO class is defined, so the brainstem's
# /health is byte-identical to the pure grail even with every pack file on disk.

if rt is not None and not rt.safe_mode():
    try:
        _apply_all(main)
    except Exception as e:  # unreachable by design; a boot must never die here
        print("[graft-engine] sweep raised: %r" % (e,))

    class GraftAgent(BasicAgent):
        """Inspect and control the grafts applied to the running brainstem."""

        def __init__(self):
            self.name = "Grafts"
            self.metadata = {
                "name": "Grafts",
                "description": (
                    "Inspect and control this brainstem's grafts - modules that patch the "
                    "RUNNING brainstem without editing a byte of it on disk. "
                    "action=status reports every graft and whether it is applied, inert "
                    "(with the reason) or failed; action=apply re-runs the sweep and retries "
                    "failures; action=revert undoes a graft; action=log tails grafts.log."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["status", "apply", "revert", "log"],
                            "description": "What to do. Defaults to status.",
                        },
                        "graft": {
                            "type": "string",
                            "description": "Limit the action to one graft id.",
                        },
                        "lines": {
                            "type": "integer",
                            "description": "For action=log, how many trailing lines to show.",
                        },
                    },
                    "required": [],
                },
            }

        def perform(self, action="status", graft=None, lines=20, **kwargs):
            try:
                action = (action or "status").strip().lower()
                if action == "apply":
                    verdicts = _apply_all(main, only=graft, retry_failed=True)
                    return json.dumps({"applied_sweep": verdicts, "status": _report(main)},
                                      indent=2, default=str)
                if action == "revert":
                    return json.dumps({"reverted": _revert(main, only=graft),
                                       "status": _report(main)}, indent=2, default=str)
                if action == "log":
                    try:
                        n = max(1, min(int(lines or 20), 500))
                    except Exception:
                        n = 20
                    path = rt.grafts_log_path()
                    if not os.path.exists(path):
                        return "No grafts.log yet at %s" % path
                    with open(path, encoding="utf-8", errors="replace") as f:
                        tail = f.readlines()[-n:]
                    return "%s (last %d lines)\n%s" % (path, len(tail), "".join(tail))
                return json.dumps(_report(main), indent=2, default=str)
            except Exception as e:
                return "Grafts agent error: %r" % (e,)
