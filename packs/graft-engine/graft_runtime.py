"""graft_runtime — the shared helper every graft imports.

A graft is an ordinary file in the brainstem's agents directory that patches the
RUNNING brainstem module instead of editing it on disk. This module holds the
machinery every graft needs to do that safely:

  * safe-mode detection          (SPEC 4: inert means invisible)
  * the normative idempotence markers (SPEC 4.1)
        host:   __main__.__grafts_applied__   {GRAFT_ID: version}
        object: fn.__graft__                  GRAFT_ID
  * anchor verification          (SPEC 7: an anchor miss is a version miss)
  * host declaration             (SPEC 10: a graft patches ONE host and says so)
  * failure recording to <packs_dir>/grafts.log

Contract for everything in here: it NEVER raises. A helper that can throw is a
helper that can stop a brainstem from booting, and SPEC 4 forbids that outright.
Every public function catches broadly and returns a safe default.

Re-entrancy note (SPEC 4.1): brainstem's _load_agent_from_file() gives every load
a fresh module name and never touches the import cache, so an agent file's body is
re-executed on EVERY /chat and /health. Nothing in a graft's own globals survives.
That is why every marker here lives on the host module, and why the engine lock
below is reached through sys.modules — see ENGINE_MODULE_NAME.
"""

import functools
import inspect
import json
import os
import sys
import threading
import traceback
from datetime import datetime, timezone

GRAFT_RUNTIME_VERSION = "1.0.0"

#: SPEC 4.1 — the attribute stamped on every wrapper a graft installs.
MARK = "__graft__"
#: SPEC 4.1 — the dict on the host module recording which grafts are applied.
HOST_MARKER = "__grafts_applied__"
#: The rich engine state (discovery, inert reasons, failures, counters).
STATE_ATTR = "_RAPP_GRAFT_STATE"

#: Every copy of this module registers itself here under ONE stable name, and the
#: engine uses the value sys.modules.setdefault() RETURNS. That makes the lock
#: below a true process singleton even though the file is re-executed per turn —
#: which matters because the brainstem serves with threaded=True and load_agents()
#: therefore runs concurrently on several worker threads.
ENGINE_MODULE_NAME = "rapp_graft_runtime"

#: Serialises graft application across those worker threads. Without it two
#: threads can both read "not applied" and both wrap the same function.
ENGINE_LOCK = threading.RLock()

_FALLBACK_STATE = {}
_MAX_FIELD = 4000


def _now():
    try:
        return datetime.now(timezone.utc).isoformat()
    except Exception:
        return ""


# ── paths and safe mode ──────────────────────────────────────────────────────

def packs_dir():
    """The pack channel's state directory. Outside the grail tree by design, so a
    grail upgrade or a repair reclone cannot lose it."""
    try:
        p = (os.environ.get("BRAINSTEM_PACKS_DIR") or "").strip()
        if not p:
            p = os.path.join(os.path.expanduser("~"), ".brainstem", "packs")
        try:
            os.makedirs(p, exist_ok=True)
        except Exception:
            pass
        return p
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".brainstem", "packs")


def safe_mode():
    """SPEC 4: with safe mode on, apply() must not run and the process must be
    indistinguishable from the pure grail."""
    try:
        if (os.environ.get("BRAINSTEM_SAFE") or "").strip().lower() in ("1", "true", "yes", "on"):
            return True
        return os.path.exists(os.path.join(packs_dir(), "SAFE-MODE"))
    except Exception:
        return False


def grafts_log_path():
    try:
        return os.path.join(packs_dir(), "grafts.log")
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".brainstem", "packs", "grafts.log")


