# W1 — the channel, and the thing that installs from it

What I built, what I proved on a real brainstem, and what I am not sure about.

Everything below was run on 2026-09-20 against **a real `brainstem.py` process**, never a
mock and never the muscle's own fixtures. Output is pasted verbatim.

---

## 1. What was built

| path | what it is |
|---|---|
| `packs/pack-manager/pack_agent.py` | the bootstrap agent — `class PackAgent(BasicAgent)`, `self.name = "Packs"`, stdlib only, Python 3.9+ |
| `packs/pack-manager/pack.json` | its manifest (`rapp-pack/1`, ring `beta`, `hosts: ["brainstem"]`) |
| `tools/build_channel.py` | scans `packs/*/pack.json` + `packs/*/pack.<ring>.json`, verifies every digest, emits + signs `channel/index.json`, `channel/feed.xml`, `channel/index.sig` |
| `tools/verify_pack.py` | the gate a pack must pass before it may be published |
| `tools/ed25519_pure.py` | RFC 8032 Ed25519 in stdlib — the one primitive the whole integrity story rests on |
| `bootstrap.sh` | the public one-liner |
| `.github/workflows/channel.yml` | CI: crypto self-test, pack verify, rebuild-and-compare, Pages publish |
| `channel/` | the built output, including the committed public key |

**Actions on the agent:** `list`, `install`, `update`, `remove`, `pin`, `unpin`, `status`,
`self_update`, plus `ring`, `revert` and `generations` for SPEC 6/8.

### Division of labour

GPT-6 Astra (round 1) built the first cut of `build_channel.py`, `verify_pack.py`,
`bootstrap.sh` and `channel.yml`. Its run died mid-investigation without emitting a
`Resume:` line, and the coordinator flagged that the muscle was contended (eight concurrent
jobs), so I applied the SPEC §§6-10 amendments — rings, `hosts`, signing, TOFU, the
`installed.json` shape — myself rather than wait. `pack_agent.py` was mine from the start:
it hinges on host-integration details that only came out of reading `brainstem.py`.

---

## 2. Design decisions that were judgement calls

**The dirty-boot counter counts brainstem *boots*, not processes.** `load_agents()`
re-imports every agent file on **every** `/chat`, so a module-level counter would fake a
boot storm. The token is stashed on the live `__main__` module (`_rapp_packs_boot_token`),
which is stable for exactly one brainstem process, and `is_brainstem_host()` gates the whole
mechanism so a CLI run or a test import is not a boot. See the defect in §5.

**"Answered a real request" = reaching `system_context()`.** That is the only per-request
hook an agent gets. Reaching it proves `load_agents()` finished for every pack (no hang, no
process-killing import) and the brainstem is inside a real `/chat`. It deliberately does
*not* wait for the model to answer — rolling packs back because Copilot is down would be a
worse bug than the one being defended against.

**Ring stability order is `beta > canary > alpha > nightly`.** A release stabilises
nightly → alpha → canary → beta, so a pinned ring falls back only toward *more* stable
rings, never less. A pack with no build on your ring gives you the more stable build.

**Ed25519 is implemented, not imported.** The manager must be single-file stdlib inside a
brainstem with no third-party packages. Verified two ways (§4.11). The build side imports
`tools/ed25519_pure.py`; `pack_agent.py` and `bootstrap.sh` each carry their own copy
because both must stand alone. CI self-tests the shared copy against RFC 8032 vector 1.

**`bootstrap.sh` embeds its verifier rather than fetching one.** A signature check that
downloads its own verifier is not a signature check. This costs ~60 duplicated lines and
removes a hand-maintained digest that would otherwise have to be bumped by hand — my order
to Astra proposed the fetch-and-pin-a-literal route; I rejected it on review.

**`released` is an addition to the SPEC §2 index entry shape.** Without a per-entry release
timestamp the Atom feed cannot be both "newest first" and byte-deterministic. `generated` is
reused verbatim when the payload is unchanged, and each entry's `released` is reused when its
`(name, ring, version)` is unchanged. Flagged in §6.

---

## 3. Test rig, and proof the live brainstem was never touched

