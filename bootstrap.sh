#!/usr/bin/env bash
# A quoted Bash body also makes the documented curl | sh entry point work with dash.
if ! command -v bash >/dev/null 2>&1; then
    printf '%s\n' 'refused: bash is required; install Bash and run this bootstrap again.' >&2
    exit 1
fi
bash -s -- "$@" <<'RAPP_BOOTSTRAP'
set -euo pipefail
umask 077

if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' 'refused: python3 is required; install Python 3.9 or newer.' >&2
    exit 1
fi
if command -v curl >/dev/null 2>&1; then
    fetch_tool=curl
elif command -v wget >/dev/null 2>&1; then
    fetch_tool=wget
else
    printf '%s\n' 'refused: curl or wget is required; install one to download the channel.' >&2
    exit 1
fi
if command -v shasum >/dev/null 2>&1; then
    sha_tool=shasum
elif command -v sha256sum >/dev/null 2>&1; then
    sha_tool=sha256sum
elif command -v openssl >/dev/null 2>&1; then
    sha_tool=openssl
else
    printf '%s\n' 'refused: install shasum, sha256sum, or openssl for SHA-256 verification.' >&2
    exit 1
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
cat > "$tmp/channel.py" <<'PY'
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import subprocess
import sys
import tempfile
from urllib.parse import quote, urljoin, urlsplit, urlunsplit


class Refused(Exception):
    pass


# ── Ed25519 verify (RFC 8032), standard library only ────────────────────────
# Embedded rather than fetched: a signature check that depends on downloading its own
# verifier is not a signature check. Mirrors tools/ed25519_pure.py, which is cross-checked
# against RFC 8032 test vector 1 and against `openssl pkeyutl -sign -rawin`.
_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493


def _inv(x):
    return pow(x, _P - 2, _P)


_D = -121665 * _inv(121666) % _P
_SQ = pow(2, (_P - 1) // 4, _P)


def _recover_x(y, sign):
    if y >= _P:
        return None
    x2 = (y * y - 1) * _inv(_D * y * y + 1) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQ % _P
    if (x * x - x2) % _P != 0:
        return None
    return _P - x if (x & 1) != sign else x


_GY = 4 * _inv(5) % _P
_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _add(P, Q):
    A = (P[1] - P[0]) * (Q[1] - Q[0]) % _P
    B = (P[1] + P[0]) * (Q[1] + Q[0]) % _P
    C = 2 * P[3] * Q[3] * _D % _P
    D = 2 * P[2] * Q[2] % _P
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _P, G * H % _P, F * G % _P, E * H % _P)


def _mul(s, P):
    Q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        s >>= 1
    return Q