def log(event, **fields):
    """Append one JSON line to grafts.log. Never raises, never blocks on failure."""
    try:
        rec = {"ts": _now(), "event": str(event)}
        for k, v in fields.items():
            if not isinstance(v, (str, int, float, bool, type(None))):
                v = repr(v)
            if isinstance(v, str) and len(v) > _MAX_FIELD:
                v = v[:_MAX_FIELD] + "...<truncated>"
            rec[str(k)] = v
        with open(grafts_log_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def record_failure(graft_id, exc):
    """SPEC 4: on exception a graft records the traceback, marks itself failed, and
    lets the brainstem run ungrafted."""
    try:
        tb = traceback.format_exc()
    except Exception:
        tb = ""
    log("graft.failed", graft=graft_id, error=repr(exc)[:300], traceback=tb)


# ── state on the host ────────────────────────────────────────────────────────

def state(main):
    """Get-or-create the rich engine state on the host module.

    This is engine bookkeeping. The NORMATIVE idempotence marker is
    main.__grafts_applied__ (SPEC 4.1); this dict carries the extra detail the
    `Grafts` agent reports (inert reasons, failures, counters, log-once guards).
    """
    default = {
        "schema": 1,
        "engine": GRAFT_RUNTIME_VERSION,
        "applied": {},
        "failed": {},
        "inert": {},
        "counters": {},
        "logged": {},
        "boot_ts": _now(),
    }
    try:
        if main is None:
            if not _FALLBACK_STATE:
                _FALLBACK_STATE.update(default)
            return _FALLBACK_STATE
        st = getattr(main, STATE_ATTR, None)
        if not isinstance(st, dict):
            st = default
            setattr(main, STATE_ATTR, st)
        for k, v in default.items():
            st.setdefault(k, v)
        return st
    except Exception:
        if not _FALLBACK_STATE:
            _FALLBACK_STATE.update(default)
        return _FALLBACK_STATE


def host_marks(main):
    """SPEC 4.1 — get-or-create main.__grafts_applied__, a dict GRAFT_ID -> version."""
    try:
        if main is None:
            return {}
        marks = getattr(main, HOST_MARKER, None)
        if not isinstance(marks, dict):
            marks = {}
            setattr(main, HOST_MARKER, marks)
        return marks
    except Exception:
        return {}


def host_marked(main, graft_id):
    try:
        return graft_id in host_marks(main)
    except Exception:
        return False


def set_host_mark(main, graft_id, version="1.0.0"):
    try:
        host_marks(main)[graft_id] = str(version)
    except Exception:
        pass


def clear_host_mark(main, graft_id):
    try:
        host_marks(main).pop(graft_id, None)
    except Exception:
        pass


# ── host identity and version (SPEC 10 / SPEC 7) ─────────────────────────────

def host_id(main):
    """Which organism are we inside? SPEC 10: a graft patches one specific host's
    live module and must refuse anywhere else, so this is checked BEFORE apply()."""
    try:
        if main is None:
            return "unknown"
        f = getattr(main, "__file__", "") or ""
        if os.path.basename(f) != "brainstem.py":
            return "unknown"
        for attr in ("AVAILABLE_MODELS", "load_agents", "app"):
            if not hasattr(main, attr):
                return "unknown"
        return "brainstem"
    except Exception:
        return "unknown"


def host_version(main):
    try:
        return str(getattr(main, "VERSION", "") or "")
    except Exception:
        return ""


def _vtuple(v):
    parts = []
    for seg in str(v).strip().split("."):
        num = ""
        for ch in seg:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    return tuple(parts) if parts else None


def _cmp(a, b):
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    return (a > b) - (a < b)


_OPS = (">=", "<=", "==", "!=", ">", "<")


def version_ok(version, spec):
    """SPEC 7 — is this host inside the pack's declared range?

    Fails OPEN on anything unparseable: a graft going inert because of a parser
    bug is a worse outcome than one that runs. Returns (ok, reason).
    """
    try:
        spec = (spec or "").strip()
        if not spec or spec == "*":
            return True, ""
        hv = _vtuple(version)
        if hv is None:
            return True, ""
        for raw in spec.split(","):
            raw = raw.strip()
            if not raw:
                continue
            op = None
            for cand in _OPS:
                if raw.startswith(cand):
                    op = cand
                    break
            if op is None:
                op, rhs = "==", raw
            else:
                rhs = raw[len(op):].strip()
            wv = _vtuple(rhs)
            if wv is None:
                continue
            c = _cmp(hv, wv)
            ok = {
                ">=": c >= 0, ">": c > 0, "<=": c <= 0,
                "<": c < 0, "==": c == 0, "!=": c != 0,
            }[op]
            if not ok:
                return False, "host version %s does not satisfy %s" % (version or "?", spec)
        return True, ""
    except Exception:
        return True, ""


# ── wrapping: marks, counts, anchors ─────────────────────────────────────────

def is_wrapped_by(fn, graft_id):
    try:
        return getattr(fn, MARK, None) == graft_id
    except Exception:
        return False


def wrap_count(fn, graft_id):
    """Walk the __wrapped__ chain and COUNT how many frames this graft installed.

    SPEC 4.2: a double-wrap compounds per message, so the conformance proof counts
    wraps rather than inferring them from behaviour. This is that counter.
    """
    n = 0
    hops = 0
    try:
        while fn is not None and hops < 50:
            if getattr(fn, MARK, None) == graft_id:
                n += 1
            fn = getattr(fn, "__wrapped__", None)
            hops += 1
    except Exception:
        pass
    return n


def mark(wrapper, original, graft_id):
    """Stamp a wrapper with the normative object marker.

    functools.update_wrapper copies __dict__ FROM the original, so the marker and
    __wrapped__ must be set AFTER it — otherwise the original's (empty) __dict__
    silently erases them and every turn wraps again.
    """
    try:
        functools.update_wrapper(wrapper, original)
    except Exception:
        pass
    try:
        wrapper.__wrapped__ = original
        setattr(wrapper, MARK, graft_id)
    except Exception:
        pass
    return wrapper


def anchor_callable(main, name, graft_id, params=()):
    """SPEC 7 — does the thing we mean to wrap still look like what we expect?

    Returns (fn, "") when it is safe to wrap, else (None, reason). A reason makes
    the graft inert; upstream drift must never produce a half-applied patch.
    """
    try:
        if main is None:
            return None, "no host module"
        if not hasattr(main, name):
            return None, "anchor missing: %s is not an attribute of the host" % name
        fn = getattr(main, name)
        if not callable(fn):
            return None, "anchor changed shape: %s is not callable" % name
        if is_wrapped_by(fn, graft_id):
            return None, "already wrapped by %s" % graft_id
        if params:
            try:
                sig = inspect.signature(fn)
            except Exception:
                sig = None  # fail open: an unintrospectable callable is not drift
            if sig is not None:
                for p in params:
                    if p not in sig.parameters:
                        return None, "anchor changed shape: %s lost parameter %r" % (name, p)
        return fn, ""
    except Exception as e:
        return None, "anchor check failed for %s: %r" % (name, e)


def anchor_list(main, name):
    try:
        if main is None:
            return None, "no host module"
        if not hasattr(main, name):
            return None, "anchor missing: %s is not an attribute of the host" % name
        v = getattr(main, name)
        if not isinstance(v, list):
            return None, "anchor changed shape: %s is %s, not a list" % (name, type(v).__name__)
        return v, ""
    except Exception as e:
        return None, "anchor check failed for %s: %r" % (name, e)


def unwrap(main, name, graft_id):
    """Best-effort revert of ONE wrap. Only removes the top frame, and only when
    that frame is ours — never clobbers a wrap another graft installed on top."""
    try:
        fn = getattr(main, name, None)
        if fn is None or not is_wrapped_by(fn, graft_id):
            return False
        inner = getattr(fn, "__wrapped__", None)
        if inner is None:
            return False
        setattr(main, name, inner)
        return True
    except Exception:
        return False