The launch command in my orders does **not** isolate state: `brainstem.py` derives
`_BASE_DIR` from its own `__file__`, so a second process started from the grail shares the
live install's `.brainstem_book.json` — whose 30-second autosave thread would overwrite it —
and can fall back to in-tree state paths. I found this while reading `brainstem.py` and
switched to a copied tree before ever launching; the coordinator independently flagged the
same thing later.

    .scratch/grail/     copy of brainstem.py, local_storage.py, soul.md, VERSION, index.html
    .scratch/agents/    AGENTS_PATH  (basic_agent.py + whatever the channel installs)
    .scratch/bhome/     fake $HOME, so ~/.brainstem/packs is a scratch dir
    PORT=7101, GITHUB_TOKEN passed in the child's environment only — never copied to disk

**After the entire run:**

    === live brainstem :7071 ===
    model: gpt-5.4 | agents: 11 | version: 0.6.16 | quarantined: []

    === grail tree untouched (state mtimes all predate this session, which began 20:32) ===
    -rw-------  20  Sep 13 18:33  .../rapp_brainstem/.brainstem_model
    -rw-------  114 Aug 29 13:24  .../rapp_brainstem/.copilot_token
    -rw-------  7   Aug 28 15:56  .../rapp_brainstem/VERSION
    grail agents dir pack files: 0

    === real ~/.brainstem/packs ===
    -rw-------  65  Sep 20 21:05  channel-signing.key

The only thing written outside the worktree is the channel signing key, which I was asked to
create. One stray `~/.brainstem/packs/boot.json` was created early (before I passed
`RAPP_PACKS_STATE_DIR` to the scratch brainstem) and was removed; the directory now holds
only the key.

---

## 4. The proofs

Unless noted, the running manager is byte-identical to the committed
`packs/pack-manager/pack_agent.py`:

    0582c52c81335b419a69d6b6cbbb02180109d8ffeed387a3f86470a1ffd44c4e  .scratch/agents/pack_agent.py
    0582c52c81335b419a69d6b6cbbb02180109d8ffeed387a3f86470a1ffd44c4e  packs/pack-manager/pack_agent.py

### 4.1 `bootstrap.sh` installs into a running brainstem, which then serves it

    === /health BEFORE bootstrap ===
    agents: [] quarantined: []

    === bootstrap.sh into the RUNNING brainstem's agents dir ===
    channel index signed by f19907489aae4b6ad0be2a79a3e7446bd42268b3012163a2975471908103c817
    installed pack_agent.py -> .../.scratch/agents/pack_agent.py
    ring beta; channel key pinned at .../.scratch/bhome/.brainstem/packs/channel.key
    The brainstem reloads agents on every /chat — Packs is live on your next message. No restart needed.
    bootstrap exit: 0

    === /health AFTER (no restart, pid unchanged) ===
    agents: ['Packs'] quarantined: []
    Python  15698 kodywildfeuer  4u  IPv4  TCP 127.0.0.1:7101 (LISTEN)

### 4.2 The manager, over a real `POST /chat`, installs a second pack — live on the next message

    === the bootstrapped manager answers a real /chat and installs a second pack ===
    [Packs] Installed hello-pack 1.0.0 (ring beta, generation 2).
      channel signature : verified-pinned
      manifest sha256   : 785660720f6a…
      files             : hello_agent.py (sha256:b2094b6cccff…, new)
      into              : .../.scratch/agents
      provides          : HelloPack
      revert            : delete hello_agent.py from the agents directory
    load_agents() runs on every /chat, so this is live on your next message. No restart needed.

    === /health ===
    agents: ['HelloPack', 'Packs'] quarantined: []

    === the new pack answers ===
    [HelloPack] {"status": "success", "marker": "HELLO-PACK-INSTALLED-FROM-CHANNEL", "pack_version": "1.0.0", "echo": "e2e"}

It appears in `/health` `agents`, it is **not** in `quarantined`, and it answers a real turn.

### 4.3 One flipped byte on the wire refuses the install

The index is already built and signed; one byte of the **served** `hello_agent.py` is flipped.

    refused: FILE digest mismatch for hello-pack/hello_agent.py
      manifest says : b2094b6cccffd26255edf854c4d563cdf9fba425f4c19c7e29fa25cc872a1024
      bytes are     : 359da661bde36c189b66ab4f3e582b7021e40f1646718ae3e98fe3df405280a8
    The whole pack was refused; nothing was written.

