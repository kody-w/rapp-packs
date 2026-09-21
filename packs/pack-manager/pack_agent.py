"""Packs — the RAPP brainstem's expansion-channel manager.

One file, standard library only. Drop it into a brainstem's agents/ directory and the
brainstem can install, update, pin, revert and remove packs from a signed channel without
a single byte of the grail tree changing and without a restart.

Contract: docs/SPEC.md in kody-w/rapp-packs.

  install   index -> signature -> manifest digest -> per-file digest -> ast.parse ->
            atomic write -> generation snapshot -> installed.json.  All-or-nothing.
  safety    fail closed on every digest and signature mismatch; allowlist the channel
            host; refuse cross-host redirects; never print a credential.
  survival  generations on disk (SPEC 6) make revert a file move; three boots that never
            serve a request auto-revert to the last known good set, offline, no human.

Never restarts the brainstem: load_agents() runs on every /chat, so a pack is live on the
next message.
"""

import ast
import binascii
import glob
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# BasicAgent lives next to us in AGENTS_PATH, which is NOT always importable: the
# brainstem only puts <brainstem_dir> and <brainstem_dir>/agents on sys.path, so a
# brainstem started with AGENTS_PATH pointing somewhere else (scratch ports, a second
# install, a test harness) cannot resolve either name. Fall through to loading the file
# that sits beside us, and finally to a local shim, so this agent never fails to load.
try:  # pragma: no cover - import shape depends on the host
    from agents.basic_agent import BasicAgent  # type: ignore
except Exception:  # noqa: BLE001
    try:
        from basic_agent import BasicAgent  # type: ignore
    except Exception:  # noqa: BLE001
        try:
            import importlib.util as _ilu

            _ba_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "basic_agent.py")
            _ba_spec = _ilu.spec_from_file_location("_packs_basic_agent", _ba_path)
            _ba_mod = _ilu.module_from_spec(_ba_spec)
            _ba_spec.loader.exec_module(_ba_mod)
            BasicAgent = _ba_mod.BasicAgent  # type: ignore
        except Exception:  # noqa: BLE001
            class BasicAgent(object):  # type: ignore
                """Minimal stand-in used only when basic_agent.py cannot be found."""

                def __init__(self, name=None, metadata=None):
                    if name is not None:
                        self.name = name
                    if metadata is not None:
                        self.metadata = metadata

                def perform(self, **kwargs):
                    return "Not implemented."

                def system_context(self):
                    return None

                def to_tool(self):
                    return {
                        "type": "function",
                        "function": {
                            "name": self.name,
                            "description": self.metadata.get("description", ""),
                            "parameters": self.metadata.get("parameters", {"type": "object"}),
                        },
                    }


__manifest__ = {
    "schema": "rapp-agent/1.0",
    "name": "@rapp/packs",
    "version": "1.0.0",
    "display_name": "Packs",
    "description": (
        "Install, update, pin, revert and remove brainstem packs from the signed "
        "rapp-packs channel. Verifies every byte before writing it."
    ),
    "author": "rapp",
    "tags": ["packs", "channel", "update", "supply-chain"],
    "category": "platform",
    "quality_tier": "core",
    "requires_env": [],
    "dependencies": ["@rapp/basic_agent"],
    "example_call": {"args": {"action": "list"}},
}

SCHEMA_PACK = "rapp-pack/1"
SCHEMA_CHANNEL = "rapp-channel/1"
SCHEMA_INSTALLED = "rapp-installed/1"
SCHEMA_SIG = "rapp-sig/1"
SCHEMA_BOOT = "rapp-boot/1"
SCHEMA_CONFIG = "rapp-packs-config/1"

DEFAULT_BASE = "https://kody-w.github.io/rapp-packs/"
THIS_HOST = "brainstem"
MANAGER_PACK = "pack-manager"

# Most stable first. A release stabilises nightly -> alpha -> canary -> beta, so a pinned
# ring falls back only toward MORE stable rings, never less.
RING_ORDER = ("beta", "canary", "alpha", "nightly")
DEFAULT_RING = "beta"

KEEP_GENERATIONS = 5
DIRTY_BOOT_LIMIT = 3

_MAX_INDEX_BYTES = 4 * 1024 * 1024
_MAX_FILE_BYTES = 4 * 1024 * 1024
_TIMEOUT = 20

_NAME_RE = re.compile(r"^[a-z0-9-]{2,40}$")
_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


# ─────────────────────────────────────────────────────────────────────────────
# Ed25519 (RFC 8032), pure stdlib. Verified against RFC 8032 test vector 1 and
# byte-compared against `openssl pkeyutl -sign -rawin`. Signing lives here too so
# tools/build_channel.py and this agent share one implementation.
# ─────────────────────────────────────────────────────────────────────────────

_ED_P = 2 ** 255 - 19
_ED_L = 2 ** 252 + 27742317777372353535851937790883648493


def _ed_sha512(b):
    return hashlib.sha512(b).digest()


def _ed_inv(x):
    return pow(x, _ED_P - 2, _ED_P)


_ED_D = -121665 * _ed_inv(121666) % _ED_P
_ED_SQRT_M1 = pow(2, (_ED_P - 1) // 4, _ED_P)


def _ed_recover_x(y, sign):
    if y >= _ED_P:
        return None
    x2 = (y * y - 1) * _ed_inv(_ED_D * y * y + 1) % _ED_P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_ED_P + 3) // 8, _ED_P)
    if (x * x - x2) % _ED_P != 0:
        x = x * _ED_SQRT_M1 % _ED_P
    if (x * x - x2) % _ED_P != 0:
        return None
    if (x & 1) != sign:
        x = _ED_P - x
    return x


_ED_GY = 4 * _ed_inv(5) % _ED_P
_ED_GX = _ed_recover_x(_ED_GY, 0)
_ED_G = (_ED_GX, _ED_GY, 1, _ED_GX * _ED_GY % _ED_P)


def _ed_add(P, Q):
    A = (P[1] - P[0]) * (Q[1] - Q[0]) % _ED_P
    B = (P[1] + P[0]) * (Q[1] + Q[0]) % _ED_P
    C = 2 * P[3] * Q[3] * _ED_D % _ED_P
    D = 2 * P[2] * Q[2] % _ED_P
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _ED_P, G * H % _ED_P, F * G % _ED_P, E * H % _ED_P)


def _ed_mul(s, P):
    Q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            Q = _ed_add(Q, P)
        P = _ed_add(P, P)
        s >>= 1
    return Q


def _ed_equal(P, Q):
    if (P[0] * Q[2] - Q[0] * P[2]) % _ED_P != 0:
        return False
    return (P[1] * Q[2] - Q[1] * P[2]) % _ED_P == 0


def _ed_compress(P):
    zinv = _ed_inv(P[2])
    x = P[0] * zinv % _ED_P
    y = P[1] * zinv % _ED_P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _ed_decompress(s):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _ed_recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _ED_P)


def _ed_expand(seed):
    if len(seed) != 32:
        raise ValueError("ed25519 seed must be 32 bytes")
    h = _ed_sha512(seed)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def ed25519_public_key(seed):
    a, _ = _ed_expand(seed)
    return _ed_compress(_ed_mul(a, _ED_G))


def ed25519_sign(seed, message):
    a, prefix = _ed_expand(seed)
    A = _ed_compress(_ed_mul(a, _ED_G))
    r = int.from_bytes(_ed_sha512(prefix + message), "little") % _ED_L
    Rs = _ed_compress(_ed_mul(r, _ED_G))
    h = int.from_bytes(_ed_sha512(Rs + A + message), "little") % _ED_L
    return Rs + int.to_bytes((r + h * a) % _ED_L, 32, "little")


