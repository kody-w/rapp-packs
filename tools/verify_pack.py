#!/usr/bin/env python3
"""Static pack conformance checks, not a sandbox or a proof of runtime safety."""

import argparse
import ast
import copy
import fnmatch
import importlib.util
import itertools
import json
from pathlib import Path, PureWindowsPath
import re
import sys


if __package__:
    from . import build_channel as channel
else:
    # Also support importlib-based test runners that do not add tools to sys.path.
    _spec = importlib.util.spec_from_file_location(
        "rapp_pack_channel", Path(__file__).with_name("build_channel.py")
    )
    if _spec is None or _spec.loader is None:
        raise ImportError("cannot load the adjacent build_channel.py")
    channel = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(channel)


GRAIL_MARKERS = (
    "brainstem.py", "rapp_brainstem", "/src/", "soul.md", "basic_agent.py",
    "index.html", ".copilot_token", ".copilot_session", ".brainstem_secret",
    ".brainstem_book.json", ".env", "local_storage.py", "start.sh", "start.ps1",
    "version",
)
SAFE_ROOTS = {"AGENTS_PATH", "agents_path", "agents_dir", "packs_dir"}
PATH_CONSTRUCTORS = {
    "Path", "pathlib.Path", "pathlib.PurePath", "pathlib.PurePosixPath",
    "pathlib.PureWindowsPath",
}
PATH_WRITES = {"write_text", "write_bytes", "unlink", "rmdir", "mkdir", "touch", "rename", "replace"}
OS_WRITES = {"remove", "unlink", "rmdir", "makedirs", "mkdir", "truncate", "chmod", "rename", "replace"}
CREDENTIAL_PATTERNS = (
    ("GitHub token prefix", re.compile(r"gh[pousr]_[A-Za-z0-9_-]*")),
    ("GitHub fine-grained token prefix", re.compile(r"github_pat_[A-Za-z0-9_-]*")),
    ("API key prefix", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
    ("Slack token prefix", re.compile(r"xox[abprs]-[A-Za-z0-9_-]*")),
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)
ASSIGNMENT_RE = re.compile(
    r"""(?P<name>[A-Za-z_]\w*)["']?\s*(?:(?::[^=\r\n]+)?=|:)\s*"""
    r"""(?:[rubf]{0,2})?(?P<quote>\"\"\"|'''|"|')(?P<value>.*?)(?P=quote)""",
    re.IGNORECASE | re.DOTALL,
)
# An endpoint URL is not a secret. GH_TOKEN_EXCHANGE = "https://.../v2/token" tripped the
# name rule on the word "token" in its PATH, which would force every pack that names an
# auth endpoint to either rename its constant or carry a waiver.
URLISH_RE = re.compile(r"^(https?|wss?)://", re.IGNORECASE)
SECRET_NAME_RE = re.compile(r"token|secret|password|passwd|api_?key|credential", re.IGNORECASE)
PLACEHOLDERS = ("xxx", "<", "your", "example", "replace", "changeme", "...")


def check_manifest(manifest, pack_dir, trees):
    path = pack_dir / "pack.json"
    errors = channel.manifest_violations(manifest, path)
    if not isinstance(manifest, dict):
        return errors
    if "requires" in manifest and not isinstance(manifest["requires"], dict):
        errors.append(channel.violation(path, "manifest-schema", "requires must be an object"))
    # SPEC 10: a pack is a capability, not a brainstem file drop. No hosts => refuse.
    hosts = manifest.get("hosts")
    if not isinstance(hosts, list) or not hosts or not all(
        isinstance(host, str) and host.strip() for host in hosts
    ):
        errors.append(channel.violation(
            path, "no-hosts", "pack.json must declare a non-empty hosts list (SPEC 10)"))
    elif manifest.get("kind") == "graft" and len(hosts) != 1:
        errors.append(channel.violation(
            path, "graft-multi-host",
            "a graft patches one host's live module and must declare exactly one host"))
    # SPEC 8: every pack version declares a ring.
    if manifest.get("ring") not in channel.RING_ORDER:
        errors.append(channel.violation(
            path, "no-ring", "ring must be one of " + ", ".join(channel.RING_ORDER)))
    for key in ("provides", "grafts", "credentials"):
        value = manifest.get(key)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            errors.append(channel.violation(path, "manifest-schema", key + " must be a list of strings"))
    if manifest.get("kind") != "graft" and manifest.get("grafts"):
        errors.append(channel.violation(path, "manifest-schema", "only grafts may declare non-empty grafts"))
    if not isinstance(manifest.get("revert"), str) or not manifest["revert"].strip():
        errors.append(channel.violation(path, "manifest-schema", "revert must be a non-empty string"))

    files = manifest.get("files")
    if not isinstance(files, list):
        return errors
    imports = set()
    for entry in files:
        if not isinstance(entry, dict):
            continue
        installed = entry.get("install_as")
        source = entry.get("path")
        if not isinstance(installed, str) or not channel.relative_source_path(source):
            continue
        tree = trees.get(pack_dir / source)
        # Entry points are what the host EXECUTES: an agent file the loader sweeps, and a
        # graft module the graft engine discovers. Both may legitimately import a module the
        # pack vendors, so both contribute to the set of justified imports. Collecting from
        # agents alone made every graft's helper look stray.
        role = entry.get("role")
        is_entry_point = fnmatch.fnmatchcase(installed, "*_agent.py") or role in ("agent", "graft")
        if not is_entry_point or tree is None:
            continue
        for node in ast.walk(tree):
            # A pack cannot rely on `import helper`: the host only puts <brainstem_dir>/agents
            # on sys.path, so a relocated AGENTS_PATH breaks plain imports. The working idiom
            # is importlib.util.spec_from_file_location with the sibling's FILENAME as a
            # literal. Count that literal as a reference, or every correct pack looks stray.
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.endswith(".py"):
                    imports.add(Path(node.value).stem)
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level <= 1:
                if node.module:
                    imports.add(node.module.split(".")[0])
                elif node.level == 1:
                    imports.update(alias.name for alias in node.names)
    installed_names = set()
    for entry in files:
        if not isinstance(entry, dict) or not isinstance(entry.get("install_as"), str):
            continue
        installed = entry["install_as"]
        if (
            not installed.strip() or installed == "." or ".." in installed
            or "/" in installed or "\\" in installed
            or any(ord(char) < 32 or ord(char) == 127 or "\ud800" <= char <= "\udfff" for char in installed)
            or PureWindowsPath(installed).drive or Path(installed).is_absolute()
        ):
            errors.append(channel.violation(path, "manifest-schema", "install_as must be a bare filename"))
        if installed.casefold() == "basic_agent.py":
            errors.append(channel.violation(path, "grail-basic-agent", "basic_agent.py must never be installed"))
        if installed.casefold() in installed_names:
            errors.append(channel.violation(path, "manifest-schema", "duplicate install_as filename"))
        installed_names.add(installed.casefold())
        role = entry.get("role") if isinstance(entry, dict) else None
        # A graft module is discovered by the graft engine at runtime, so it is neither an
        # agent nor statically imported by one. It is legitimate ONLY when the pack declares
        # kind "graft" AND names the file's role, so "stray" stays a real finding elsewhere.
        if role == "graft" and manifest.get("kind") == "graft":
            continue
        if not fnmatch.fnmatchcase(installed, "*_agent.py") and Path(installed).stem not in imports:
            errors.append(
                channel.violation(path, "stray-file", "installed file is neither an agent nor its imported module")
            )
    return errors


def check_digests(manifest, pack_dir):
    return channel.digest_violations(manifest, pack_dir)


def check_channel_history(manifest, pack_dir):
    """Enforce channel-wide rules when a pack is in the standard packs/ layout."""
    if not isinstance(manifest, dict) or pack_dir.parent.name != "packs":
        return []
    name, version = manifest.get("name"), manifest.get("version")
    if (
        not isinstance(name, str) or channel.NAME_RE.fullmatch(name) is None
        or not isinstance(version, str) or channel.VERSION_RE.fullmatch(version) is None
    ):
        return []
    errors = []
    path = pack_dir / "pack.json"
    for sibling in sorted(pack_dir.parent.glob("*/pack.json")):
        if sibling == path:
            continue
        other, _, read_errors = channel.read_manifest(sibling)
        if not read_errors and isinstance(other, dict) and other.get("name") == name:
            errors.append(channel.violation(path, "duplicate-name", "duplicate name also declared in " + str(sibling)))
    index_path = pack_dir.parent.parent / "channel" / "index.json"
    try:
        with index_path.open(encoding="utf-8") as stream:
            previous = json.load(stream)
    except FileNotFoundError:
        return errors
    except (json.JSONDecodeError, UnicodeError):
        return errors + [channel.violation(index_path, "channel-history", "previous index is not valid JSON")]
    except OSError as exc:
        return errors + [channel.violation(index_path, "channel-history", "cannot read previous index: " + exc.strerror)]
    if not isinstance(previous, dict) or not isinstance(previous.get("packs"), list):
        return errors + [channel.violation(index_path, "channel-history", "previous index must contain a packs list")]
    matches = [entry for entry in previous["packs"] if isinstance(entry, dict) and entry.get("name") == name]
    if len(matches) > 1:
        errors.append(channel.violation(index_path, "duplicate-name", "previous index contains duplicate pack names"))
    for entry in matches:
        prior = entry.get("version")
        if not isinstance(prior, str) or channel.VERSION_RE.fullmatch(prior) is None:
            errors.append(channel.violation(index_path, "channel-history", "invalid previous pack version"))
        elif channel.version_tuple(version) < channel.version_tuple(prior):
            errors.append(
                channel.violation(path, "version-regression", "{} -> {} (recorded in {})".format(prior, version, index_path))
            )
    return errors


def check_syntax(sources, trees=None):
    errors = []
    if trees is None:
        trees = {}
    for path, raw in sources.items():
        if path.suffix != ".py":
            continue
        try:
            trees[path] = ast.parse(raw, filename=str(path))
        except SyntaxError as exc:
            # SyntaxError's source excerpt can itself contain a credential.
            errors.append(channel.violation(path, "syntax", "Python source does not parse", exc.lineno))
        except (ValueError, UnicodeError):
            errors.append(channel.violation(path, "syntax", "invalid Python source encoding"))
    return errors


def symbol(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        root = symbol(node.value)
        return root + "." + node.attr if root else node.attr
    return ""


def ast_context(tree):
    aliases = {}
    bindings = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                aliases[alias.asname or alias.name] = node.module + "." + alias.name
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if node.value is not None:
                for target in targets:
                    name = symbol(target)
                    if name:
                        bindings.setdefault(name, []).append(node.value)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bindings.setdefault(node.name, []).append(node)
    return aliases, bindings


def call_name(node, aliases):
    name = symbol(node)
    head, dot, rest = name.partition(".")
    return aliases.get(head, head) + dot + rest


def join_parts(parts):
    result = ""
    for part in parts:
        if part.startswith("/") or PureWindowsPath(part).is_absolute():
            result = part
        else:
            result = result.rstrip("/") + "/" + part if result else part
    return result or "."


def expanded_arguments(arguments):
    result = []
    for argument_node in arguments:
        if isinstance(argument_node, ast.Starred) and isinstance(argument_node.value, (ast.Tuple, ast.List)):
            result.extend(expanded_arguments(argument_node.value.elts))
        else:
            result.append(argument_node)
    return result


def helper_returns(call, aliases, bindings, seen):
    """Inline only expression-returning path helpers, never execute pack code."""
    name = call_name(call.func, aliases)
    if "call:" + name in seen:
        return []
    results = []
    for definition in bindings.get(name, []):
        if not isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef)) or definition.decorator_list:
            continue
        body = definition.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            body = body[1:]
        if len(body) != 1 or not isinstance(body[0], ast.Return) or body[0].value is None:
            continue
        parameters = definition.args.posonlyargs + definition.args.args
        arguments = expanded_arguments(call.args)
        replacements = {
            parameter.arg: value
            for parameter, value in zip(parameters[len(parameters) - len(definition.args.defaults):], definition.args.defaults)
        }
        replacements.update(
            (parameter.arg, value) for parameter, value in zip(parameters, arguments)
        )
        replacements.update(
            (parameter.arg, value)
            for parameter, value in zip(definition.args.kwonlyargs, definition.args.kw_defaults)
            if value is not None
        )
        if any(keyword.arg is None for keyword in call.keywords):
            continue
        replacements.update((keyword.arg, keyword.value) for keyword in call.keywords)
        if any(parameter.arg not in replacements for parameter in parameters + definition.args.kwonlyargs):
            continue
        if definition.args.vararg:
            replacements[definition.args.vararg.arg] = ast.Tuple(
                elts=arguments[len(parameters):], ctx=ast.Load()
            )
        elif len(arguments) > len(parameters):
            continue

        class Substitute(ast.NodeTransformer):
            def visit_Name(self, node):
                return replacements.get(node.id, node)

        results.append(Substitute().visit(copy.deepcopy(body[0].value)))
    return results