### 4.4 A stale index digest is refused

`pack.json` edited after the index was signed. The signature still verifies — the manifest
digest is what catches it, which is the layering working as designed.

    refused: MANIFEST digest mismatch for hello-pack
      index says : 785660720f6ae7198be166c156f1188ce2cd11ff3154a21c423fd2a55d431201
      bytes are  : 59c899d0d305ce270660453fd345a505ee726e2fb0e21d84f9c40a73444159ba
    Nothing was written.

### 4.5 An index signed by a different key is refused (TOFU, SPEC 9)

    REFUSED: the channel index is signed by a DIFFERENT key.
      pinned here : f19907489aae4b6ad0be2a79a3e7446bd42268b3012163a2975471908103c817
      presented   : c614c85e111051821b4c386dcad7d23c192965875ac4e374fe3fdeaa9d359931
    A key change is a human decision, not an auto-update. Nothing was written. If this
    rotation is real, delete .../channel.key deliberately and re-pin.

A tampered index body under the *right* key is refused too:

    refused: channel/index.sig does not verify against channel/index.json
             (key f19907489aae4b6ad0be2a79a3e7446bd42268b3012163a2975471908103c817). Nothing was written.

An unsigned channel is refused:

    refused: the channel index is not signed (channel/index.sig could not be fetched).
             SPEC 9 requires a signed index; nothing was written.

### 4.6 Every refusal above wrote nothing

    agents dir digests before: 701488bc00d5… b2094b6cccff… fe101ca94d8e…
    agents dir digests after : 701488bc00d5… b2094b6cccff… fe101ca94d8e…
    UNCHANGED

### 4.7 `1.0.0 → 1.0.1`, and a pin that blocks it

    hello-pack is pinned at 1.0.0 — action=update will skip it until you unpin (or pass force=true).
    Update (ring beta, signature verified-pinned)  -> nothing changed
      hello-pack           pinned at 1.0.0 — skipped (force=true overrides)

    hello-pack is unpinned and will follow the channel again.
    Update (ring beta, signature verified-pinned)  -> generation 3
      hello-pack           1.0.0 -> 1.0.1

The updated pack answered on the next message with no restart:

    [HelloPack] {"status": "success", "marker": "HELLO-PACK-INSTALLED-FROM-CHANNEL", "pack_version": "1.0.1", "echo": "after-update"}

### 4.8 `remove` deletes exactly the installed files

A bystander file was placed in `AGENTS_PATH` first; digests taken before and after.

    before:  basic_agent.py 701488bc00d5 | bystander_agent.py.txt e3b0c44298fc
             hello_agent.py aeea389f83ac | pack_agent.py 3d7518220bc3
    Removed hello-pack 2.0.0 (generation 9).
      deleted : hello_agent.py
      nothing else in .../.scratch/agents was touched.
    after:   basic_agent.py 701488bc00d5 | bystander_agent.py.txt e3b0c44298fc
             pack_agent.py 3d7518220bc3

### 4.9 `self_update` — the path every future fix travels

A marker string was added to the channel's copy so the swap is visible.

    marker in the installed manager before self_update: 0
    [Packs] Packs updated itself: 1.0.0 -> 1.0.1 (ring beta, generation 4).
      channel signature : verified-pinned
      files             : pack_agent.py (sha256:6e425adfd234…)
      previous set kept : .../packs/generations/3
    load_agents() runs on every /chat, so this is live on your next message. No restart needed.
    marker after self_update: 1
    brainstem pid still: Python 15698 ... TCP 127.0.0.1:7101 (LISTEN)

    === the self-updated manager answers the next message ===
      generation     : 4 (on disk: 1, 2, 3, 4)
      installed      : 2
        hello-pack           1.0.1    beta
        pack-manager         1.0.1    beta

Same process, new manager, no restart. `self_update` additionally refuses a replacement that
defines no class with a `perform()` method — the one pack whose breakage is hardest to
recover from should not be replaceable by something the brainstem cannot load.

