#!/usr/bin/env python3
"""Build a digest-verified channel; --check compares bytes without writing."""

import argparse
from datetime import datetime, timezone
import difflib
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sys
import tempfile
from xml.etree import ElementTree
from xml.sax.saxutils import escape, quoteattr

import importlib.util as _ilu

_ED_SPEC = _ilu.spec_from_file_location(
    "rapp_pack_ed25519", Path(__file__).with_name("ed25519_pure.py")
)
if _ED_SPEC is None or _ED_SPEC.loader is None:
    raise ImportError("cannot load the adjacent ed25519_pure.py")
ed25519 = _ilu.module_from_spec(_ED_SPEC)
_ED_SPEC.loader.exec_module(ed25519)


DEFAULT_BASE = "https://kody-w.github.io/rapp-packs/"
DEFAULT_ROOT = Path(__file__).resolve().parent.parent
NAME_RE = re.compile(r"[a-z0-9-]{2,40}", re.ASCII)
VERSION_RE = re.compile(r"\d+\.\d+\.\d+", re.ASCII)
DIGEST_RE = re.compile(r"[0-9a-f]{64}", re.ASCII)
SIGNATURE_RE = re.compile(r"[0-9a-f]{128}", re.ASCII)
# Most stable first. A release stabilises nightly -> alpha -> canary -> beta, so a client
# pinned to a ring falls back only toward MORE stable rings, never less (SPEC 8). The
# estate already ships these four words; do not coin a fifth.
RING_ORDER = ("beta", "canary", "alpha", "nightly")
DEFAULT_RING = "beta"
DEFAULT_KEY = Path.home() / ".brainstem" / "packs" / "channel-signing.key"


class BuildRefused(Exception):
    """A channel cannot be built without violating its contract."""


def violation(path, rule, message, line=None):
    location = str(path) if line is None else "{}:{}".format(path, line)
    return "{}: {}: {}".format(location, rule, message)


def relative_source_path(value):
    return (
        isinstance(value, str)
        and bool(value.strip())
        and not any(ord(char) < 32 or ord(char) == 127 or "\ud800" <= char <= "\udfff" for char in value)
        and "\\" not in value
        and not PurePosixPath(value).is_absolute()
        and not PureWindowsPath(value).drive
        and ".." not in PurePosixPath(value).parts
        and value not in (".", "..")
    )


def read_manifest(path):
    """Return the parsed manifest, the exact hashed bytes, and read violations."""
    path = Path(path)
    try:
        raw = path.read_bytes()
        return json.load(io.StringIO(raw.decode("utf-8"))), raw, []
    except json.JSONDecodeError as exc:
        return None, b"", [
            violation(path, "manifest-schema", "invalid JSON: " + exc.msg, exc.lineno)
        ]
    except UnicodeError:
        return None, b"", [
            violation(path, "manifest-schema", "manifest must be UTF-8")
        ]
    except OSError as exc:
        return None, b"", [
            violation(path, "manifest-schema", "cannot read manifest: " + exc.strerror)
        ]


def manifest_violations(manifest, path):
    """The common manifest schema used by both the builder and verifier."""
    errors = []

    def bad(message):
        errors.append(violation(path, "manifest-schema", message))

    if not isinstance(manifest, dict):
        return [violation(path, "manifest-schema", "manifest must be an object")]
    if manifest.get("schema") != "rapp-pack/1":
        bad("schema must be rapp-pack/1")
    for key, pattern in (("name", NAME_RE), ("version", VERSION_RE)):
        value = manifest.get(key)
        if not isinstance(value, str) or pattern.fullmatch(value) is None:
            bad("{} has an invalid or missing value".format(key))
    if manifest.get("kind") not in ("pack", "graft"):
        bad("kind must be pack or graft")
    if manifest.get("ring") not in RING_ORDER:
        bad("ring must be one of " + ", ".join(RING_ORDER))
    hosts = manifest.get("hosts")
    if not isinstance(hosts, list) or not hosts or not all(
        isinstance(host, str) and host.strip() for host in hosts
    ):
        bad("hosts must be a non-empty list of strings (SPEC 10)")
    elif manifest.get("kind") == "graft" and len(hosts) != 1:
        bad("a graft patches one host's live module and must declare exactly one host")
    for key in ("display_name", "summary"):
        value = manifest.get(key)
        if not isinstance(value, str) or not value.strip():
            bad("{} must be a non-empty string".format(key))
        elif any(
            not (
                char in "\t\n\r"
                or "\x20" <= char <= "\ud7ff"
                or "\ue000" <= char <= "\ufffd"
                or "\U00010000" <= char <= "\U0010ffff"
            )
            for char in value
        ):
            bad("{} contains a character that XML 1.0 cannot represent".format(key))
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        bad("files must be a non-empty list")
        return errors
    for number, entry in enumerate(files, 1):
        label = "files[{}]".format(number)
        if not isinstance(entry, dict):
            bad(label + " must be an object")
            continue
        if not relative_source_path(entry.get("path")):
            bad(label + ".path must stay inside the pack")
        digest = entry.get("sha256")
        if not isinstance(digest, str) or DIGEST_RE.fullmatch(digest) is None:
            bad(label + ".sha256 must be 64 lowercase hexadecimal characters")
        if not isinstance(entry.get("install_as"), str) or not entry["install_as"].strip():
            bad(label + ".install_as must be a non-empty string")
    return errors