def resolve_paths(node, aliases, bindings, seen=frozenset()):
    """Retain literal path pieces; unknown components are represented by '*'."""
    if node is None:
        return {"*"}
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    name = symbol(node)
    if isinstance(node, (ast.Name, ast.Attribute)):
        if name.split(".")[-1] in SAFE_ROOTS:
            return {"*"}
        if name in bindings and name not in seen:
            return set().union(
                *(resolve_paths(value, aliases, bindings, seen | {name}) for value in bindings[name])
            )
        return {"*"}
    if isinstance(node, ast.JoinedStr):
        pieces = [
            resolve_paths(part.value if isinstance(part, ast.FormattedValue) else part, aliases, bindings, seen)
            for part in node.values
        ]
        return {"".join(parts) for parts in itertools.product(*pieces)}
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Div)):
        left = resolve_paths(node.left, aliases, bindings, seen)
        right = resolve_paths(node.right, aliases, bindings, seen)
        return {
            first + second if isinstance(node.op, ast.Add) else join_parts((first, second))
            for first in left for second in right
        }
    if isinstance(node, ast.Call):
        func = call_name(node.func, aliases)
        if func.split(".")[-1] in SAFE_ROOTS:
            return {"*"}
        if func in PATH_CONSTRUCTORS or func in ("os.path.join", "posixpath.join", "ntpath.join"):
            pieces = [resolve_paths(arg, aliases, bindings, seen) for arg in expanded_arguments(node.args)]
            return {join_parts(parts) for parts in itertools.product(*pieces)}
        if func in ("Path.home", "pathlib.Path.home"):
            return {"~"}
        if func == "os.path.expanduser" and node.args:
            return resolve_paths(node.args[0], aliases, bindings, seen)
        if isinstance(node.func, ast.Attribute):
            if node.func.attr in ("expanduser", "resolve", "absolute"):
                return resolve_paths(node.func.value, aliases, bindings, seen)
            if node.func.attr == "joinpath":
                pieces = [
                    resolve_paths(arg, aliases, bindings, seen)
                    for arg in [node.func.value] + node.args
                ]
                return {join_parts(parts) for parts in itertools.product(*pieces)}
        returns = helper_returns(node, aliases, bindings, seen)
        if returns:
            return set().union(*(
                resolve_paths(value, aliases, bindings, seen | {"call:" + func}) for value in returns
            ))
    return {"*"}


