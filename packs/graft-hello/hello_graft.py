"""graft-hello — the smallest honest graft there is.

It adds one marker attribute to the running brainstem module and nothing else.
Its whole job is to be the thing the conformance suite exercises: applied on
boot, exactly once after fifty turns, invisible in safe mode, revertible without
a restart, and inert (never destructive) if the name it wants is already taken.

It wraps no function, so it is also the control in the 50-turn experiment: if the
wrap counter moves for `hello`, the counter itself is wrong.
"""

import os
import sys
from datetime import datetime, timezone

GRAFT_ID = "hello"
VERSION = "1.0.0"
HOSTS = ("brainstem",)
REQUIRES_BRAINSTEM = ">=0.1.0"

MARKER = "_rapp_graft_hello"


def _rt():
    """The shared runtime, if the engine published it. hello needs it only for the
    host mark, so its absence degrades rather than fails."""
    return sys.modules.get("rapp_graft_runtime")


def _now():
    try:
        return datetime.now(timezone.utc).isoformat()
    except Exception:
        return ""


def apply(main):
    """Return None on success, or a REASON STRING to go inert. Never raises."""
    if main is None:
        return "no host module to graft"

    existing = getattr(main, MARKER, None)
    if existing is not None:
        if isinstance(existing, dict) and existing.get("graft") == GRAFT_ID:
            return None  # already ours — idempotent, nothing to do
        # Someone else owns this name. Refuse rather than overwrite: upstream
        # drift must make a graft inert, never corrupt the host.
        return ("anchor occupied: %s already exists on the host and is not ours (%r)"
                % (MARKER, type(existing).__name__))

    setattr(main, MARKER, {
        "graft": GRAFT_ID,
        "version": VERSION,
        "applied_at": _now(),
        "pid": os.getpid(),
    })
    return None


def verify(main):
    """True only if the graft is actually in effect right now."""
    try:
        v = getattr(main, MARKER, None)
        return isinstance(v, dict) and v.get("graft") == GRAFT_ID
    except Exception:
        return False


def revert(main):
    """Reversible without a restart: the attribute simply goes away."""
    try:
        if verify(main):
            delattr(main, MARKER)
            rt = _rt()
            if rt is not None:
                rt.clear_host_mark(main, GRAFT_ID)
            return True
        return False
    except Exception:
        return False


def wrap_report(main):
    """hello wraps nothing — this is the control arm of the wrap count."""
    return {"marker_present": verify(main), "wraps": 0}