def ed25519_verify(public, message, signature):
    if len(public) != 32 or len(signature) != 64:
        return False
    A = _ed_decompress(public)
    if A is None:
        return False
    Rs = signature[:32]
    R = _ed_decompress(Rs)
    if R is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _ED_L:
        return False
    h = int.from_bytes(_ed_sha512(Rs + bytes(public) + message), "little") % _ED_L
    return _ed_equal(_ed_mul(s, _ED_G), _ed_add(R, _ed_mul(h, A)))


# ─────────────────────────────────────────────────────────────────────────────
# Errors
# ─────────────────────────────────────────────────────────────────────────────


class _Refused(Exception):
    """A fail-closed refusal. The message is safe to show the user."""


class _OffsiteRedirect(Exception):
    def __init__(self, target):
        Exception.__init__(self, target)
        self.target = target


# ─────────────────────────────────────────────────────────────────────────────
# Paths, config, small IO
# ─────────────────────────────────────────────────────────────────────────────


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _jdump(obj):
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _short(digest):
    return (digest or "")[:12]


def agents_path():
    """The agents directory of the brainstem that is ACTUALLY running.

    Read it off the live module first (the brainstem already resolved relative values
    against its own directory); only then the environment, only then the default. A pack
    must never land in a guessed tree.
    """
    main = sys.modules.get("__main__")
    live = getattr(main, "AGENTS_PATH", None)
    if isinstance(live, str) and live.strip():
        return os.path.abspath(os.path.expanduser(live.strip()))
    env = (os.environ.get("AGENTS_PATH") or "").strip()
    if env:
        if os.path.isabs(os.path.expanduser(env)):
            return os.path.abspath(os.path.expanduser(env))
        base = None
        mfile = getattr(main, "__file__", None)
        if isinstance(mfile, str) and mfile:
            base = os.path.dirname(os.path.abspath(mfile))
        if not base:
            base = os.path.join(os.path.expanduser("~"), ".brainstem", "src", "rapp_brainstem")
        return os.path.abspath(os.path.join(base, env))
    return os.path.join(os.path.expanduser("~"), ".brainstem", "src", "rapp_brainstem", "agents")