### 4.10 Safe mode blocks every write and says why

    REFUSED: safe mode is on because .../packs/SAFE-MODE exists.
    Safe mode still lists and reports, but installs, updates, pins and reverts are all
    refused so the grail on disk stays exactly what it is.
    Clear it with:  rm .../packs/SAFE-MODE   (or unset BRAINSTEM_SAFE)

Identical refusal for `update`, `self_update`, `remove`, `pin`, `ring` and `revert`, and for
the environment form:

    REFUSED: BRAINSTEM_SAFE=1 is set in the brainstem's environment.

`list` and `status` keep working. In safe mode the boot counter, the auto-rollback and the
host re-gate all no-op as well — safe mode modifies nothing, by contract.

### 4.11 Ed25519 — verified two ways before anything was built on it

    pub(mine)  = d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a
    RFC8032 T1 = d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a
    RFC8032 test-1 public key: MATCH
    openssl sig = 9ffcc96b0a4d311ebe407364c69b31a4f346b2080d3dd479b5b6c65a0adc77f6bb81ed…
    mine    sig = 9ffcc96b0a4d311ebe407364c69b31a4f346b2080d3dd479b5b6c65a0adc77f6bb81ed…
    openssl cross-check sign: MATCH
    verify(good) = True | verify(tampered sig) = False | verify(tampered msg) = False
    verify(wrong key) = False | verify x5 took 0.01s

### 4.12 Automatic rollback after three dirty boots (SPEC 6)