def safe_anchor(node, aliases, bindings, seen=frozenset()):
    if node is None:
        return False
    name = symbol(node)
    if isinstance(node, (ast.Name, ast.Attribute)):
        if name.split(".")[-1] in SAFE_ROOTS:
            return True
        return name not in seen and name in bindings and all(
            safe_anchor(value, aliases, bindings, seen | {name}) for value in bindings[name]
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Div, ast.Add)):
        return safe_anchor(node.left, aliases, bindings, seen)
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        return isinstance(first, ast.FormattedValue) and safe_anchor(first.value, aliases, bindings, seen)
    if isinstance(node, ast.Call):
        func = call_name(node.func, aliases)
        if func.split(".")[-1] in SAFE_ROOTS:
            return True
        # A CALL to a safe root (packs_dir(), agents_path()) is the anchor this rule
        # asks for, exactly as its own message says. Without this, a pack that resolves
        # its state root through a function -- the correct shape -- could never pass.
        if func.split(".")[-1] in SAFE_ROOTS:
            return True
        if func in PATH_CONSTRUCTORS or func in ("os.path.join", "posixpath.join", "ntpath.join"):
            return bool(node.args) and safe_anchor(node.args[0], aliases, bindings, seen)
        if isinstance(node.func, ast.Attribute) and node.func.attr in ("joinpath", "expanduser", "resolve", "absolute"):
            return safe_anchor(node.func.value, aliases, bindings, seen)
        returns = helper_returns(node, aliases, bindings, seen)
        if returns:
            return all(
                safe_anchor(value, aliases, bindings, seen | {"call:" + func}) for value in returns
            )
    return False


