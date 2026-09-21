# Doctrine — how this stays sustainable

Written after four adversarial reviews and two research sweeps attacked the design. Every rule
here exists because something broke, not because it sounded prudent. Where a claim in the README
did not survive, this document overrides it.

## 0. The one-paragraph version

**Packs are permanent. Grafts are transitional. The destination is an upstream extension API.**
A pack rides the host's supported agent contract and can be shipped forever. A graft patches the
running host, buys immediacy nothing else can, and is a **loan against the future** — it is only
allowed with a declared seam, a compatibility gate, a cold-recovery route, and a written
retirement condition. A graft is a hook proposal with running code attached; when the hook ships
upstream, the graft dies. Grafts that never retire are the drift disease.

## 1. Two tracks, one destination

| | **Track A — packs** | **Track B — grafts** |
|---|---|---|
| what it touches | the `agents/` directory | the live `__main__` module |
| stability promise | the host's release doctrine names the agent contract (`*_agent.py`, `BasicAgent`, `perform()`) as userspace that never breaks | **none**. Internals are not userspace and may move without notice |
| who it affects | only hosts that install it | **everyone sharing that process**, whether or not they invoke it |
| lifetime | indefinite | until the upstream hook exists |
| approval | may be unattended within a ring | **human-only, never unattended** |
| reversal | delete the file | restart the process |

Every graft ships with `upstream/PROPOSAL.md`: the seam it patches, why the host should own that
seam, the hook's proposed shape, and the condition under which this graft is deleted. A graft
without a retirement condition is not accepted into the channel.

## 2. Claims we are no longer allowed to make

Each of these was disproved by reproduction. Saying them again is a defect.

- ~~"The grail stays pristine, so nothing can break."~~ Byte-identical source does not make a
  byte-identical *runtime*. A graft owns the host's objects, credentials and behaviour regardless
  of what the bytes on disk say.
- ~~"A bad graft cannot brick the brainstem."~~ Per-file `try/except` is not a containment
  boundary. It does not catch a hang, `os._exit`, OOM, or the worst case — a graft that corrupts
  the host and returns cleanly.
- ~~"Safe mode is free and always available."~~ Safe mode is **boot-scoped** and **cooperative**:
  it only disarms packs that choose to check it, and it undoes nothing in a process already
  running. Deleting a pack file does not reverse its live mutation either; only a restart does.
- ~~"Auto-rollback protects users."~~ The boot counter is boot-scoped. The failure a graft is most
  likely to cause — corruption that develops hours after a clean boot — produces **zero dirty
  boots** and never triggers it. Mature OTA systems have the identical blind spot, documented.
- ~~"Rollback restores the previous state."~~ Rollback restores **code**. State a newer generation
  migrated stays migrated, and restoring old code over it is its own hazard. Forward-fix is the
  primary remedy; auto-revert is the exception.

## 3. The four conditions a graft must satisfy

1. **Declared seam.** Name the exact host symbol it depends on and the host versions it was
   verified against. The channel watches that symbol; the day it moves upstream, every graft
   depending on it is flagged before a user meets the failure.
2. **Compatibility gate.** Re-checked every boot, not only at install. Out of range, or anchor
   missing, means **inert with a reason** — never a partial patch, never a loud crash.
3. **Cold-recovery route.** A documented way back that does not depend on the graft behaving:
   physically relocating graft files out of `AGENTS_PATH`, then restarting. Safe mode as a flag is
   a convenience; the physical move is the guarantee.
4. **Retirement condition.** In writing, in the proposal, before the graft is accepted.

## 4. Activation is separate from delivery

Fetching is not enabling. A pack arrives **inert**; a separate, recorded act enables it. That
split is what makes an auto-updating channel compatible with human control, and it is the only
answer to the sharpest threat here: the pack manager is itself an agent the host's own model can
call, with arguments the model chose, in a context full of text from the outside world.

- The model may **install** within an allowlisted ring, and only into the inert state.
- **Activation of a graft is human-only, always.** No prompt, no tool call, no model decision
  activates a graft.
- Local edits are respected: a file whose hash no longer matches its origin is marked
  **user-modified** and excluded from sync rather than overwritten. Upstream never clobbers work
  someone did by hand.

## 5. Trust: what replaces TOFU

TOFU key-pinning was the weakest part of the design and does not survive scrutiny: pinning a key
delivered by the same channel it is meant to authenticate proves nothing on first contact, and a
valid signature authenticates a bad release just as faithfully as a good one.

- **Bootstrap trust arrives through an already-authenticated path** — the host release carries the
  channel root once. A key fetched from the same Pages site it validates is not a trust anchor.
- **Freshness is mandatory**: a monotonic index sequence, an expiry, retained high-water state, one
  index per transaction, and strict channel/ring/name binding. Without freshness, an attacker who
  can serve bytes replays a validly-signed *old* index forever.
- **The release signer is inaccessible to the running brainstem.** A key the host process can
  reach is a key a compromised host can use.
- **Do not build TUF by another name.** Either accept a narrow protocol and state its limit
  plainly — single-author operation with one release key cannot survive compromise of that key —
  or adopt a maintained client. Deciding is required; pretending is not allowed.
- Over-engineering to refuse at this scale: delegation trees for one maintainer, hashed bins for a
  handful of packs, mirror selection for one origin, a transparency log as a prerequisite.

## 6. Containment: the honest limit

There is **no manifest-only change** that closes the host-authority hole while a pack may write to
`sys.modules["__main__"]`. An accepted graft owns the process: its credentials, its import
environment, its behaviour. Therefore:

- Grafts are treated as **privileged host updates**, reviewed as such, human-activated, from a
  first-party source only.
- Third-party extension, if it ever opens beyond that, runs **out of process** under OS-enforced
  restriction with a narrow broker — not a child process under the same user with the same home
  directory, which contains nothing.
- The pre-import safe-mode gate and the recovery controller live **outside replaceable pack code**.
  A safety mechanism a pack can disable is not one.

## 7. Rings, and where they stop

The estate's train already exists: **Canary → Nightly → Alpha → Beta → Grail**, Grail frozen
production, promoted only by a deliberate human act. Packs ride the same rings with the same
meanings and the same vocabulary. Automatic promotion stops at Beta, exactly as it does for the
host. No new words.

## 8. What "done" requires, every time

A pack or graft is not finished until, against a **real brainstem** (a copied tree, never the
user's live install — `BRAINSTEM_STATE_DIR` is not an isolation boundary):

1. it loads without quarantine and answers a real turn;
2. a graft holds wrap depth at exactly 1 across 50+ consecutive turns and concurrent sweeps,
   **counted inside the process**;
3. its failure drills pass: raises, anchor gone, wrong host, safe mode, cold recovery;
4. the harness self-check proves the user's live install was untouched;
5. the honesty pass: every test is shown failing with the bug reintroduced, or it is theatre.