The fixture is a pack that **wedges `load_agents()` at import**, so no `/chat` is ever
served — precisely the failure manual safe mode cannot reach.

    === establish a known-good generation: boot, then serve one request ===
      brainstem says: READY.
      dirty_boots=0 generation=1 last_good=1

    === install the bricking pack ===
    Installed badpack 1.0.0 (ring beta, generation 2).
      agents dir: __pycache__ basic_agent.py pack_agent.py zz_bad_agent.py

    === BOOT 1 ===  curl /health exit: 7   dirty_boots=1 generation=2 last_good=1
    === BOOT 2 ===  curl /health exit: 7   dirty_boots=2 generation=2 last_good=1
    === BOOT 3 ===  curl /health exit: 0   dirty_boots=0 generation=1 last_good=1

    --- grafts.log ---
    2026-09-21T01:19:10Z packs: ROLLBACK generation 2 -> 1: automatic: 3 consecutive boots
      loaded packs without the brainstem serving a request (restored pack_agent.py;
      removed zz_bad_agent.py)
    --- agents dir ---  __pycache__  basic_agent.py  pack_agent.py
    --- installed.json ---
      generation: 1  packs: ['pack-manager']
      last rollback: {"at": "2026-09-21T01:19:10Z", "from_generation": 2,
        "reason": "automatic: 3 consecutive boots loaded packs without the brainstem
        serving a request", "to_generation": 1}
    --- does the reverting boot itself recover? ---
      /health agents: ['Packs'] quarantined: []
      brainstem says: RECOVERED.

Better than specified: the boot that *performs* the revert also recovers. The revert deletes
the offending file before `load_agents()` reaches it in the already-globbed list, and the now
missing file is an ordinary caught exception. No human, no network, no fourth restart.

Manual revert works with the channel **down**:

    channel unreachable (curl exit 7)
    Reverted to generation 3.
      restored : hello_agent.py, pack_agent.py
      removed  : nothing
      A revert is a file move — offline, instant, and recorded in installed.json.

### 4.13 `requires.brainstem` enforced at install and re-checked every boot (SPEC 7)

At install, asked through the running brainstem (which knows it is 0.6.16):

    [Packs] refused: hello-pack 1.0.2 requires brainstem >=9.0.0 but the host is 0.6.16
            (0.6.16 fails >=9.0.0). Nothing was written.

At boot, with the grail version moved under an already-installed pack:

    host version: 0.5.0
    agents: ['Packs']          <- HelloPack is gone, and was never loaded
    quarantined: []            <- inert, not quarantined: it never got the chance to explode
    agents dir: __pycache__ basic_agent.py pack_agent.py
    withdrawn to: .../packs/inert/hello-pack/hello_agent.py
    2026-09-21T01:17:24Z packs: inert hello-pack: requires brainstem >=0.6.0 but the host is 0.5.0

`status` names the reason:

    installed      : 2
        hello-pack     1.0.1  beta  INERT: requires brainstem >=0.6.0 but the host is 0.5.0 (0.5.0 fails >=0.6.0)
        pack-manager   1.0.1  beta  INERT: requires brainstem >=0.6.0 but the host is 0.5.0 (0.5.0 fails >=0.6.0)

Back in range on the next boot:

    host version: 0.6.16
    agents: ['HelloPack', 'Packs']   quarantined: []
    2026-09-21T01:17:36Z packs: restored hello-pack: host is back in range

The manager flags itself out of range but **never withdraws itself** — withdrawing the
recovery agent would remove the way back.

### 4.14 A ring pin changes what `install` resolves (SPEC 8)

    --- pinned to beta ---
    PACK          CHANNEL  RING     STATUS
    hello-pack    1.0.2    beta     UPDATE 1.0.1 -> 1.0.2
    pack-manager  1.0.1    beta     installed 1.0.1
    Other rings: hello-pack 2.0.0 on canary

    --- switch the pin to canary ---
    Ring beta -> canary (fallback order: canary -> beta).

    --- list on canary ---
    hello-pack    2.0.0    canary   UPDATE 1.0.1 -> 2.0.0
    pack-manager  1.0.1    beta     installed 1.0.1      <- no canary build: falls back to beta

    --- update on canary resolves the canary build ---
    Update (ring canary, signature verified-pinned)  -> generation 6
      hello-pack           1.0.1 -> 2.0.0

    --- back on beta, 2.0.0 is NOT silently downgraded ---
      hello-pack           2.0.0 is current (channel offers 1.0.2) — skipped

### 4.15 The tools: 16/16 refusal cases

Run against throwaway trees (full script output kept; summary below).

    rings in index               PASS   both beta 1.0.0 and canary 1.1.0 in one index
    determinism                  PASS   index.json and feed.xml byte-identical on rebuild
    released per ring            PASS   bumping canary did not move beta's released
    version regression           PASS   canary 1.2.0 -> 1.0.9 refused, exit 2
    ring filename mismatch       PASS   pack.nightly.json declaring ring beta refused
    unknown ring                 PASS   ring "stable" refused by builder and verifier
    build no hosts               PASS
    verify no hosts              PASS   rule id no-hosts
    graft multi host             PASS   rule id graft-multi-host
    credential refused           PASS
    credential NOT echoed        PASS   "GitHub token prefix matched (value withheld)"
    grail write                  PASS   a pack writing brainstem.py -> grail-write
    basic_agent                  PASS   install_as basic_agent.py -> grail-basic-agent
    digest                       PASS
    duplicate name               PASS
    16/16 passed

Determinism and `--check` on the real repo:

    e7ac345dc2fc…  channel/index.json     (build 1)
    e7ac345dc2fc…  channel/index.json     (build 2)
    2f554d0972…    channel/feed.xml       identical across both
    9ba24dda52…    channel/index.sig      identical across both
    $ python3 tools/build_channel.py --check
    channel is up to date and signed (1 packs)

`--check` fails on a hand-edited index (prints a unified diff), on a signature that does not
verify, and on a missing signature:

    refused: .../channel/index.sig does not verify against .../channel/index.json (key 34c7caee…)
    refused: .../channel/index.sig is missing: an unsigned index is refused by every client (SPEC 9)

### 4.16 The signing key

    $ python3 tools/build_channel.py --keygen ~/.brainstem/packs/channel-signing.key
    34c7caee6aa305111430a8ab3116a65fdef23d4d336e1172de8d8fc5f80762e4
    private key written to /Users/kodywildfeuer/.brainstem/packs/channel-signing.key (0600);
      it must NEVER be committed
    -rw-------  65  channel-signing.key

    $ python3 tools/build_channel.py --keygen <same path>     # refuses to overwrite
    refused: a signing key already exists. Rotating it is a deliberate human act — move the
    old key aside first.

The public key `34c7caee…` is committed at `channel/channel.pub` and inside
`channel/index.sig`. **The private key is at `~/.brainstem/packs/channel-signing.key`, mode
0600, outside the repository.** Moving it into CI is a secret upload plus pointing
`--key` at it; CI does not need it today because `--check` verifies rather than re-signs.
A CI step fails the build if any private key material is ever tracked.

### 4.17 Python floor

`python3.9` is not installed on this machine. Nearest available:

    Python 3.10.20
    $ python3.10 tools/verify_pack.py      -> PASS pack-manager   exit 0
    $ python3.10 tools/build_channel.py --check -> channel is up to date and signed   exit 0
    $ python3.10 ... ed25519_pure           -> RFC8032 pub d75a9801…  (correct)

3.11 parses all four files. CI pins `3.9` so the real floor is enforced there, not here.
**Not proven on 3.9 locally** — see §6.

---

## 5. Defects found and fixed during verification

**1. Every non-brainstem process counted as a "boot", so three CLI commands fired a spurious
auto-rollback.** Found by using the agent's own CLI: `list`, `install`, `install` in sequence
hit `DIRTY_BOOT_LIMIT` and reverted a generation that was perfectly healthy —

    ROLLBACK generation 3 -> 2: automatic: 3 consecutive boots loaded packs without the
    brainstem serving a request (restored hello_agent.py; removed nothing)

Root cause: the counter keyed on "this module was imported by a new process", which is true
of any CLI run or test import, and only `system_context()` clears it — something only a
brainstem calls. Fix: `is_brainstem_host()` gates `boot_observe()` on `__main__` actually
having a callable `load_agents` and a string `AGENTS_PATH`. Re-proved in §4.12.

**2. `enforce_requires()` would restore an inert pack when it could not read the host
version.** From the CLI, `host_version()` is `None`, `requires_ok()` returned "allow", and
an inert pack was silently restored on a guess. Fix: the re-gate returns early when the host
version is unknown — it never restores on a guess; `status` reports the unknown version.

**3. `verify_pack.py` legitimately failed my own pack** (`grail-write-opaque` ×3): three
`shutil.rmtree` calls whose root came through the helpers `_gen_dir()` / `_p()`, which a
static reader cannot follow. I changed the **pack**, not the rule — those three call sites
now bind their target to `os.path.join(packs_dir(), …)` in one visible expression. I also
made one narrow widening to the rule: `safe_anchor` now accepts a *call* to a safe root
(`packs_dir()`), which is exactly what the rule's own error message asks for and without
which no pack that resolves its state root through a function could ever pass.

**4. The scratch brainstem wrote its boot counter into the real `~/.brainstem/packs/`**
before I passed `RAPP_PACKS_STATE_DIR` through to it. Caught within a minute, file removed,
env var added. (Related: the launch command in my orders shares `.brainstem_book.json` with
the live install — see §3.)

**5. `boot.json` kept a stale `generation` after an automatic rollback** (said 5 while
`installed.json` said 4). Cosmetic, but it is the file a human reads when diagnosing a
rollback. Fixed and re-proved in §4.12.

---

## 6. Flags, surprises, and things I am not sure about

**`from basic_agent import BasicAgent` does not work when `AGENTS_PATH` is relocated.** The
brainstem puts `<brainstem_dir>` and `<brainstem_dir>/agents` on `sys.path` — not
`AGENTS_PATH`. A stock RAR-style agent with the usual two-level fallback fails outright:

    [brainstem] Failed to load .../.scratch/agents/bad_agent.py: No module named 'basic_agent'

`pack_agent.py` therefore carries a **third** fallback that loads `basic_agent.py` by file
path from its own directory, and a fourth that defines a minimal shim. **W2/W3/W4 should
copy that import block into every pack they ship**, or packs will work on a default install
and silently fail on a relocated one. This is also worth a line in `docs/SPEC.md` §1.

**The auto-rollback cannot fire if a bad pack sorts alphabetically before `pack_agent.py`
and hangs at import.** `load_agents()` globs `*_agent.py` sorted; if `astra_agent.py` wedges,
the manager is never imported and never bumps the counter. I proved the mechanism with a pack
installed as `zz_bad_agent.py`, and observed the limitation directly with a `bad_agent.py`.
Any in-process watchdog has this hole. Two options for the coordinator, neither taken
unilaterally because other workstreams reference the filename: (a) install the manager as
something that sorts first (e.g. `aaa_pack_agent.py`) — a heuristic, not a guarantee; (b) an
out-of-process watchdog, which is a different piece of work. **Flagged, not fixed.**

**`BRAINSTEM_STATE_DIR` does nothing.** It appears nowhere in `brainstem.py` (`grep` finds
zero hits). Anything relying on it for isolation is relying on nothing. The manager uses
`RAPP_PACKS_STATE_DIR`, which is mine and does work; `bootstrap.sh` derives its state path
from `$HOME`.

**`released` extends the SPEC §2 index entry shape**, and so does `ring`. `ring` is forced by
§8; `released` is my call, needed to make a "newest first" Atom feed byte-deterministic. If a
conformance suite asserts the *exact* key set of an index entry, it will fail. I think the
right answer is to document both in §2, but that is the coordinator's call.

**Rings live at `packs/<name>/pack.<ring>.json`.** SPEC §8 says every pack version declares a
ring and the index carries all rings, but does not say where a second ring's manifest lives.
This was my choice. `pack.json` is the pack's default-ring manifest; `pack.<ring>.json` adds
others; the filename's ring must equal the manifest's.

**`boot.json` is mine and W2 must not write it.** Format:
`{"schema": "rapp-boot/1", "boot_token", "dirty_boots", "generation", "last_good_generation",
"last_boot_at", "last_clear_at", "last_action", "last_action_at"}`. The clear happens in
`PackAgent.system_context()`. If W2's graft engine wants a stronger "served a real request"
signal than reaching `system_context()`, the clean seam is for it to call into the manager
module rather than write the file — say so and I will expose a named function.

**`verify_pack.py` warns rather than fails on `subprocess` / `os.system` / `ctypes` /
dynamic `exec`.** A pack that shells out can write anywhere, and no static check can prove
otherwise. I kept these as warnings because the order named the grail-path check specifically
and a hard failure would block legitimate W2/W3/W4 packs without review. **This is a real
residual hole in the "no writes outside AGENTS_PATH" guarantee** and should be an explicit
policy decision, not a default.

**TOFU pins on first *successful verification during a write-capable action*, not literally
"first install".** `list` and `status` will report `verified-unpinned` against an unpinned
channel without pinning anything, since a read-only action should not create state. Install,
update and `self_update` pin.

**The index's own `base` field is ignored for fetching.** Honouring it would let a channel
redirect a client elsewhere. The configured base — default
`https://kody-w.github.io/rapp-packs/`, `RAPP_PACKS_BASE` for testing — is the allowlist, and
a redirect off it is refused both by a custom redirect handler and by a post-hoc check of
`resp.geturl()`.

**Not proven:** (a) the Pages deploy — no push was made, so `channel.yml` has never run on
GitHub; the workflow's YAML parses and all three verify steps pass locally. (b) Python 3.9
exactly (3.10 and 3.11 pass). (c) `bootstrap.sh` on Linux or on `wget`/`sha256sum`/`openssl`
paths — only macOS with `curl` + `shasum` was exercised. (d) the SPEC §5 conformance suite
cases 7, 8 and 10 (graft double-load, graft catastrophe, upgrade survival) — those are graft
behaviour and `tests/`, which are not mine.

**Coordinator's request I could not complete inside my own paths:** a conformance-suite case
that fails when a test run mutates the source tree belongs in `tests/`, which another
workstream owns. The check itself is three lines — record the mtimes of
`~/.brainstem/src/rapp_brainstem/{VERSION,.brainstem_model,.copilot_token,.brainstem_book.json}`
before and after, and fail on any change. My own evidence for this run is in §3.

**Astra's round 1 is still in the tree**, amended by me. Its `verify_pack.py` in particular is
a more thorough static checker than I specified (it resolves f-strings, `Path` arithmetic and
variable bindings, and it caught a real problem in my pack). I read the parts I changed and
the parts that fired; I have **not** line-by-line reviewed all 26 KB of it. A second pair of
eyes on that file before it becomes the publishing gate would be worth it.
