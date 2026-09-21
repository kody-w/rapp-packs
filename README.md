# rapp-packs — the brainstem expansion channel

A RAPP brainstem ships as **the grail**: one pristine upstream tree. This repo is how a
brainstem grows past it — **without ever editing the grail**, and without waiting for a
release train.

    curl -fsSL https://kody-w.github.io/rapp-packs/bootstrap.sh | sh

That drops exactly one file into your brainstem's `agents/` directory. From then on the
brainstem updates itself from this channel.

## Four words

| word | what it is |
|---|---|
| **grail** | the pristine brainstem tree on disk. Never edited by anything here. Always the fallback. |
| **pack** | a drop-in capability: one or more `*_agent.py` files plus a `pack.json`. Fetched from this channel, SHA-256 verified, written into `agents/`. |
| **graft** | a pack that changes the *running* brainstem — it patches `sys.modules['__main__']` when it loads. Permanent in effect (re-applied every boot), zero bytes of the grail changed. |
| **safe mode** | boot with every pack and graft inert. What you get is the pure grail. |

## Why this exists

The brainstem's built-in catalog is pinned to one immutable RAR revision per release
(`RAR_REVISION` in `brainstem.py`; `/agents/import` refuses bytes carrying any other
`source_revision`). That pin is a real integrity guarantee — it is also why a newly
published agent cannot reach an installed brainstem until a grail release moves it.

A pack channel routes around the *release coupling* without giving up the *integrity*:
every pack is verified against a hash in a signed channel index before a byte is written.

## Why a graft instead of a patch

An agent in `agents/` is imported **into the brainstem's own process**. It can read and
rewrite the live module:

    __main__file=.../brainstem.py | MODEL=YES | AVAILABLE_MODELS=YES (writable)
    load_agents=YES | get_copilot_token=YES | app=YES

So a graft never edits `brainstem.py`. It re-applies itself every time the process starts,
which means:

- **An upgrade cannot clobber it.** The installer replaces the tree; `agents/` is preserved;
  the graft re-applies on the next boot.
- **Safe mode is free.** The grail on disk is already pristine — falling back is just
  refusing to load the graft, never an uninstall or a revert.
- **A bad graft cannot brick the brainstem.** It is isolated per file; a graft that raises is
  quarantined by the brainstem's existing agent quarantine and the rest still boot.

## Safe mode

    touch ~/.brainstem/packs/SAFE-MODE     # every pack and graft goes inert on next boot
    rm    ~/.brainstem/packs/SAFE-MODE     # back to normal

Any pack that fails to go inert under that file is a defect, and the conformance suite in
`tests/` fails the pack.

## Layout

    packs/<name>/pack.json      manifest: version, files + sha256, kind (pack|graft), requires
    packs/<name>/*.py           the agent files
    channel/index.json          every published pack + version + digest (built by CI)
    channel/feed.xml            Atom feed of releases (poll this, not the API)
    tools/                      channel builder + verifier
    tests/                      conformance suite — run against a REAL brainstem, not a fixture
    docs/SPEC.md                the pack + graft contract

## Trust

- Every file is SHA-256 pinned in `pack.json`, and every `pack.json` is digest-pinned in
  `channel/index.json`. A mismatch refuses the write. Fail closed, always.
- No pack in this channel ever carries a credential. Packs read credentials the host already
  holds (the brainstem's own Copilot token, `~/.treg/token`, the environment) and never copy,
  log, or transmit them.