def _decompress(b):
    if len(b) != 32:
        return None
    y = int.from_bytes(b, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


def ed25519_verify(public, message, signature):
    if len(public) != 32 or len(signature) != 64:
        return False
    A = _decompress(public)
    R = _decompress(signature[:32])
    if A is None or R is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        return False
    h = int.from_bytes(hashlib.sha512(signature[:32] + public + message).digest(), "little") % _L
    sB, hA = _mul(s, _G), _mul(h, A)
    RhA = _add(R, hA)
    if (sB[0] * RhA[2] - RhA[0] * sB[2]) % _P != 0:
        return False
    return (sB[1] * RhA[2] - RhA[1] * sB[2]) % _P == 0


def relative_path(value):
    if (
        not isinstance(value, str) or not value or value in (".", "..")
        or PurePosixPath(value).is_absolute() or PureWindowsPath(value).drive
        or "\\" in value or ".." in PurePosixPath(value).parts
        or any(ord(char) < 32 or ord(char) == 127 or "\ud800" <= char <= "\udfff" for char in value)
    ):
        raise Refused("channel path must be a relative file inside the pack")
    return value


def digest(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise Refused("channel SHA-256 must contain 64 lowercase hexadecimal characters")
    return value


def read_json(path):
    try:
        with Path(path).open(encoding="utf-8") as stream:
            return json.load(stream)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise Refused(Path(path).name + " is not valid UTF-8 JSON") from exc


def normalize_base(value):
    try:
        parts = urlsplit(value)
        parts.port
    except ValueError as exc:
        raise Refused("RAPP_PACKS_BASE is not a valid URL") from exc
    if not parts.hostname or parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise Refused("RAPP_PACKS_BASE must have a host and no credentials, query, or fragment")
    if parts.scheme != "https" and not (
        parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost")
    ):
        raise Refused("RAPP_PACKS_BASE must use HTTPS (HTTP is allowed only for 127.0.0.1 or localhost)")
    if ".." in PurePosixPath(parts.path).parts or any(
        ord(char) < 32 or ord(char) == 127 or "\ud800" <= char <= "\udfff" for char in value
    ):
        raise Refused("RAPP_PACKS_BASE contains an unsafe path")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") + "/", "", ""))


def allowed_url(base, value):
    try:
        actual = urlsplit(value)
        expected = urlsplit(base)
        allowed = (
            value.startswith(base)
            and actual.scheme == expected.scheme
            and actual.hostname == expected.hostname
            and actual.port == expected.port
            and actual.username is None and actual.password is None
        )
    except ValueError as exc:
        raise Refused("redirect has an invalid URL") from exc
    if not allowed:
        raise Refused(
            "redirect to host {} is outside the allowed channel base".format(actual.hostname or "<unknown>")
        )


def response_headers(text):
    status, location = None, None
    for line in text.splitlines():
        match = re.match(r"\s*HTTP/\S+\s+(\d{3})", line)
        if match:
            status, location = int(match.group(1)), None
        elif line.strip().lower().startswith("location:"):
            location = re.sub(r"\s+\[following\]\s*$", "", line.strip().split(":", 1)[1].strip())
    return status, location


def fetch(tool, base, relative, destination):
    url = base + quote(relative_path(relative), safe="/")
    protocol = "=http,https" if urlsplit(base).scheme == "http" else "=https"
    for _ in range(11):
        allowed_url(base, url)
        if tool == "curl":
            headers = destination + ".headers"
            result = subprocess.run(
                [
                    "curl", "--proto", protocol, "--proto-redir", protocol, "-fsSL",
                    "--max-redirs", "0", "--connect-timeout", "15", "--max-time", "120",
                    "-D", headers, "-o", destination, "-w", "\n%{url_effective}", url,
                ],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            header_text = Path(headers).read_text(encoding="utf-8", errors="replace") if Path(headers).exists() else ""
            effective = result.stdout.splitlines()[-1] if result.stdout.strip() else url
        else:
            result = subprocess.run(
                ["wget", "--server-response", "--max-redirect=0", "--timeout=30", "--tries=1", "-O", destination, url],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            header_text, effective = result.stderr, url
        allowed_url(base, effective)
        status, location = response_headers(header_text)
        if status in (301, 302, 303, 307, 308) and location:
            # Inspect each hop before following it; even an intermediate off-owner hop is refused.
            url = urljoin(url, location)
            allowed_url(base, url)
            continue
        if result.returncode != 0 or status is None or not 200 <= status < 300:
            raise Refused(
                "fetch failed for {} ({} exit {}, HTTP {}); check the channel URL and connection".format(
                    relative, tool, result.returncode, status if status is not None else "unavailable"
                )
            )
        return
    raise Refused("too many redirects while fetching " + relative)


RING_ORDER = ("beta", "canary", "alpha", "nightly")   # most stable first (SPEC 8)


def state_dir():
    return Path.home() / ".brainstem" / "packs"


def verify_signature(index_path, sig_path):
    """SPEC 9: refuse an index that is unsigned, badly signed, or signed by a key this
    machine has not already pinned. Returns the public key hex; writes nothing."""
    message = Path(index_path).read_bytes()
    signature = read_json(sig_path)
    if not isinstance(signature, dict) or signature.get("schema") != "rapp-sig/1" \
            or signature.get("alg") != "ed25519":
        raise Refused("channel/index.sig is not an rapp-sig/1 ed25519 signature")
    public = str(signature.get("public_key", "")).strip().lower()
    sig = str(signature.get("signature", "")).strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", public) is None or re.fullmatch(r"[0-9a-f]{128}", sig) is None:
        raise Refused("channel/index.sig has a malformed key or signature")
    pin = state_dir() / "channel.key"
    if pin.is_file():
        pinned = pin.read_text(encoding="utf-8").strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", pinned) and pinned != public:
            raise Refused(
                "the channel index is signed by a DIFFERENT key.\n"
                "  pinned here : " + pinned + "\n"
                "  presented   : " + public + "\n"
                "A key change is a human decision, not an auto-update. Nothing was written. "
                "If this rotation is real, delete " + str(pin) + " deliberately and re-pin."
            )
    if not ed25519_verify(bytes.fromhex(public), message, bytes.fromhex(sig)):
        raise Refused(
            "channel/index.sig does not verify against channel/index.json (key "
            + public + "). Nothing was written."
        )
    print(public)


def pin_key(public):
    """Trust on first use. Called only AFTER the signature verified."""
    root = state_dir()
    root.mkdir(parents=True, exist_ok=True)
    pin = root / "channel.key"
    if not pin.exists():
        handle = os.open(str(pin), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(public + "\n")
    config = root / "config.json"
    if not config.exists():
        with config.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump({"ring": os.environ.get("RAPP_PACKS_RING") or "beta",
                       "schema": "rapp-packs-config/1"}, stream, indent=2, sort_keys=True)
            stream.write("\n")


def index_reference(path, ring=None):
    index = read_json(path)
    if not isinstance(index, dict) or index.get("schema") != "rapp-channel/1" or not isinstance(index.get("packs"), list):
        raise Refused("index.json is not a rapp-channel/1 index")
    ring = ring if ring in RING_ORDER else "beta"
    by_ring = {}
    for entry in index["packs"]:
        if not isinstance(entry, dict) or entry.get("name") != "pack-manager":
            continue
        where = entry.get("ring", "beta")
        if where in by_ring:
            raise Refused("index.json lists pack-manager twice on ring " + where)
        by_ring[where] = entry
    # Fall back only toward MORE stable rings, never less.
    for candidate in RING_ORDER[RING_ORDER.index(ring)::-1]:
        if candidate in by_ring:
            entry = by_ring[candidate]
            print(
                relative_path(entry.get("manifest"))
                + "\t" + digest(entry.get("manifest_sha256"))
                + "\t" + candidate
            )
            return
    raise Refused(
        "index.json offers no pack-manager on ring " + ring + " or any more stable ring"
        + (" (it offers: " + ", ".join(sorted(by_ring)) + ")" if by_ring else "")
    )


def manifest_files(path):
    manifest = read_json(path)
    if not isinstance(manifest, dict) or manifest.get("schema") != "rapp-pack/1" or manifest.get("name") != "pack-manager":
        raise Refused("pack.json must describe the rapp-pack/1 pack-manager")
    version = manifest.get("version")
    if not isinstance(version, str) or re.fullmatch(r"\d+\.\d+\.\d+", version, re.ASCII) is None:
        raise Refused("pack-manager version must have the form 1.0.0")
    if manifest.get("kind") not in ("pack", "graft"):
        raise Refused("pack-manager kind must be pack or graft")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise Refused("pack-manager files must be a non-empty list")
    names = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise Refused("pack-manager file entries must be objects")
        source = relative_path(entry.get("path"))
        installed = entry.get("install_as")
        if (
            not isinstance(installed, str) or not installed.endswith(".py")
            or "/" in installed or "\\" in installed or ".." in installed
            or PureWindowsPath(installed).drive
            or any(ord(char) < 32 or ord(char) == 127 or "\ud800" <= char <= "\udfff" for char in installed)
        ):
            raise Refused("install_as must be a bare Python filename")
        if installed.casefold() == "basic_agent.py":
            raise Refused("basic_agent.py must never be installed or overwritten")
        if installed.casefold() in names:
            raise Refused("duplicate install_as filename in pack-manager")
        names.add(installed.casefold())
        digest(entry.get("sha256"))
        entry["path"] = source
    return manifest


def install(manifest_path, scratch, agents, base, pid, ring="beta", manifest_digest=""):
    manifest = manifest_files(manifest_path)
    scratch, agents = Path(scratch), Path(agents)
    state_dir = Path.home() / ".brainstem" / "packs"
    state_dir.mkdir(parents=True, exist_ok=True)
    registry = state_dir / "installed.json"
    if registry.is_symlink():
        raise Refused("installed.json must not be a symlink")
    state = read_json(registry) if registry.exists() else {
        "schema": "rapp-installed/1", "generation": 1, "packs": {}, "rollbacks": []
    }
    if not isinstance(state, dict) or state.get("schema") != "rapp-installed/1" or not isinstance(state.get("packs"), dict):
        raise Refused("installed.json has an unsupported schema; preserve and repair it before installing")
    state.setdefault("generation", 1)
    state.setdefault("rollbacks", [])
    targets = [agents / entry["install_as"] for entry in manifest["files"]]
    if any(target.is_symlink() or target.exists() and not target.is_file() for target in targets):
        raise Refused("an installation target is a symlink or not a regular file")
    # Field-for-field the shape packs/pack-manager/pack_agent.py::_record() writes, so the
    # manager can manage what the bootstrap installed instead of treating it as foreign.
    previous = state["packs"].get("pack-manager") or {}
    state["packs"]["pack-manager"] = {
        "name": "pack-manager", "version": manifest["version"], "kind": manifest["kind"],
        "ring": ring if ring in RING_ORDER else "beta",
        "hosts": manifest.get("hosts") or ["brainstem"],
        "requires": manifest.get("requires") or {},
        "provides": manifest.get("provides") or [],
        "revert": manifest.get("revert") or "delete the installed files",
        "files": [{"install_as": entry["install_as"], "sha256": entry["sha256"]} for entry in manifest["files"]],
        "agents_path": str(agents.resolve()), "source": base,
        "installed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pinned": bool(previous.get("pinned")),
        "inert": None,
    }
    created, staged, committed = [], [], []
    try:
        for number, target in enumerate(targets, 1):
            stage = agents / ("." + target.name + ".tmp." + pid)
            with stage.open("xb"):
                pass
            created.append(stage)
            subprocess.run(["cp", str(scratch / ("file." + str(number))), str(stage)], check=True, stderr=subprocess.PIPE)
            stage.chmod(0o644)
            backup = None
            if target.exists():
                backup = agents / ("." + target.name + ".rollback." + pid)
                with backup.open("xb"):
                    pass
                created.append(backup)
                subprocess.run(["cp", "-p", str(target), str(backup)], check=True, stderr=subprocess.PIPE)
            staged.append((stage, target, backup))
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", prefix=".installed.", dir=state_dir, delete=False
        ) as stream:
            receipt = Path(stream.name)
            created.append(receipt)
            json.dump(state, stream, indent=2, sort_keys=True, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        for stage, target, backup in staged:
            subprocess.run(["mv", str(stage), str(target)], check=True, stderr=subprocess.PIPE)
            committed.append((target, backup))
        os.replace(str(receipt), str(registry))
    except (OSError, subprocess.CalledProcessError) as exc:
        rollback_errors = []
        for target, backup in reversed(committed):
            try:
                if backup is None:
                    target.unlink()
                else:
                    os.replace(str(backup), str(target))
            except OSError:
                rollback_errors.append(target.name)
        if rollback_errors:
            # Leave backups available for recovery instead of deleting the only original.
            created = [path for path in created if ".rollback." not in path.name]
            raise Refused("installation failed; rollback also failed for " + ", ".join(rollback_errors)) from exc
        raise Refused("installation failed; original agents and installed.json were preserved") from exc
    finally:
        for path in created:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                print("warning: unable to remove installer temporary file " + str(path), file=sys.stderr)


def main():
    command, args = sys.argv[1], sys.argv[2:]
    if command == "base":
        print(normalize_base(args[0]))
    elif command == "target":
        print(Path(args[0]).expanduser().resolve())
    elif command == "fetch":
        fetch(*args)
    elif command == "index":
        index_reference(*args)
    elif command == "signature":
        verify_signature(args[0], args[1])
    elif command == "pin":
        pin_key(args[0])
    elif command == "manifest":
        for entry in manifest_files(args[0])["files"]:
            print(entry["install_as"] + "\t" + entry["path"] + "\t" + entry["sha256"])
    elif command == "install":
        install(*args)
    else:
        raise Refused("unknown bootstrap operation")


try:
    main()
except Refused as exc:
    print("refused: " + str(exc), file=sys.stderr)
    sys.exit(1)
except OSError as exc:
    print("refused: filesystem or download tool error: " + (exc.strerror or "operation failed"), file=sys.stderr)
    sys.exit(1)
PY

base=$(python3 "$tmp/channel.py" base "${RAPP_PACKS_BASE:-https://kody-w.github.io/rapp-packs/}")
agents_dir=$(python3 "$tmp/channel.py" target "${1:-${AGENTS_PATH:-${HOME:?HOME is required}/.brainstem/src/rapp_brainstem/agents}}")
if ! mkdir -p "$agents_dir" || [ ! -w "$agents_dir" ]; then
    printf 'refused: agents directory is not writable: %s\n' "$agents_dir" >&2
    exit 1
fi

fetch() {
    python3 "$tmp/channel.py" fetch "$fetch_tool" "$base" "$1" "$2"
}
sha256() {
    case "$sha_tool" in
        shasum) shasum -a 256 "$1" | awk '{print $1}' ;;
        sha256sum) sha256sum "$1" | awk '{print $1}' ;;
        openssl) openssl dgst -sha256 "$1" | awk '{print $NF}' ;;
    esac
}

fetch 'channel/index.json' "$tmp/index.json"
if ! fetch 'channel/index.sig' "$tmp/index.sig" 2> "$tmp/sig-fetch-error"; then
    cat "$tmp/sig-fetch-error" >&2
    printf '%s\n' 'refused: the channel index is not signed (channel/index.sig could not be fetched). SPEC 9 requires a signed index; nothing was written.' >&2
    exit 1
fi
channel_key=$(python3 "$tmp/channel.py" signature "$tmp/index.json" "$tmp/index.sig")
printf 'channel index signed by %s\n' "$channel_key"
python3 "$tmp/channel.py" index "$tmp/index.json" "${RAPP_PACKS_RING:-beta}" > "$tmp/manifest-ref"
tab=$(printf '\t')
IFS="$tab" read -r manifest expected_manifest ring < "$tmp/manifest-ref"
fetch "$manifest" "$tmp/pack.json"
actual_manifest=$(sha256 "$tmp/pack.json")
if [ "$actual_manifest" != "$expected_manifest" ]; then
    printf 'refused: manifest digest mismatch (index says %s, bytes are %s)\n' "$expected_manifest" "$actual_manifest" >&2
    exit 1
fi
python3 "$tmp/channel.py" manifest "$tmp/pack.json" > "$tmp/files.tsv"
number=0
while IFS="$tab" read -r install_as path expected; do
    number=$((number + 1))
    fetched="$tmp/file.$number"
    fetch "packs/pack-manager/$path" "$fetched"
    actual=$(sha256 "$fetched")
    if [ "$actual" != "$expected" ]; then
        printf 'refused: %s digest mismatch (manifest says %s, bytes are %s)\n' "$path" "$expected" "$actual" >&2
        exit 1
    fi
    if ! python3 -c 'import ast,sys; ast.parse(open(sys.argv[1],encoding="utf-8").read())' "$fetched" 2> "$tmp/parse-error"; then
        printf 'refused: Python parse failed for %s\n' "$path" >&2
        exit 1
    fi
done < "$tmp/files.tsv"

python3 "$tmp/channel.py" pin "$channel_key"
python3 "$tmp/channel.py" install "$tmp/pack.json" "$tmp" "$agents_dir" "$base" "$$" "$ring" "$expected_manifest"
while IFS="$tab" read -r install_as path expected; do
    printf 'installed %s -> %s/%s\n' "$install_as" "$agents_dir" "$install_as"
done < "$tmp/files.tsv"
printf 'ring %s; channel key pinned at %s/.brainstem/packs/channel.key\n' "$ring" "$HOME"
printf '%s\n' 'The brainstem reloads agents on every /chat — Packs is live on your next message. No restart needed.'
RAPP_BOOTSTRAP