def digest_violations(manifest, pack_dir):
    errors = []
    pack_dir = Path(pack_dir)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        return errors
    name = manifest.get("name", pack_dir.name)
    for entry in manifest["files"]:
        if not isinstance(entry, dict) or not relative_source_path(entry.get("path")):
            continue
        expected = entry.get("sha256")
        if not isinstance(expected, str) or DIGEST_RE.fullmatch(expected) is None:
            continue
        source = pack_dir / entry["path"]
        try:
            source.resolve().relative_to(pack_dir.resolve())
        except (ValueError, RuntimeError):
            actual = "<outside pack or symlink loop>"
        except OSError as exc:
            actual = "<unreadable: {}>".format(exc.strerror)
        else:
            try:
                actual = hashlib.sha256(source.read_bytes()).hexdigest()
            except FileNotFoundError:
                actual = "<missing>"
            except OSError as exc:
                actual = "<unreadable: {}>".format(exc.strerror)
        if actual != expected:
            errors.append(
                violation(
                    source,
                    "digest",
                    "pack {}: expected {}, actual {}".format(name, expected, actual),
                )
            )
    return errors


def read_existing_index(path):
    try:
        with Path(path).open(encoding="utf-8") as stream:
            value = json.load(stream)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, UnicodeError):
        print("warning: {}: ignoring unparseable previous index".format(path), file=sys.stderr)
        return None
    if not isinstance(value, dict) or not isinstance(value.get("packs"), list):
        print("warning: {}: ignoring invalid previous index shape".format(path), file=sys.stderr)
        return None
    if not all(isinstance(entry, dict) for entry in value["packs"]):
        raise BuildRefused("{}: previous packs must contain objects".format(path))
    return value


def version_tuple(version):
    return tuple(int(part) for part in version.split("."))


def utc_now():
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    try:
        now = (
            datetime.fromtimestamp(int(epoch), timezone.utc)
            if epoch is not None
            else datetime.now(timezone.utc)
        )
    except (ValueError, OverflowError, OSError) as exc:
        raise BuildRefused("channel/index.json: invalid SOURCE_DATE_EPOCH") from exc
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def payload(index):
    return {
        "schema": index.get("schema"),
        "base": index.get("base"),
        "packs": [
            {key: value for key, value in entry.items() if key != "released"}
            for entry in index["packs"]
        ],
    }