def packs_dir():
    """State root. Lives OUTSIDE the grail tree so `rm -rf src` cannot lose it."""
    env = (os.environ.get("RAPP_PACKS_STATE_DIR") or "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(os.path.expanduser("~"), ".brainstem", "packs")


def _p(*parts):
    return os.path.join(packs_dir(), *parts)


def host_version():
    """The running brainstem's version, or None when it cannot be determined."""
    main = sys.modules.get("__main__")
    v = getattr(main, "VERSION", None)
    if isinstance(v, str) and _SEMVER_RE.match(v.strip()):
        return v.strip()
    mfile = getattr(main, "__file__", None)
    roots = []
    if isinstance(mfile, str) and mfile:
        roots.append(os.path.dirname(os.path.abspath(mfile)))
    roots.append(os.path.dirname(agents_path()))
    for root in roots:
        try:
            with open(os.path.join(root, "VERSION"), encoding="utf-8") as fh:
                raw = fh.read().strip()
            if _SEMVER_RE.match(raw):
                return raw
        except Exception:  # noqa: BLE001
            continue
    return None


def safe_mode_reason():
    if (os.environ.get("BRAINSTEM_SAFE") or "").strip() == "1":
        return "BRAINSTEM_SAFE=1 is set in the brainstem's environment"
    marker = _p("SAFE-MODE")
    if os.path.exists(marker):
        return "safe mode is on because %s exists" % marker
    return None


def channel_base():
    raw = (os.environ.get("RAPP_PACKS_BASE") or "").strip() or DEFAULT_BASE
    if not raw.endswith("/"):
        raw += "/"
    return raw


def _read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return default


def _atomic_write_bytes(path, data):
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".rapp-packs-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write_json(path, obj):
    _atomic_write_bytes(path, _jdump(obj).encode("utf-8"))


def _log(line):
    """Append to grafts.log. Never raises; never carries a credential."""
    try:
        os.makedirs(packs_dir(), exist_ok=True)
        with open(_p("grafts.log"), "a", encoding="utf-8") as fh:
            fh.write("%s packs: %s\n" % (_now(), line))
    except Exception:  # noqa: BLE001
        pass


def load_config():
    cfg = _read_json(_p("config.json"), None)
    if not isinstance(cfg, dict):
        cfg = {}
    ring = cfg.get("ring")
    if ring not in RING_ORDER:
        ring = DEFAULT_RING
    return {"schema": SCHEMA_CONFIG, "ring": ring}


def save_config(cfg):
    _atomic_write_json(_p("config.json"), cfg)


def load_installed():
    state = _read_json(_p("installed.json"), None)
    if not isinstance(state, dict) or not isinstance(state.get("packs"), dict):
        state = {"schema": SCHEMA_INSTALLED, "generation": 1, "packs": {}, "rollbacks": []}
    state.setdefault("schema", SCHEMA_INSTALLED)
    state.setdefault("rollbacks", [])
    try:
        state["generation"] = int(state.get("generation") or 1)
    except (TypeError, ValueError):
        state["generation"] = 1
    return state


def save_installed(state):
    _atomic_write_json(_p("installed.json"), state)


# ─────────────────────────────────────────────────────────────────────────────
# Versions and ranges
# ─────────────────────────────────────────────────────────────────────────────


def _semver(value):
    m = _SEMVER_RE.match((value or "").strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _cmp(a, b):
    return (a > b) - (a < b)


def range_ok(spec, version):
    """Evaluate a comma-joined range like '>=0.6.0,<0.7.0'. Returns (ok, reason)."""
    spec = (spec or "").strip()
    if not spec or spec == "*":
        return True, ""
    have = _semver(version)
    if have is None:
        return False, "host version %r is not a semver" % (version,)
    for clause in [c.strip() for c in spec.split(",") if c.strip()]:
        m = re.match(r"^(>=|<=|==|!=|>|<)?\s*(\d+\.\d+\.\d+)$", clause)
        if not m:
            return False, "unparseable version clause %r" % clause
        op = m.group(1) or ">="
        want = _semver(m.group(2))
        c = _cmp(have, want)
        ok = {
            ">=": c >= 0, "<=": c <= 0, ">": c > 0,
            "<": c < 0, "==": c == 0, "!=": c != 0,
        }[op]
        if not ok:
            return False, "%s fails %s%s" % (version, op, m.group(2))
    return True, ""


def requires_ok(requires):
    """Check a manifest's `requires` against the live host. Returns (ok, reason)."""
    if not isinstance(requires, dict):
        return True, ""
    bs = requires.get("brainstem")
    if bs:
        hv = host_version()
        if hv is None:
            return True, ""  # unknown host version: allow, and say so in status
        ok, why = range_ok(bs, hv)
        if not ok:
            return False, "requires brainstem %s but the host is %s (%s)" % (bs, hv, why)
    py = requires.get("python")
    if py:
        ours = "%d.%d.%d" % sys.version_info[:3]
        ok, why = range_ok(py, ours)
        if not ok:
            return False, "requires python %s but this interpreter is %s (%s)" % (py, ours, why)
    return True, ""


def rings_for(pinned):
    """The pinned ring first, then progressively MORE stable rings."""
    if pinned not in RING_ORDER:
        pinned = DEFAULT_RING
    return tuple(RING_ORDER[RING_ORDER.index(pinned)::-1])


# ─────────────────────────────────────────────────────────────────────────────
# Fetching — allowlisted host, no cross-origin redirects
# ─────────────────────────────────────────────────────────────────────────────


def _origin(url):
    parts = urllib.parse.urlsplit(url)
    return (parts.scheme.lower(), (parts.hostname or "").lower(), parts.port)


class _PinnedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse any redirect that leaves the configured channel base."""

    def __init__(self, base):
        self._base = base

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _origin(newurl) != _origin(self._base) or not newurl.startswith(self._base):
            raise _OffsiteRedirect(newurl)
        return urllib.request.HTTPRedirectHandler.redirect_request(
            self, req, fp, code, msg, headers, newurl
        )


def fetch(rel, limit=_MAX_FILE_BYTES, base=None):
    """Fetch a path relative to the channel base. Fail closed on anything off-base."""
    base = base or channel_base()
    url = urllib.parse.urljoin(base, rel)
    if not url.startswith(base):
        raise _Refused("refused: %r resolves outside the channel base %s" % (rel, base))
    if _origin(url) != _origin(base):
        raise _Refused("refused: %s is not on the allowlisted channel host %s" % (url, base))
    opener = urllib.request.build_opener(_PinnedRedirectHandler(base))
    req = urllib.request.Request(url, headers={"User-Agent": "rapp-packs/1 (brainstem)"})
    try:
        with opener.open(req, timeout=_TIMEOUT) as resp:
            final = resp.geturl()
            if _origin(final) != _origin(base) or not final.startswith(base):
                raise _Refused(
                    "refused: the channel redirected to %s, which is off the allowlisted "
                    "base %s. Nothing was read." % (final, base)
                )
            data = resp.read(limit + 1)
    except _OffsiteRedirect as exc:
        raise _Refused(
            "refused: the channel tried to redirect to %s, which is off the allowlisted "
            "base %s. Nothing was fetched." % (exc.target, base)
        )
    except urllib.error.HTTPError as exc:
        raise _Refused("refused: %s returned HTTP %s" % (url, exc.code))
    except urllib.error.URLError as exc:
        raise _Refused("refused: could not reach %s (%s)" % (url, exc.reason))
    if len(data) > limit:
        raise _Refused("refused: %s is larger than the %d byte limit" % (url, limit))
    return data


# ─────────────────────────────────────────────────────────────────────────────
# Channel index: signature, key pinning, ring resolution
# ─────────────────────────────────────────────────────────────────────────────


def _pinned_key():
    try:
        with open(_p("channel.key"), encoding="utf-8") as fh:
            raw = fh.read().strip().lower()
        return raw if _HEX64_RE.match(raw) else None
    except Exception:  # noqa: BLE001
        return None


def load_index(may_pin=False):
    """Fetch and verify the channel index.

    Returns (index, signature_status). signature_status is one of:
      'verified-pinned'   signed by the key this brainstem already trusts
      'verified-pinned-now'  signed, and the key was just pinned TOFU
      'verified-unpinned' signed and internally consistent, but no key is pinned yet
    Anything else raises _Refused. A mismatch NEVER results in a write.
    """
    base = channel_base()
    raw = fetch("channel/index.json", limit=_MAX_INDEX_BYTES, base=base)
    try:
        sig_raw = fetch("channel/index.sig", limit=64 * 1024, base=base)
    except _Refused as exc:
        raise _Refused(
            "refused: the channel index is not signed (%s). SPEC 9 requires "
            "channel/index.sig; nothing was written." % exc
        )
    try:
        sig = json.loads(sig_raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        raise _Refused("refused: channel/index.sig is not valid JSON")
    if sig.get("schema") != SCHEMA_SIG or sig.get("alg") != "ed25519":
        raise _Refused("refused: channel/index.sig is not an %s ed25519 signature" % SCHEMA_SIG)
    pub_hex = (sig.get("public_key") or "").strip().lower()
    sig_hex = (sig.get("signature") or "").strip().lower()
    if not _HEX64_RE.match(pub_hex) or not re.match(r"^[0-9a-f]{128}$", sig_hex):
        raise _Refused("refused: channel/index.sig has a malformed key or signature")

    pinned = _pinned_key()
    if pinned and pinned != pub_hex:
        raise _Refused(
            "REFUSED: the channel index is signed by a DIFFERENT key.\n"
            "  pinned here : %s\n"
            "  presented   : %s\n"
            "A key change is a human decision, not an auto-update. Nothing was written. "
            "If this rotation is real, delete %s deliberately and re-pin." % (pinned, pub_hex, _p("channel.key"))
        )
    if not ed25519_verify(binascii.unhexlify(pub_hex), raw, binascii.unhexlify(sig_hex)):
        raise _Refused(
            "refused: channel/index.sig does not verify against channel/index.json "
            "(key %s). Nothing was written." % pub_hex
        )

    try:
        index = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        raise _Refused("refused: channel/index.json is not valid JSON")
    if index.get("schema") != SCHEMA_CHANNEL:
        raise _Refused("refused: channel index schema is %r, expected %r"
                       % (index.get("schema"), SCHEMA_CHANNEL))
    if not isinstance(index.get("packs"), list):
        raise _Refused("refused: channel index has no packs list")

    status = "verified-pinned" if pinned else "verified-unpinned"
    if not pinned and may_pin:
        _atomic_write_bytes(_p("channel.key"), (pub_hex + "\n").encode("utf-8"))
        _log("pinned channel key %s (TOFU) from %s" % (pub_hex, base))
        status = "verified-pinned-now"
    return index, status


def resolve_entry(index, name, ring):
    """The channel entry for `name` on the pinned ring, falling back to more stable rings."""
    by_ring = {}
    for entry in index.get("packs", []):
        if entry.get("name") == name:
            by_ring[entry.get("ring") or DEFAULT_RING] = entry
    for candidate in rings_for(ring):
        if candidate in by_ring:
            return by_ring[candidate]
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Manifest validation
# ─────────────────────────────────────────────────────────────────────────────


def validate_manifest(manifest, expect_name=None, expect_version=None):
    """Structural gate before any byte is fetched. Raises _Refused."""
    if not isinstance(manifest, dict):
        raise _Refused("refused: pack.json is not a JSON object")
    if manifest.get("schema") != SCHEMA_PACK:
        raise _Refused("refused: pack.json schema is %r, expected %r"
                       % (manifest.get("schema"), SCHEMA_PACK))
    name = manifest.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise _Refused("refused: pack name %r must match [a-z0-9-]{2,40}" % (name,))
    if expect_name and name != expect_name:
        raise _Refused("refused: the index calls this pack %r but its manifest says %r"
                       % (expect_name, name))
    version = manifest.get("version")
    if not isinstance(version, str) or not _SEMVER_RE.match(version):
        raise _Refused("refused: pack %s has a non-semver version %r" % (name, version))
    if expect_version and version != expect_version:
        raise _Refused("refused: the index offers %s %s but its manifest says %s"
                       % (name, expect_version, version))
    if manifest.get("kind") not in ("pack", "graft"):
        raise _Refused("refused: pack %s has kind %r (expected 'pack' or 'graft')"
                       % (name, manifest.get("kind")))
    ring = manifest.get("ring")
    if ring not in RING_ORDER:
        raise _Refused("refused: pack %s declares ring %r; expected one of %s"
                       % (name, ring, ", ".join(RING_ORDER)))
    hosts = manifest.get("hosts")
    if not isinstance(hosts, list) or not hosts or not all(isinstance(h, str) for h in hosts):
        raise _Refused("refused: pack %s has no `hosts` list (SPEC 10)" % name)
    if THIS_HOST not in hosts:
        raise _Refused("refused: pack %s declares hosts %s and does not support %r"
                       % (name, hosts, THIS_HOST))
    if manifest.get("kind") == "graft" and hosts != [THIS_HOST]:
        raise _Refused(
            "refused: graft %s must declare exactly one host (it patches a specific "
            "host's live module); it declares %s" % (name, hosts)
        )
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise _Refused("refused: pack %s declares no files" % name)
    seen = set()
    for spec in files:
        if not isinstance(spec, dict):
            raise _Refused("refused: pack %s has a malformed files entry" % name)
        path = spec.get("path")
        digest = (spec.get("sha256") or "").lower()
        install_as = spec.get("install_as") or path
        if not isinstance(path, str) or not path:
            raise _Refused("refused: pack %s has a file with no path" % name)
        if not _HEX64_RE.match(digest):
            raise _Refused("refused: pack %s file %s has a malformed sha256" % (name, path))
        if not isinstance(install_as, str) or install_as != os.path.basename(install_as) \
                or install_as in ("", ".", "..") or "/" in install_as or "\\" in install_as:
            raise _Refused("refused: pack %s would install to %r, which is not a bare filename"
                           % (name, install_as))
        if install_as == "basic_agent.py":
            raise _Refused("refused: pack %s tries to install basic_agent.py, which SPEC 1 "
                           "forbids any pack from touching" % name)
        if not install_as.endswith(".py"):
            raise _Refused("refused: pack %s would install a non-Python file %r" % (name, install_as))
        if install_as in seen:
            raise _Refused("refused: pack %s installs %s twice" % (name, install_as))
        seen.add(install_as)
    if not any(f.get("install_as", f.get("path", "")).endswith("_agent.py") for f in files):
        raise _Refused("refused: pack %s installs no *_agent.py file, so the brainstem's "
                       "loader would never see it" % name)
    return manifest


# ─────────────────────────────────────────────────────────────────────────────
# Generations
# ─────────────────────────────────────────────────────────────────────────────


def _gen_dir(n):
    return _p("generations", str(int(n)))


def list_generations():
    out = []
    for path in glob.glob(_p("generations", "*")):
        base = os.path.basename(path)
        if base.isdigit() and os.path.isdir(path):
            out.append(int(base))
    return sorted(out)


def snapshot_generation(state, generation, source_dir=None):
    """Write generation <n>: its installed.json plus the exact bytes of every file."""
    # Rooted at packs_dir() in one visible expression: a destructive call must be
    # anchored where a static reader (tools/verify_pack.py, rule grail-write-opaque)
    # can see the root, not behind a helper it would have to follow.
    gdir = os.path.join(packs_dir(), "generations", str(int(generation)))
    files_dir = os.path.join(gdir, "files")
    if os.path.isdir(gdir):
        shutil.rmtree(gdir, ignore_errors=True)
    os.makedirs(files_dir, exist_ok=True)
    apath = source_dir or agents_path()
    for pack_name, record in state.get("packs", {}).items():
        dest = os.path.join(files_dir, pack_name)
        os.makedirs(dest, exist_ok=True)
        for spec in record.get("files", []):
            install_as = spec.get("install_as")
            src = os.path.join(record.get("agents_path") or apath, install_as)
            if os.path.exists(src):
                with open(src, "rb") as fh:
                    _atomic_write_bytes(os.path.join(dest, install_as), fh.read())
    snap = dict(state)
    snap["generation"] = generation
    _atomic_write_json(os.path.join(gdir, "installed.json"), snap)


def ensure_current_generation(state):
    """bootstrap.sh installs without snapshotting. Materialise the current generation
    from the live bytes so there is always something to revert TO."""
    gen = state.get("generation", 1)
    if not os.path.isdir(_gen_dir(gen)):
        snapshot_generation(state, gen)
        _log("materialised generation %s from the live agents directory" % gen)


def prune_generations(keep=KEEP_GENERATIONS):
    gens = list_generations()
    for n in gens[:-keep] if len(gens) > keep else []:
        gdir = os.path.join(packs_dir(), "generations", str(int(n)))
        shutil.rmtree(gdir, ignore_errors=True)


def revert_to_generation(target, reason):
    """Restore generation <target>: pure file moves, offline, no network."""
    gdir = _gen_dir(target)
    snap = _read_json(os.path.join(gdir, "installed.json"), None)
    if not isinstance(snap, dict):
        raise _Refused("refused: generation %s has no usable snapshot at %s" % (target, gdir))
    current = load_installed()
    apath = agents_path()

    want = {}
    for pack_name, record in snap.get("packs", {}).items():
        for spec in record.get("files", []):
            install_as = spec.get("install_as")
            src = os.path.join(gdir, "files", pack_name, install_as)
            if not os.path.exists(src):
                raise _Refused("refused: generation %s is incomplete (missing %s/%s)"
                               % (target, pack_name, install_as))
            want[os.path.join(record.get("agents_path") or apath, install_as)] = src

    have = set()
    for pack_name, record in current.get("packs", {}).items():
        for spec in record.get("files", []):
            have.add(os.path.join(record.get("agents_path") or apath, spec.get("install_as")))

    restored, removed = [], []
    for dest, src in sorted(want.items()):
        with open(src, "rb") as fh:
            _atomic_write_bytes(dest, fh.read())
        restored.append(os.path.basename(dest))
    for dest in sorted(have - set(want)):
        try:
            os.unlink(dest)
            removed.append(os.path.basename(dest))
        except FileNotFoundError:
            pass

    new_state = dict(snap)
    new_state["generation"] = int(target)
    new_state["rollbacks"] = list(current.get("rollbacks", [])) + [{
        "at": _now(),
        "from_generation": current.get("generation"),
        "to_generation": int(target),
        "reason": reason,
    }]
    save_installed(new_state)
    _log("ROLLBACK generation %s -> %s: %s (restored %s; removed %s)"
         % (current.get("generation"), target, reason,
            ", ".join(restored) or "nothing", ", ".join(removed) or "nothing"))
    return new_state, restored, removed


# ─────────────────────────────────────────────────────────────────────────────
# Boot counter and automatic rollback (SPEC 6)
# ─────────────────────────────────────────────────────────────────────────────


def is_brainstem_host():
    """True only when this file was imported BY a running brainstem.

    The dirty-boot counter counts brainstem boots, not Python processes. Without this
    check every CLI invocation, test harness and other host that imports the file looks
    like a fresh boot that never serves a request, and three of them in a row fire a
    spurious auto-revert. (Observed, then fixed: 2026-09-20.)
    """
    main = sys.modules.get("__main__")
    if main is None:
        return False
    return callable(getattr(main, "load_agents", None)) and isinstance(
        getattr(main, "AGENTS_PATH", None), str
    )


def _boot_token():
    """Stable for the life of ONE brainstem process.

    load_agents() re-imports this file on every /chat, so a module-level token would be
    new every request and would fake a boot storm. Stash it on the live __main__ module
    instead, and fall back to the pid if that is not writable.
    """
    pid = os.getpid()
    main = sys.modules.get("__main__")
    if main is not None:
        try:
            token = getattr(main, "_rapp_packs_boot_token", None)
            if not token:
                token = "%d-%s" % (pid, binascii.hexlify(os.urandom(6)).decode())
                setattr(main, "_rapp_packs_boot_token", token)
                token = getattr(main, "_rapp_packs_boot_token", None)
            if token:
                return token
        except Exception:  # noqa: BLE001
            pass
    return "pid-%d" % pid


def load_boot():
    boot = _read_json(_p("boot.json"), None)
    if not isinstance(boot, dict):
        boot = {}
    boot.setdefault("schema", SCHEMA_BOOT)
    try:
        boot["dirty_boots"] = int(boot.get("dirty_boots") or 0)
    except (TypeError, ValueError):
        boot["dirty_boots"] = 0
    return boot


def boot_observe():
    """Called at import. Bumps the dirty-boot counter once per brainstem process and
    auto-reverts after DIRTY_BOOT_LIMIT boots that never served a request."""
    if not is_brainstem_host():
        return None  # a CLI run or a test import is not a brainstem boot
    reason = safe_mode_reason()
    if reason:
        return None  # safe mode modifies nothing, by contract
    token = _boot_token()
    boot = load_boot()
    if boot.get("boot_token") == token:
        return None
    state = load_installed()
    boot["boot_token"] = token
    boot["dirty_boots"] = boot.get("dirty_boots", 0) + 1
    boot["generation"] = state.get("generation")
    boot["last_boot_at"] = _now()
    _atomic_write_json(_p("boot.json"), boot)

    if boot["dirty_boots"] < DIRTY_BOOT_LIMIT:
        return None

    target = boot.get("last_good_generation")
    current = state.get("generation")
    note = None
    if target is None or int(target) == int(current) or not os.path.isdir(_gen_dir(target)):
        note = ("%d boots without a served request, but there is no earlier known-good "
                "generation to revert to (current=%s, last_good=%s)"
                % (boot["dirty_boots"], current, target))
        _log(note)
    else:
        try:
            revert_to_generation(
                int(target),
                "automatic: %d consecutive boots loaded packs without the brainstem "
                "serving a request" % boot["dirty_boots"],
            )
            note = "auto-reverted generation %s -> %s" % (current, target)
        except Exception as exc:  # noqa: BLE001
            note = "auto-revert to generation %s FAILED: %s" % (target, exc)
            _log(note)
    # Reset either way: a failing revert must not loop on every boot.
    boot["dirty_boots"] = 0
    boot["last_action"] = note
    boot["last_action_at"] = _now()
    _atomic_write_json(_p("boot.json"), boot)
    return note


def boot_clear():
    """Called from system_context(), i.e. inside a real /chat.

    Reaching this point proves load_agents() finished for every pack (no hang, no
    process-killing import) and the brainstem is serving a request — exactly the thing
    the dirty-boot counter defends against. It deliberately does NOT wait for the model
    to answer: a Copilot outage must never roll back working packs.
    """
    if safe_mode_reason():
        return
    boot = load_boot()
    state = load_installed()
    gen = state.get("generation")
    if boot.get("dirty_boots") == 0 and boot.get("last_good_generation") == gen:
        return
    boot["dirty_boots"] = 0
    boot["last_good_generation"] = gen
    boot["generation"] = gen
    boot["last_clear_at"] = _now()
    _atomic_write_json(_p("boot.json"), boot)


# ─────────────────────────────────────────────────────────────────────────────
# Host-version re-gating (SPEC 7)
# ─────────────────────────────────────────────────────────────────────────────


def enforce_requires():
    """Re-check every installed pack against the LIVE host on every boot.

    A grail upgrade can move the host out from under a legitimately-installed pack.
    Out of range => the files are withdrawn from AGENTS_PATH into packs/inert/<name>/
    with a reason, so the next load_agents() sweep does not load them. Back in range =>
    restored. The manager never withdraws itself.
    """
    if safe_mode_reason():
        return []
    if host_version() is None:
        # Cannot judge the host, so cannot re-gate. Never restore an inert pack on a
        # guess — `status` reports the unknown host version instead.
        return []
    state = load_installed()
    apath = agents_path()
    changed, notes = False, []
    for name, record in sorted(state.get("packs", {}).items()):
        ok, why = requires_ok(record.get("requires"))
        inert = record.get("inert")
        if not ok and not inert:
            if name == MANAGER_PACK:
                record["inert"] = {"reason": why, "since": _now(), "withdrawn": False}
                notes.append("%s is out of range (%s) but is the manager itself; kept loaded"
                             % (name, why))
                _log("out-of-range manager kept loaded: %s" % why)
                changed = True
                continue
            dest_dir = _p("inert", name)
            os.makedirs(dest_dir, exist_ok=True)
            moved = []
            for spec in record.get("files", []):
                src = os.path.join(record.get("agents_path") or apath, spec.get("install_as"))
                if os.path.exists(src):
                    with open(src, "rb") as fh:
                        _atomic_write_bytes(os.path.join(dest_dir, spec.get("install_as")), fh.read())
                    os.unlink(src)
                    moved.append(spec.get("install_as"))
            record["inert"] = {"reason": why, "since": _now(), "withdrawn": True}
            notes.append("%s went inert: %s (withdrew %s)" % (name, why, ", ".join(moved) or "nothing"))
            _log("inert %s: %s" % (name, why))
            changed = True
        elif ok and inert:
            src_dir = _p("inert", name)
            for spec in record.get("files", []):
                src = os.path.join(src_dir, spec.get("install_as"))
                if os.path.exists(src):
                    with open(src, "rb") as fh:
                        _atomic_write_bytes(
                            os.path.join(record.get("agents_path") or apath, spec.get("install_as")),
                            fh.read(),
                        )
                    os.unlink(src)
            record["inert"] = None
            notes.append("%s is back in range and was restored" % name)
            _log("restored %s: host is back in range" % name)
            changed = True
    if changed:
        save_installed(state)
    return notes


# ─────────────────────────────────────────────────────────────────────────────
# Install / update / remove
# ─────────────────────────────────────────────────────────────────────────────


def _download_pack(index, entry):
    """Fetch and verify one pack completely. Returns (manifest, [(install_as, bytes)]).

    Nothing is written by this function. Every refusal names the digest that differed.
    """
    name = entry.get("name")
    manifest_rel = entry.get("manifest") or ("packs/%s/pack.json" % name)
    want_manifest = (entry.get("manifest_sha256") or "").lower()
    if not _HEX64_RE.match(want_manifest):
        raise _Refused("refused: the index carries no usable manifest digest for %s" % name)
    raw = fetch(manifest_rel)
    got = _sha256(raw)
    if got != want_manifest:
        raise _Refused(
            "refused: MANIFEST digest mismatch for %s\n"
            "  index says : %s\n"
            "  bytes are  : %s\n"
            "Nothing was written." % (name, want_manifest, got)
        )
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        raise _Refused("refused: %s pack.json is not valid JSON" % name)
    validate_manifest(manifest, expect_name=name, expect_version=entry.get("version"))
    if manifest.get("ring") != (entry.get("ring") or DEFAULT_RING):
        raise _Refused("refused: the index lists %s on ring %r but its manifest says %r"
                       % (name, entry.get("ring"), manifest.get("ring")))
    ok, why = requires_ok(manifest.get("requires"))
    if not ok:
        raise _Refused("refused: %s %s %s. Nothing was written."
                       % (name, manifest.get("version"), why))

    payload = []
    pack_root = manifest_rel.rsplit("/", 1)[0] + "/"
    for spec in manifest["files"]:
        rel = urllib.parse.urljoin(pack_root, spec["path"])
        data = fetch(rel)
        got = _sha256(data)
        if got != spec["sha256"].lower():
            raise _Refused(
                "refused: FILE digest mismatch for %s/%s\n"
                "  manifest says : %s\n"
                "  bytes are     : %s\n"
                "The whole pack was refused; nothing was written."
                % (name, spec["path"], spec["sha256"].lower(), got)
            )
        try:
            source = data.decode("utf-8")
        except UnicodeDecodeError:
            raise _Refused("refused: %s/%s is not valid UTF-8" % (name, spec["path"]))
        try:
            ast.parse(source, filename=spec["path"])
        except SyntaxError as exc:
            raise _Refused("refused: %s/%s does not parse (line %s: %s). Nothing was written."
                           % (name, spec["path"], exc.lineno, exc.msg))
        payload.append((spec["install_as"] or os.path.basename(spec["path"]), data, got))
    return manifest, payload


def _defines_perform(source):
    """A *_agent.py with no class exposing perform() loads into nothing."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "perform":
                    return True
    return False


def _write_payload(apath, payload):
    """All-or-nothing write with rollback of anything already replaced."""
    os.makedirs(apath, exist_ok=True)
    backups, created, written = {}, [], []
    try:
        for install_as, data, _digest in payload:
            dest = os.path.join(apath, install_as)
            if os.path.exists(dest):
                with open(dest, "rb") as fh:
                    backups[dest] = fh.read()
            else:
                backups[dest] = None
                created.append(dest)
            _atomic_write_bytes(dest, data)
            written.append(dest)
    except Exception:
        for dest in reversed(written):
            old = backups.get(dest)
            try:
                if old is None:
                    os.unlink(dest)
                else:
                    _atomic_write_bytes(dest, old)
            except Exception:  # noqa: BLE001
                pass
        raise
    return created


def _record(state, manifest, payload, apath, base, ring):
    state["packs"][manifest["name"]] = {
        "name": manifest["name"],
        "version": manifest["version"],
        "kind": manifest.get("kind"),
        "ring": ring,
        "hosts": manifest.get("hosts"),
        "requires": manifest.get("requires") or {},
        "provides": manifest.get("provides") or [],
        "revert": manifest.get("revert") or "delete the installed files",
        "files": [{"install_as": ia, "sha256": d} for ia, _data, d in payload],
        "agents_path": apath,
        "source": base,
        "installed_at": _now(),
        "pinned": bool(state["packs"].get(manifest["name"], {}).get("pinned")),
        "inert": None,
    }
    return state


def _commit(state, note):
    """Advance to a new generation and record it. Snapshot AFTER the files are live."""
    new_gen = int(state.get("generation") or 1) + 1
    state["generation"] = new_gen
    save_installed(state)
    snapshot_generation(state, new_gen)
    prune_generations()
    _log("generation %s: %s" % (new_gen, note))
    return new_gen


# ─────────────────────────────────────────────────────────────────────────────
# The agent
# ─────────────────────────────────────────────────────────────────────────────

_LIVE_NOTE = ("load_agents() runs on every /chat, so this is live on your next message. "
              "No restart needed.")


class PackAgent(BasicAgent):
    """Packs — the brainstem's expansion channel."""

    def __init__(self):
        self.name = "Packs"
        self.metadata = {
            "name": self.name,
            "description": __manifest__["description"],
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": (
                            "list | install | update | remove | pin | unpin | status | "
                            "self_update | ring | revert | generations"
                        ),
                    },
                    "name": {"type": "string", "description": "Pack name, e.g. gpt-6-astra."},
                    "version": {"type": "string", "description": "Expected version, e.g. 1.0.1."},
                    "ring": {"type": "string", "description": "beta | canary | alpha | nightly."},
                    "generation": {"type": "integer", "description": "Generation number to revert to."},
                    "force": {"type": "boolean", "description": "Override the version gate or a pin."},
                },
                "required": [],
            },
        }
        BasicAgent.__init__(self, name=self.name, metadata=self.metadata)

    # The only per-request hook an agent gets. Used to clear the dirty-boot counter.
    def system_context(self):
        try:
            boot_clear()
        except Exception:  # noqa: BLE001
            pass
        return None

    def perform(self, **kwargs):
        action = str(kwargs.get("action") or "list").strip().lower()
        name = kwargs.get("name")
        name = str(name).strip() if name else None
        version = kwargs.get("version")
        version = str(version).strip() if version else None
        ring = kwargs.get("ring")
        ring = str(ring).strip().lower() if ring else None
        force = bool(kwargs.get("force"))
        generation = kwargs.get("generation")

        mutating = action in (
            "install", "update", "remove", "pin", "unpin", "self_update", "ring", "revert",
        )
        reason = safe_mode_reason()
        if mutating and reason:
            return (
                "REFUSED: %s.\n"
                "Safe mode still lists and reports, but installs, updates, pins and reverts "
                "are all refused so the grail on disk stays exactly what it is.\n"
                "Clear it with:  rm %s   (or unset BRAINSTEM_SAFE)" % (reason, _p("SAFE-MODE"))
            )

        try:
            if action == "list":
                return self._list()
            if action == "status":
                return self._status()
            if action == "generations":
                return self._generations()
            if action == "install":
                return self._install(name, version, force)
            if action == "update":
                return self._update(name, force)
            if action == "self_update":
                return self._self_update(force)
            if action == "remove":
                return self._remove(name)
            if action in ("pin", "unpin"):
                return self._pin(name, action == "pin")
            if action == "ring":
                return self._ring(ring)
            if action == "revert":
                return self._revert(generation)
        except _Refused as exc:
            return str(exc)
        except Exception as exc:  # noqa: BLE001
            _log("unexpected failure in %s: %s: %s" % (action, type(exc).__name__, exc))
            return "Packs hit an unexpected error during %r: %s: %s (nothing was written by " \
                   "this handler after the failure)" % (action, type(exc).__name__, exc)
        return ("Unknown action %r. Try: list, install, update, remove, pin, unpin, status, "
                "self_update, ring, revert, generations." % action)

    # ── read-only ──────────────────────────────────────────────────────────

    def _list(self):
        cfg = load_config()
        index, sigstat = load_index(may_pin=False)
        state = load_installed()
        lines = ["Channel: %s   ring: %s   signature: %s"
                 % (channel_base(), cfg["ring"], sigstat)]
        if sigstat == "verified-unpinned":
            lines.append("  (no channel key pinned yet — the first install pins it TOFU)")
        names = sorted({e.get("name") for e in index.get("packs", []) if e.get("name")})
        if not names:
            lines.append("The channel is empty.")
        rows = []
        for pname in names:
            entry = resolve_entry(index, pname, cfg["ring"])
            record = state.get("packs", {}).get(pname)
            offered = entry.get("version") if entry else "-"
            offered_ring = (entry.get("ring") if entry else "-") or "-"
            have = record.get("version") if record else None
            if not record:
                status = "not installed"
            elif record.get("inert"):
                status = "INERT (%s)" % record["inert"].get("reason", "")
            elif entry and _semver(offered) and _semver(have) and _semver(offered) > _semver(have):
                status = "UPDATE %s -> %s" % (have, offered)
            elif have:
                status = "installed %s%s" % (have, " [pinned]" if record.get("pinned") else "")
            else:
                status = "installed"
            rows.append((pname, offered, offered_ring, status,
                         (entry or {}).get("summary") or ""))
        width = max([len(r[0]) for r in rows] + [4])
        lines.append("")
        lines.append("%-*s  %-8s %-8s %s" % (width, "PACK", "CHANNEL", "RING", "STATUS"))
        for pname, offered, oring, status, summary in rows:
            lines.append("%-*s  %-8s %-8s %s" % (width, pname, offered, oring, status))
            if summary:
                lines.append("%s  %s" % (" " * width, summary))
        other = sorted({(e.get("name"), e.get("ring"), e.get("version"))
                        for e in index.get("packs", [])
                        if e.get("ring") != cfg["ring"]})
        if other:
            lines.append("")
            lines.append("Other rings: " + ", ".join("%s %s on %s" % (n, v, r) for n, r, v in other))
        return "\n".join(lines)

    def _status(self):
        cfg = load_config()
        state = load_installed()
        boot = load_boot()
        reason = safe_mode_reason()
        hv = host_version()
        lines = [
            "Packs status",
            "  agents path    : %s" % agents_path(),
            "  state          : %s" % packs_dir(),
            "  channel        : %s" % channel_base(),
            "  ring           : %s (fallback order: %s)" % (cfg["ring"], " -> ".join(rings_for(cfg["ring"]))),
            "  channel key    : %s" % (_pinned_key() or "not pinned yet"),
            "  host brainstem : %s" % (hv or "unknown (requires.brainstem gating is skipped)"),
            "  python         : %d.%d.%d" % sys.version_info[:3],
            "  safe mode      : %s" % (reason or "off"),
            "  generation     : %s (on disk: %s)"
            % (state.get("generation"), ", ".join(str(g) for g in list_generations()) or "none"),
            "  dirty boots    : %s of %s (last good generation: %s)"
            % (boot.get("dirty_boots", 0), DIRTY_BOOT_LIMIT, boot.get("last_good_generation", "none")),
        ]
        if boot.get("last_action"):
            lines.append("  last boot act. : %s (%s)" % (boot["last_action"], boot.get("last_action_at")))
        packs = state.get("packs", {})
        lines.append("  installed      : %d" % len(packs))
        for pname in sorted(packs):
            record = packs[pname]
            flags = []
            if record.get("pinned"):
                flags.append("pinned")
            if record.get("inert"):
                flags.append("INERT: %s" % record["inert"].get("reason"))
            ok, why = requires_ok(record.get("requires"))
            if not ok and not record.get("inert"):
                flags.append("out of range: %s" % why)
            lines.append("    %-20s %-8s %-8s %s"
                         % (pname, record.get("version"), record.get("ring") or "-",
                            "; ".join(flags)))
            for spec in record.get("files", []):
                lines.append("        %s  sha256:%s…" % (spec.get("install_as"), _short(spec.get("sha256"))))
        for entry in state.get("rollbacks", [])[-3:]:
            lines.append("  rollback       : %s  gen %s -> %s  (%s)"
                         % (entry.get("at"), entry.get("from_generation"),
                            entry.get("to_generation"), entry.get("reason")))
        return "\n".join(lines)

    def _generations(self):
        state = load_installed()
        lines = ["Generations under %s" % _p("generations")]
        for n in list_generations():
            snap = _read_json(os.path.join(_gen_dir(n), "installed.json"), {}) or {}
            packs = snap.get("packs", {})
            marker = "  <- current" if n == state.get("generation") else ""
            lines.append("  %-4s %s%s" % (n, ", ".join("%s %s" % (k, v.get("version"))
                                                       for k, v in sorted(packs.items())) or "empty", marker))
        if len(lines) == 1:
            lines.append("  none yet")
        lines.append("Revert with:  action=revert generation=<n>   (a file move, offline)")
        return "\n".join(lines)

    # ── mutating ───────────────────────────────────────────────────────────

    def _install(self, name, version, force, _self=False):
        if not name:
            return "install needs a pack name. Run action=list to see what the channel offers."
        cfg = load_config()
        state = load_installed()
        ensure_current_generation(state)
        if name in state.get("packs", {}) and not force and not _self:
            have = state["packs"][name].get("version")
            return ("%s %s is already installed. Use action=update (or force=true to "
                    "reinstall this exact version)." % (name, have))
        index, sigstat = load_index(may_pin=True)
        entry = resolve_entry(index, name, cfg["ring"])
        if entry is None:
            offered = sorted({e.get("name") for e in index.get("packs", [])})
            return ("refused: the channel has no pack %r on ring %s or any more stable ring.\n"
                    "It offers: %s" % (name, cfg["ring"], ", ".join(offered) or "nothing"))
        if version and entry.get("version") != version:
            return ("refused: you asked for %s %s but the channel offers %s on ring %s. "
                    "Nothing was written." % (name, version, entry.get("version"), entry.get("ring")))
        manifest, payload = _download_pack(index, entry)
        apath = agents_path()
        created = _write_payload(apath, payload)
        _record(state, manifest, payload, apath, channel_base(), entry.get("ring") or DEFAULT_RING)
        gen = _commit(state, "installed %s %s" % (name, manifest["version"]))
        lines = [
            "Installed %s %s (ring %s, generation %s)." % (name, manifest["version"], entry.get("ring"), gen),
            "  channel signature : %s" % sigstat,
            "  manifest sha256   : %s…" % _short(entry.get("manifest_sha256")),
            "  files             : %s" % ", ".join(
                "%s (sha256:%s…, %s)" % (ia, _short(d), "new" if os.path.join(apath, ia) in created else "replaced")
                for ia, _b, d in payload),
            "  into              : %s" % apath,
            "  provides          : %s" % (", ".join(manifest.get("provides") or []) or "-"),
            "  revert            : %s" % manifest.get("revert"),
            _LIVE_NOTE,
        ]
        return "\n".join(lines)

    def _update(self, name, force):
        cfg = load_config()
        state = load_installed()
        ensure_current_generation(state)
        installed = state.get("packs", {})
        if not installed:
            return "Nothing is installed yet. Run action=install name=<pack>."
        targets = [name] if name and name.lower() != "all" else sorted(installed)
        if name and name.lower() != "all" and name not in installed:
            return "refused: %r is not installed. Use action=install." % name
        index, sigstat = load_index(may_pin=True)
        report, changed = [], []
        for pname in targets:
            record = installed[pname]
            if record.get("pinned") and not force:
                report.append("  %-20s pinned at %s — skipped (force=true overrides)"
                              % (pname, record.get("version")))
                continue
            entry = resolve_entry(index, pname, cfg["ring"])
            if entry is None:
                report.append("  %-20s not on ring %s — skipped" % (pname, cfg["ring"]))
                continue
            have, offer = _semver(record.get("version")), _semver(entry.get("version"))
            if not force and not (offer and have and offer > have):
                report.append("  %-20s %s is current (channel offers %s) — skipped"
                              % (pname, record.get("version"), entry.get("version")))
                continue
            try:
                manifest, payload = _download_pack(index, entry)
            except _Refused as exc:
                report.append("  %-20s %s" % (pname, str(exc).replace("\n", "\n      ")))
                continue
            apath = record.get("agents_path") or agents_path()
            stale = {s.get("install_as") for s in record.get("files", [])} - {ia for ia, _b, _d in payload}
            _write_payload(apath, payload)
            for gone in sorted(stale):
                try:
                    os.unlink(os.path.join(apath, gone))
                except FileNotFoundError:
                    pass
            _record(state, manifest, payload, apath, channel_base(), entry.get("ring") or DEFAULT_RING)
            changed.append("%s %s -> %s" % (pname, record.get("version"), manifest["version"]))
            report.append("  %-20s %s -> %s%s"
                          % (pname, record.get("version"), manifest["version"],
                             ("; dropped " + ", ".join(sorted(stale))) if stale else ""))
        head = ["Update (ring %s, signature %s)" % (cfg["ring"], sigstat)]
        if changed:
            gen = _commit(state, "updated " + "; ".join(changed))
            head[0] += "  -> generation %s" % gen
            report.append(_LIVE_NOTE)
        else:
            head[0] += "  -> nothing changed"
        return "\n".join(head + report)

    def _self_update(self, force):
        """The manager updates itself. This is how every future fix reaches an install."""
        cfg = load_config()
        state = load_installed()
        ensure_current_generation(state)
        record = state.get("packs", {}).get(MANAGER_PACK)
        have = record.get("version") if record else None
        index, sigstat = load_index(may_pin=True)
        entry = resolve_entry(index, MANAGER_PACK, cfg["ring"])
        if entry is None:
            return ("refused: the channel offers no %r on ring %s or any more stable ring."
                    % (MANAGER_PACK, cfg["ring"]))
        if have and not force:
            a, b = _semver(have), _semver(entry.get("version"))
            if not (a and b and b > a):
                return ("Packs is already %s; the channel offers %s on ring %s. Nothing to do "
                        "(force=true reinstalls)." % (have, entry.get("version"), entry.get("ring")))
        manifest, payload = _download_pack(index, entry)
        # Extra gate for the one pack that cannot be re-fetched by a broken manager:
        # refuse a replacement that would not actually register an agent.
        for install_as, data, _digest in payload:
            if install_as.endswith("_agent.py") and not _defines_perform(data.decode("utf-8")):
                raise _Refused(
                    "refused: the replacement %s defines no class with a perform() method, so "
                    "it would install a manager the brainstem cannot load. Nothing was written."
                    % install_as
                )
        apath = record.get("agents_path") if record else agents_path()
        apath = apath or agents_path()
        _write_payload(apath, payload)
        _record(state, manifest, payload, apath, channel_base(), entry.get("ring") or DEFAULT_RING)
        gen = _commit(state, "self_update %s -> %s" % (have or "(unrecorded)", manifest["version"]))
        return "\n".join([
            "Packs updated itself: %s -> %s (ring %s, generation %s)."
            % (have or "(unrecorded)", manifest["version"], entry.get("ring"), gen),
            "  channel signature : %s" % sigstat,
            "  files             : %s" % ", ".join("%s (sha256:%s…)" % (ia, _short(d))
                                                   for ia, _b, d in payload),
            "  into              : %s" % apath,
            "  previous set kept : %s" % _gen_dir(gen - 1),
            _LIVE_NOTE,
        ])

    def _remove(self, name):
        if not name:
            return "remove needs a pack name."
        state = load_installed()
        ensure_current_generation(state)
        record = state.get("packs", {}).get(name)
        if not record:
            return "refused: %r is not installed." % name
        if name == MANAGER_PACK:
            return ("refused: removing %r would remove the manager that is answering you. "
                    "Reinstall it with bootstrap.sh if you really want it gone." % name)
        apath = record.get("agents_path") or agents_path()
        deleted, missing = [], []
        for spec in record.get("files", []):
            dest = os.path.join(apath, spec.get("install_as"))
            try:
                os.unlink(dest)
                deleted.append(spec.get("install_as"))
            except FileNotFoundError:
                missing.append(spec.get("install_as"))
        inert_dir = os.path.join(packs_dir(), "inert", name)
        if os.path.isdir(inert_dir):
            shutil.rmtree(inert_dir, ignore_errors=True)
        state["packs"].pop(name, None)
        gen = _commit(state, "removed %s %s" % (name, record.get("version")))
        lines = ["Removed %s %s (generation %s)." % (name, record.get("version"), gen),
                 "  deleted : %s" % (", ".join(deleted) or "nothing")]
        if missing:
            lines.append("  already gone : %s" % ", ".join(missing))
        lines.append("  nothing else in %s was touched." % apath)
        lines.append("  the previous set is still on disk at %s — action=revert generation=%s"
                     % (_gen_dir(gen - 1), gen - 1))
        return "\n".join(lines)

    def _pin(self, name, pin):
        if not name:
            return "%s needs a pack name." % ("pin" if pin else "unpin")
        state = load_installed()
        record = state.get("packs", {}).get(name)
        if not record:
            return "refused: %r is not installed." % name
        record["pinned"] = bool(pin)
        save_installed(state)
        _log("%s %s" % ("pinned" if pin else "unpinned", name))
        return ("%s is pinned at %s — action=update will skip it until you unpin (or pass "
                "force=true)." % (name, record.get("version"))) if pin else \
               ("%s is unpinned and will follow the channel again." % name)

    def _ring(self, ring):
        cfg = load_config()
        if not ring:
            return ("This brainstem is pinned to ring %s (fallback order: %s). Set another "
                    "with action=ring ring=<%s>." % (cfg["ring"], " -> ".join(rings_for(cfg["ring"])),
                                                     "|".join(RING_ORDER)))
        if ring not in RING_ORDER:
            return "refused: %r is not a ring. Choose one of: %s" % (ring, ", ".join(RING_ORDER))
        was = cfg["ring"]
        cfg["ring"] = ring
        save_config(cfg)
        _log("ring %s -> %s" % (was, ring))
        return ("Ring %s -> %s (fallback order: %s). Nothing was installed; run action=list to "
                "see what this ring offers, then action=update."
                % (was, ring, " -> ".join(rings_for(ring))))

    def _revert(self, generation):
        gens = list_generations()
        state = load_installed()
        if generation is None:
            earlier = [g for g in gens if g < int(state.get("generation") or 1)]
            if not earlier:
                return ("refused: there is no earlier generation to revert to. Current is %s; "
                        "on disk: %s" % (state.get("generation"), gens or "none"))
            generation = earlier[-1]
        try:
            generation = int(generation)
        except (TypeError, ValueError):
            return "refused: generation must be a number. On disk: %s" % (gens or "none")
        if generation not in gens:
            return "refused: generation %s is not on disk. Available: %s" % (generation, gens or "none")
        new_state, restored, removed = revert_to_generation(generation, "requested by the user")
        boot = load_boot()
        boot["dirty_boots"] = 0
        boot["last_good_generation"] = generation
        boot["generation"] = generation
        _atomic_write_json(_p("boot.json"), boot)
        return "\n".join([
            "Reverted to generation %s." % generation,
            "  restored : %s" % (", ".join(restored) or "nothing"),
            "  removed  : %s" % (", ".join(removed) or "nothing"),
            "  packs    : %s" % (", ".join("%s %s" % (k, v.get("version"))
                                           for k, v in sorted(new_state.get("packs", {}).items())) or "none"),
            "  A revert is a file move — offline, instant, and recorded in installed.json.",
            _LIVE_NOTE,
        ])


# ─────────────────────────────────────────────────────────────────────────────
# Boot hooks. These run on import, i.e. inside load_agents(), on every /chat.
# They must be cheap and MUST NEVER raise: an exception here would quarantine the
# manager and take the recovery path with it.
# ─────────────────────────────────────────────────────────────────────────────

try:
    boot_observe()
except Exception as _exc:  # noqa: BLE001
    _log("boot_observe failed: %s: %s" % (type(_exc).__name__, _exc))
try:
    enforce_requires()
except Exception as _exc:  # noqa: BLE001
    _log("enforce_requires failed: %s: %s" % (type(_exc).__name__, _exc))


if __name__ == "__main__":
    _agent = PackAgent()
    _args = {}
    for _a in sys.argv[1:]:
        if "=" in _a:
            _k, _v = _a.split("=", 1)
            if _v.lower() in ("true", "false"):
                _args[_k] = _v.lower() == "true"
            elif _v.isdigit():
                _args[_k] = int(_v)
            else:
                _args[_k] = _v
        else:
            _args["action"] = _a
    print(_agent.perform(**_args))
