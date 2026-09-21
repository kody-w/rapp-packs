# The pack + graft contract (v1)

Normative. A pack that violates any MUST is refused by the verifier and fails the
conformance suite.

## 1. pack.json

    {
      "schema": "rapp-pack/1",
      "name": "gpt-6-astra",                  // [a-z0-9-]{2,40}, unique in the channel
      "version": "1.0.0",                     // semver; strictly increasing per release
      "kind": "pack",                         // "pack" | "graft"
      "display_name": "GPT-6 Astra",
      "summary": "one line — what the brainstem can do that it could not before",
      "files": [
        {"path": "astra_agent.py", "sha256": "<64 hex>", "install_as": "astra_agent.py"}
      ],
      "requires": {"brainstem": ">=0.6.0", "python": ">=3.9"},
      "provides": ["Astra"],                  // agent names this pack adds
      "grafts": [],                           // graft ids, "graft" kind only
      "credentials": ["copilot-token"],       // host credentials READ (never copied/stored)
      "revert": "delete the installed files"  // one sentence, always true
    }

- Every installed file MUST match `*_agent.py` (the brainstem's loader glob) or be a module
  the pack's own agent imports from the same directory.
- A pack MUST NOT install, overwrite, or delete `basic_agent.py`.
- A pack MUST NOT write outside `AGENTS_PATH` and `~/.brainstem/packs/`.

## 2. channel/index.json

    {"schema": "rapp-channel/1", "generated": "<ISO8601>",
     "base": "https://kody-w.github.io/rapp-packs/",
     "packs": [{"name": "...", "version": "...", "manifest": "packs/<name>/pack.json",
                "manifest_sha256": "<64 hex>", "kind": "...", "summary": "..."}]}

Built by `tools/build_channel.py` in CI. Hand-editing it is a defect; the suite checks that
a rebuild is byte-identical to what is committed.

## 3. Install algorithm (what the pack manager MUST do)

1. Fetch `channel/index.json`. Fetch `pack.json`; verify its digest against the index.
   **Mismatch → refuse, write nothing, report which digest differed.**
2. For each file: fetch, verify `sha256` from the manifest, `ast.parse()` it. Any failure →
   refuse the whole pack (all-or-nothing; never a half-installed pack).
3. Write atomically into `AGENTS_PATH` (temp file in the same directory + `os.replace`).
4. Record in `~/.brainstem/packs/installed.json`: name, version, installed files + digests,
   source URL, UTC timestamp. That file lives OUTSIDE the grail tree so an upgrade or a
   repair (`rm -rf src` + reclone) cannot lose it.
5. Report what is live. `load_agents()` runs on every `/chat`, so a new pack is usable on the
   next message — no restart, and the manager MUST NOT restart the brainstem.

Update = the same, gated on `version` being strictly greater unless `force`.
Remove = delete exactly the files listed in `installed.json`, nothing else.

## 4. Graft contract

A graft is an ordinary agent file that ALSO patches the running brainstem at import.

    GRAFT_ID = "astra-model"
    def apply(main):        # main is sys.modules["__main__"] — the brainstem module
        ...                 # MUST be idempotent: applying twice == applying once
    def verify(main):       # return True only if the graft is actually in effect
        ...
    def revert(main):       # best-effort restore of what apply() replaced

MUSTs:

- **Fail closed and quiet.** Every graft applies inside `try/except`. On exception it records
  the traceback to `~/.brainstem/packs/grafts.log`, marks itself failed, and lets the
  brainstem run ungrafted. A graft MUST NEVER be able to stop the brainstem from booting.
- **Safe mode.** If `~/.brainstem/packs/SAFE-MODE` exists (or `BRAINSTEM_SAFE=1`), `apply()`
  MUST NOT run. The process is then indistinguishable from the pure grail.
- **Anchored.** A graft that wraps a function MUST check the thing it is wrapping still looks
  like what it expects (name present, callable, not already wrapped) and refuse otherwise —
  upstream drift makes a graft inert, never corrupt.
- **Idempotent + re-entrant.** `load_agents()` runs on EVERY `/chat`. A graft is re-imported
  and re-applied constantly; double-wrapping is a defect the suite tests for.
- **Reversible without a restart** where possible; where not, `revert()` says so plainly.
- Grafts MUST NOT edit `brainstem.py`, the installer, or anything in the grail tree. A pack
  that writes into the grail tree is refused by the verifier.

## 5. Conformance suite (tests/)

Runs against a REAL brainstem process on a scratch port with a scratch `AGENTS_PATH` — never
a mock, never the fixture the pack author wrote. It MUST prove, per pack:

1. install from the channel → the agent appears in `/health` `agents` and NOT in `quarantined`
2. the agent answers a real `/chat` turn end to end
3. a tampered byte (flip one) → install refused, nothing written
4. a stale index digest → refused
5. update `1.0.0 → 1.0.1` → new version live, `installed.json` correct
6. remove → gone from `/health`, no other file touched
7. **graft, double-load**: 50 consecutive `/chat` turns → still exactly one wrap
8. **graft, catastrophe**: a graft whose `apply()` raises → brainstem still boots, still
   answers, failure recorded in `grafts.log`
9. **safe mode**: with `SAFE-MODE` present → `/health` identical to the pure grail
10. **upgrade survival**: replace the grail tree with a fresh checkout → packs still installed,
    grafts re-apply on next boot

---

# v1.1 amendments — the Hermes lessons

Four corrections, each from a failure mode another ecosystem already paid for.

## 6. Generations and automatic rollback (the OTA lesson)

Every over-the-air update system that ships code to machines it cannot see learns the same
thing: a bad bundle that only fails at boot is indistinguishable from a bricked host. Manual
safe mode is not enough — the person who would flip it is exactly the person who cannot reach
the machine.

- The manager keeps **generations**, not "latest": the previous pack set stays on disk under
  `~/.brainstem/packs/generations/<n>/`. Revert is a file move, instant and offline.
- `~/.brainstem/packs/boot.json` records a **boot counter**, bumped when packs load and
  cleared once the brainstem answers a real request. Three consecutive bumps without a clear
  → the manager **automatically reverts to the last known good generation** and writes why to
  `grafts.log`. No human, no network.
- `installed.json` names the current generation; a rollback is itself recorded, never silent.

## 7. Gate on the host version — and re-gate after an upgrade

`requires.brainstem` is currently decorative. It MUST be enforced at install, and **re-checked
on every boot**: a grail upgrade can move the host out from under a pack that was installed
legitimately. A pack whose range no longer matches goes **inert with a reason** (listed in
`status`, not loaded), never left to explode against a changed seam. A graft that cannot find
its anchor is the same event and takes the same path.

## 8. Rings, not one channel (reuse the estate's word)

The estate already ships on `alpha / beta / canary / nightly`. The channel adopts that
vocabulary rather than inventing one: every pack version declares a `ring`, the manager is
pinned to a ring (default `beta`), and `index.json` carries all rings so one fetch answers
"what would I get on canary". Do not coin a new word for a thing the estate already names.

## 9. Sign the index, pin the key (the supply-chain lesson)

An auto-update channel that writes executable Python onto a machine IS the attack surface —
digests rooted in a URL only prove the bytes match what that URL said.

- `channel/index.json` is accompanied by `channel/index.sig` (detached signature).
- First install pins the channel's public key (TOFU) into `~/.brainstem/packs/channel.key`.
  A later index signed by a different key is **refused**, loudly, and never written — a key
  change is a human decision, not an auto-update.
- Unchanged and non-negotiable: fail closed on every mismatch, allowlist the channel host,
  refuse cross-host redirects, and never ship a credential inside a pack.

## 10. A pack is a capability, not a brainstem file drop (the membrane lesson)

Canon: *"How can we layer a functioning brainstem over anything else (Hermes, openclaw, open
human) without killing that organism."* A channel that can only feed a RAPP brainstem narrows
that on day one.

- `pack.json` declares `"hosts": ["brainstem"]` — and the format stays projectable: an agent
  file is already convertible to `SKILL.md` / openclaw / openrappter by `agentshim.py`
  (canonical copy in `rapp-tower/tools/`), with its RCI capsule carrying byte-exact fidelity
  both ways.
- Grafts are the exception and must say so: a graft patches a specific host's live module, so
  it declares its host explicitly and refuses to apply anywhere else.