def build(root, out, base):
    old_index = read_existing_index(out / "index.json")
    previous = {}
    for entry in old_index["packs"] if old_index else []:
        name = entry.get("name")
        version = entry.get("version")
        ring = entry.get("ring", DEFAULT_RING)
        if not isinstance(name, str) or not isinstance(version, str) or ring not in RING_ORDER:
            raise BuildRefused(
                "{}: invalid previous pack name/version/ring".format(out / "index.json")
            )
        if VERSION_RE.fullmatch(version) is None or (name, ring) in previous:
            raise BuildRefused(
                "{}: pack {} on {}: invalid or duplicate previous version".format(
                    out / "index.json", name, ring
                )
            )
        previous[(name, ring)] = entry

    entries = []
    names = {}
    titles = {}
    manifest_paths = sorted((root / "packs").glob("*/pack.json")) + sorted(
        p for p in (root / "packs").glob("*/pack.*.json") if p.name != "pack.json"
    )
    for path in manifest_paths:
        manifest, raw, errors = read_manifest(path)
        if not errors:
            errors = manifest_violations(manifest, path)
        if not errors:
            errors = digest_violations(manifest, path.parent)
        if errors:
            raise BuildRefused(errors[0])
        name = manifest["name"]
        ring = manifest["ring"]
        # A ring-specific manifest must say the same ring its filename claims, or the
        # index would advertise a build on a ring nobody published it to.
        if path.name != "pack.json":
            declared = path.name[len("pack."):-len(".json")]
            if declared != ring:
                raise BuildRefused(
                    "{}: filename declares ring {} but the manifest says {}".format(
                        path, declared, ring
                    )
                )
        if name != path.parent.name:
            raise BuildRefused(
                "{}: pack name {} does not match its directory {}".format(
                    path, name, path.parent.name
                )
            )
        if (name, ring) in names:
            raise BuildRefused(
                "pack {} on ring {}: duplicate in {} and {}".format(
                    name, ring, names[(name, ring)], path
                )
            )
        names[(name, ring)] = path
        prior = previous.get((name, ring))
        if prior and version_tuple(manifest["version"]) < version_tuple(prior["version"]):
            raise BuildRefused(
                "{}: pack {} on {}: version regression {} -> {} (recorded in {})".format(
                    path, name, ring, prior["version"], manifest["version"], out / "index.json"
                )
            )
        entries.append(
            {
                "name": name,
                "version": manifest["version"],
                "ring": ring,
                "manifest": path.relative_to(root).as_posix(),
                "manifest_sha256": hashlib.sha256(raw).hexdigest(),
                "kind": manifest["kind"],
                "summary": manifest["summary"],
            }
        )
        titles[(name, ring)] = manifest["display_name"]
    entries.sort(key=lambda entry: (entry["name"], entry["ring"]))
    index = {"schema": "rapp-channel/1", "base": base, "packs": entries}
    unchanged = old_index is not None and payload(index) == payload(old_index)
    index["generated"] = (
        old_index["generated"]
        if unchanged and isinstance(old_index.get("generated"), str)
        else utc_now()
    )
    for entry in entries:
        prior = previous.get((entry["name"], entry["ring"]))
        entry["released"] = (
            prior["released"]
            if prior
            and prior["version"] == entry["version"]
            and isinstance(prior.get("released"), str)
            else index["generated"]
        )

    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<feed xmlns="http://www.w3.org/2005/Atom">',
        "  <title>rapp-packs channel</title>",
        '  <link rel="self" href={}/>'.format(quoteattr(base + "channel/feed.xml")),
        '  <link rel="alternate" href={}/>'.format(quoteattr(base)),
        "  <id>{}</id>".format(escape(base)),
        "  <updated>{}</updated>".format(escape(index["generated"])),
    ]
    # The stable second sort preserves ascending names when release times tie.
    for entry in sorted(
        entries, key=lambda item: (item["released"], item["name"], item["ring"]), reverse=True
    ):
        lines.extend(
            [
                "  <entry>",
                "    <title>{}</title>".format(
                    escape(
                        titles[(entry["name"], entry["ring"])]
                        + " "
                        + entry["version"]
                        + " ("
                        + entry["ring"]
                        + ")"
                    )
                ),
                "    <id>{}</id>".format(
                    escape(
                        base + "packs/" + entry["name"] + "/#"
                        + entry["ring"] + "-" + entry["version"]
                    )
                ),
                '    <link rel="alternate" href={}/>'.format(quoteattr(base + entry["manifest"])),
                "    <updated>{}</updated>".format(escape(entry["released"])),
                '    <summary type="text">{}</summary>'.format(escape(entry["summary"])),
                "    <category term={}/>".format(quoteattr(entry["kind"])),
                "    <category term={} label=\"ring\"/>".format(quoteattr(entry["ring"])),
                "  </entry>",
            ]
        )
    lines.append("</feed>")
    feed = "\n".join(lines) + "\n"
    try:
        ElementTree.fromstring(feed)
    except ElementTree.ParseError as exc:
        raise BuildRefused(
            "{}: feed contains invalid XML text or timestamps".format(out / "feed.xml")
        ) from exc
    return {
        "index.json": json.dumps(index, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        "feed.xml": feed,
    }, len(entries)


def check_outputs(out, outputs):
    identical = True
    for name, text in outputs.items():
        path = out / name
        try:
            existing = path.read_bytes()
        except FileNotFoundError:
            existing = b""
        if existing == text.encode("utf-8"):
            continue
        identical = False
        diff = "".join(
            difflib.unified_diff(
                existing.decode("utf-8", errors="replace").splitlines(keepends=True),
                text.splitlines(keepends=True),
                fromfile=str(path),
                tofile=str(path) + " (rebuilt)",
            )
        )
        sys.stdout.write(diff or "{}: bytes differ\n".format(path))
        if diff and not diff.endswith("\n"):
            sys.stdout.write("\n")
    return identical


def read_seed(path):
    """Load a 32-byte Ed25519 seed stored as 64 lowercase hex. Never printed."""
    raw = Path(path).read_text(encoding="utf-8").strip().lower()
    if DIGEST_RE.fullmatch(raw) is None:
        raise BuildRefused("{}: signing key must be 64 lowercase hex characters".format(path))
    return bytes.fromhex(raw)


def sign_index(index_text, seed):
    message = index_text.encode("utf-8")
    public = ed25519.public_key_from_seed(seed)
    signature = ed25519.sign(seed, message)
    obj = {
        "alg": "ed25519",
        "public_key": public.hex(),
        "schema": "rapp-sig/1",
        "signature": signature.hex(),
    }
    return json.dumps(obj, indent=2, sort_keys=True) + "\n", public.hex()


def verify_committed_signature(out):
    """Check channel/index.sig against channel/index.json. No private key needed.

    This proves the committed pair is internally consistent. It does NOT prove identity --
    that is the client's TOFU pin in ~/.brainstem/packs/channel.key (SPEC 9).
    """
    index_path, sig_path = out / "index.json", out / "index.sig"
    try:
        message = index_path.read_bytes()
    except OSError as exc:
        return "cannot read {}: {}".format(index_path, exc.strerror)
    try:
        signature = json.loads(sig_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ("{} is missing: an unsigned index is refused by every client "
                "(SPEC 9)".format(sig_path))
    except (json.JSONDecodeError, UnicodeError, OSError):
        return "{} is not readable JSON".format(sig_path)
    if not isinstance(signature, dict) or signature.get("schema") != "rapp-sig/1" \
            or signature.get("alg") != "ed25519":
        return "{} is not an rapp-sig/1 ed25519 signature".format(sig_path)
    public = str(signature.get("public_key", "")).lower()
    sig = str(signature.get("signature", "")).lower()
    if DIGEST_RE.fullmatch(public) is None or SIGNATURE_RE.fullmatch(sig) is None:
        return "{} has a malformed key or signature".format(sig_path)
    if not ed25519.verify(bytes.fromhex(public), message, bytes.fromhex(sig)):
        return "{} does not verify against {} (key {})".format(sig_path, index_path, public)
    return None


def keygen(path, root):
    """Create a channel signing key. Refuses to overwrite. Never prints the seed."""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    seed = os.urandom(32)
    try:
        handle = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise BuildRefused(
            "{}: a signing key already exists. Rotating it is a deliberate human act -- "
            "move the old key aside first.".format(path)
        )
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(seed.hex() + "\n")
    os.chmod(str(path), 0o600)
    public = ed25519.public_key_from_seed(seed).hex()
    pub_path = root / "channel" / "channel.pub"
    pub_path.parent.mkdir(parents=True, exist_ok=True)
    write_output(pub_path, public + "\n")
    print(public)
    print("private key written to {} (0600); it must NEVER be committed".format(path),
          file=sys.stderr)
    print("public key mirrored to {}".format(pub_path), file=sys.stderr)
    return 0


def write_output(path, text):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        os.replace(str(temporary), str(path))
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--key", type=Path, default=DEFAULT_KEY,
                        help="Ed25519 signing seed (64 hex). Default: %(default)s")
    parser.add_argument("--keygen", type=Path, metavar="PATH",
                        help="create a signing key at PATH, print its public key, exit")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    out = args.out.resolve() if args.out else root / "channel"
    base = args.base if args.base.endswith("/") else args.base + "/"
    try:
        if not root.is_dir():
            raise BuildRefused("{}: repository root is not a directory".format(root))
        if args.keygen is not None:
            return keygen(args.keygen, root)
        outputs, count = build(root, out, base)
        if args.check:
            if not check_outputs(out, outputs):
                return 1
            problem = verify_committed_signature(out)
            if problem:
                print("refused: " + problem, file=sys.stderr)
                return 1
            print("channel is up to date and signed ({} packs)".format(count))
        else:
            out.mkdir(parents=True, exist_ok=True)
            for name, text in outputs.items():
                write_output(out / name, text)
            key_path = Path(args.key).expanduser()
            if key_path.is_file():
                signature, public = sign_index(outputs["index.json"], read_seed(key_path))
                write_output(out / "index.sig", signature)
                write_output(out / "channel.pub", public + "\n")
                print("built {}, {} and {} ({} packs, signed by {})".format(
                    out / "index.json", out / "feed.xml", out / "index.sig", count, public))
            else:
                print(
                    "WARNING: no signing key at {}; {} was NOT written. Clients WILL refuse "
                    "to install from an unsigned index (SPEC 9).".format(key_path, out / "index.sig"),
                    file=sys.stderr,
                )
                print("built {} and {} ({} packs, UNSIGNED)".format(
                    out / "index.json", out / "feed.xml", count))
    except (BuildRefused, OSError) as exc:
        print("refused: " + str(exc).replace("\n", "\\n"), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