def path_object(node, aliases, bindings, seen=frozenset()):
    name = symbol(node)
    if isinstance(node, ast.Call):
        if call_name(node.func, aliases) in PATH_CONSTRUCTORS:
            return True
        return isinstance(node.func, ast.Attribute) and (
            call_name(node.func, aliases) in ("Path.home", "pathlib.Path.home", "Path.cwd", "pathlib.Path.cwd")
            or node.func.attr in ("joinpath", "expanduser", "resolve", "absolute")
            and path_object(node.func.value, aliases, bindings, seen)
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return path_object(node.left, aliases, bindings, seen)
    return name not in seen and name in bindings and any(
        path_object(value, aliases, bindings, seen | {name}) for value in bindings[name]
    )


def argument(call, index, *keywords):
    for keyword in call.keywords:
        if keyword.arg in keywords:
            return keyword.value
    return call.args[index] if len(call.args) > index else None


def write_targets(call, aliases, bindings):
    name = call_name(call.func, aliases)
    if name in ("open", "builtins.open", "io.open"):
        mode = argument(call, 1, "mode")
        if mode is None:
            return []
        modes = resolve_paths(mode, aliases, bindings)
        return [argument(call, 0, "file")] if any(
            "*" in item or any(flag in item for flag in "wax+") for item in modes
        ) else []
    if name.startswith("os.") and name[3:] in OS_WRITES:
        if name in ("os.rename", "os.replace"):
            return [argument(call, 0, "src"), argument(call, 1, "dst")]
        return [argument(call, 0, "path", "name")]
    if name.startswith("shutil.copy"):
        return [argument(call, 1, "dst", "fdst")]
    if name == "shutil.move":
        return [argument(call, 0, "src"), argument(call, 1, "dst")]
    if name == "shutil.rmtree":
        return [argument(call, 0, "path")]
    if isinstance(call.func, ast.Attribute):
        receiver = call.func.value
        method = call.func.attr
        if method == "open" and path_object(receiver, aliases, bindings):
            mode = argument(call, 0, "mode")
            modes = resolve_paths(mode, aliases, bindings) if mode is not None else {"r"}
            return [receiver] if any(
                "*" in item or any(flag in item for flag in "wax+") for item in modes
            ) else []
        if method in PATH_WRITES and (method != "replace" or path_object(receiver, aliases, bindings)):
            targets = [receiver]
            if method in ("rename", "replace"):
                targets.append(argument(call, 0, "target"))
            return targets
    return []


def allowed_state_path(value):
    value = value.replace("\\", "/")
    state = str(Path.home() / ".brainstem" / "packs").replace("\\", "/")
    return any(value == root or value.startswith(root + "/") for root in ("~/.brainstem/packs", state))


def check_writes(trees):
    errors = []
    for path, tree in trees.items():
        aliases, bindings = ast_context(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = call_name(node.func, aliases)
            for target in write_targets(node, aliases, bindings):
                for value in resolve_paths(target, aliases, bindings):
                    normalized = value.replace("\\", "/")
                    marker = any(item in normalized.casefold() for item in GRAIL_MARKERS)
                    absolute = normalized.startswith(("/", "~")) or PureWindowsPath(value).is_absolute()
                    if marker or ".." in normalized or absolute and not allowed_state_path(normalized):
                        errors.append(
                            channel.violation(
                                path, "grail-write", "write/delete target is protected or outside allowed roots", node.lineno
                            )
                        )
                    if (
                        name in ("shutil.rmtree", "os.remove")
                        and "*" in normalized
                        and not allowed_state_path(normalized)
                        and not safe_anchor(target, aliases, bindings)
                    ):
                        errors.append(
                            channel.violation(
                                path, "grail-write-opaque",
                                "deletion has an unresolvable root; anchor it to AGENTS_PATH or packs_dir",
                                node.lineno,
                            )
                        )
    return list(dict.fromkeys(errors))


def check_warnings(trees):
    warnings = []
    message = "the static gate cannot prove where such a call writes"
    for path, tree in trees.items():
        aliases, _ = ast_context(tree)
        for node in ast.walk(tree):
            rule = None
            if isinstance(node, ast.Import) and any(alias.name.split(".")[0] == "ctypes" for alias in node.names):
                rule = "warn-ctypes"
            elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "ctypes":
                rule = "warn-ctypes"
            elif isinstance(node, ast.Call):
                name = call_name(node.func, aliases)
                if name == "os.system" or name.startswith("subprocess."):
                    rule = "warn-subprocess"
                elif name.startswith("ctypes."):
                    rule = "warn-ctypes"
                elif name in ("eval", "exec", "compile", "builtins.eval", "builtins.exec", "builtins.compile"):
                    value = argument(node, 0, "source", "object")
                    if not isinstance(value, ast.Constant) or not isinstance(value.value, (str, bytes)):
                        rule = "warn-dynamic-exec"
            if rule:
                warnings.append(channel.violation(path, rule, message, node.lineno))
    return list(dict.fromkeys(warnings))


def credential_findings(sources, trees=None):
    findings = []
    for path, raw in sources.items():
        text = raw.decode("utf-8", errors="replace")
        for label, pattern in CREDENTIAL_PATTERNS:
            for match in pattern.finditer(text):
                findings.append(
                    (path, text.count("\n", 0, match.start()) + 1, label, match.group(0))
                )
        for match in ASSIGNMENT_RE.finditer(text):
            value = match.group("value")
            if (
                SECRET_NAME_RE.search(match.group("name"))
                and len(value) >= 20
                and not URLISH_RE.match(value.strip().strip("\"'"))
                and not any(part in value.casefold() for part in PLACEHOLDERS)
            ):
                findings.append(
                    (path, text.count("\n", 0, match.start()) + 1, "credential assignment", value)
                )
        tree = trees.get(path) if trees is not None else None
        if trees is None and path.suffix == ".py":
            try:
                tree = ast.parse(raw)
            except (SyntaxError, ValueError, UnicodeError):
                # check_syntax reports these; raw-text credential checks still apply.
                tree = None
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                continue
            if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
                continue
            value = node.value.value
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if (
                len(value) >= 20 and not URLISH_RE.match(value.strip())
                and not any(part in value.casefold() for part in PLACEHOLDERS)
                and any(SECRET_NAME_RE.search(symbol(target)) for target in targets)
            ):
                findings.append((path, node.lineno, "credential assignment", value))
    return findings


def check_credentials(sources):
    findings = credential_findings(sources)
    return list(dict.fromkeys(
        redact_diagnostic(
            channel.violation(path, "credential", label + " matched (value withheld)", line), findings
        )
        for path, line, label, _ in findings
    ))


def redact_diagnostic(text, findings):
    # A secret can occur in a manifest name or a diagnostic filename, not only source text.
    for value in sorted({item[3] for item in findings}, key=len, reverse=True):
        text = text.replace(value, "<value withheld>")
    for _, pattern in CREDENTIAL_PATTERNS:
        text = pattern.sub("<value withheld>", text)
    return text.replace("\r", "\\r").replace("\n", "\\n")


def read_sources(pack_dir):
    sources = {}
    errors = []
    if not pack_dir.is_dir():
        return sources, [channel.violation(pack_dir, "file-read", "pack directory does not exist")]
    for path in sorted(pack_dir.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            continue
        try:
            path.resolve().relative_to(pack_dir.resolve())
        except (ValueError, RuntimeError):
            errors.append(channel.violation(path, "file-read", "file escapes pack or contains a symlink loop"))
            continue
        except OSError as exc:
            errors.append(channel.violation(path, "file-read", "cannot resolve file: " + exc.strerror))
            continue
        if path.is_dir():
            errors.append(channel.violation(path, "file-read", "symlinked directories cannot be statically scanned"))
            continue
        try:
            sources[path] = path.read_bytes()
        except OSError as exc:
            errors.append(channel.violation(path, "file-read", "cannot read file: " + exc.strerror))
    return sources, errors


def verify_pack_report(pack_dir):
    pack_dir = Path(pack_dir).resolve()
    manifest, _, manifest_errors = channel.read_manifest(pack_dir / "pack.json")
    errors = list(manifest_errors)
    sources, read_errors = read_sources(pack_dir)
    errors.extend(read_errors)
    trees = {}
    errors.extend(check_syntax(sources, trees))
    if not manifest_errors:
        errors.extend(check_manifest(manifest, pack_dir, trees))
        errors.extend(check_digests(manifest, pack_dir))
        errors.extend(check_channel_history(manifest, pack_dir))
    errors.extend(check_writes(trees))
    findings = credential_findings(sources, trees)
    errors.extend(
        channel.violation(path, "credential", label + " matched (value withheld)", line)
        for path, line, label, _ in findings
    )
    name = manifest.get("name") if isinstance(manifest, dict) else None
    if not isinstance(name, str) or channel.NAME_RE.fullmatch(name) is None:
        name = pack_dir.name
    return (
        redact_diagnostic(name, findings),
        not errors,
        list(dict.fromkeys(redact_diagnostic(error, findings) for error in errors)),
        [redact_diagnostic(warning, findings) for warning in check_warnings(trees)],
    )


def verify_pack(pack_dir):
    """Return (ok, violations, warnings); diagnostics never include source values."""
    _, ok, errors, warnings = verify_pack_report(pack_dir)
    return ok, errors, warnings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=channel.DEFAULT_ROOT)
    parser.add_argument("packdirs", nargs="*", type=Path, metavar="PACKDIR")
    args = parser.parse_args(argv)
    if not args.root.is_dir():
        print("FAIL {}".format(args.root))
        print("  " + channel.violation(args.root, "file-read", "repository root is not a directory"))
        return 1
    packs = args.packdirs or sorted(path for path in (args.root / "packs").glob("*") if path.is_dir())
    failed = False
    for pack_dir in packs:
        name, ok, errors, warnings = verify_pack_report(pack_dir)
        print("{} {}".format("PASS" if ok else "FAIL", name))
        for error in errors:
            print("  " + error)
        for warning in warnings:
            print("  WARNING " + warning)
        failed = failed or not ok
    if not packs:
        print("No packs found under {}".format(args.root / "packs"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
