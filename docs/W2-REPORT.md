# W2 — the graft engine

How a brainstem permanently modifies itself without editing the grail.

Everything below was exercised against a **real brainstem process** (v0.6.16) on port 7102,
launched from a byte-identical copy of the grail, with a real GitHub Copilot token and real
`/chat` turns. No fixtures stand in for the host. Output is pasted verbatim.

---

## 1. What shipped

| path | what it is |
|---|---|
| `packs/graft-engine/graft_runtime.py` | the shared helper every graft imports: safe mode, the SPEC §4.1 markers, anchor verification, host identity, version gating, `grafts.log` |
| `packs/graft-engine/graft_agent.py` | the loader/registry. Discovers `*_graft.py`, applies them under a process-wide lock, isolates failures, and exposes the `Grafts` agent (`status` / `apply` / `revert` / `log`) |
| `packs/graft-hello/hello_graft.py` | the reference graft: one marker attribute, wraps nothing — the control arm of the wrap count |
| `packs/graft-astra-model/astra_model_graft.py` | the first real graft: makes `gpt-6-astra` a first-class model of the running brainstem |
| `packs/graft-astra-model/astra_responses.py` | a stdlib-only Copilot Responses client (no `requests`, no dependency on another workstream's files) |
| three `pack.json` | schema `rapp-pack/1` + the v1.1 fields `hosts` and `ring`, digests computed from the bytes on disk |

Graft modules are named `*_graft.py` **on purpose**: that does not match the brainstem's own
`*_agent.py` loader glob, so the host never imports a graft directly. Only `graft_agent.py` is
an agent; it is the single door every graft comes through.

---

## 2. Why `call_copilot` is the seam

I read `brainstem.py` (3480 lines) looking for the point of least drift. Four candidates:

| candidate | rejected because |
|---|---|
| relax the `supported_endpoints` filter in `_fetch_copilot_models()` | that filter is **correct** — it is what stops `gpt-5.5` and the `*-codex` models from failing with `unsupported_api_for_model`. Letting Astra through it would just move the failure to the POST. `docs/ASTRA-RESPONSES.md` says route around it, not weaken it. |
| monkeypatch `requests.post` | process-global; catches every unrelated HTTP call in the brainstem. Enormous blast radius for a one-model problem. |
| wrap the `chat()` Flask view | would mean re-implementing the three-round tool loop, the history validation and the error contract. Far more surface than the problem. |
| **wrap `call_copilot`** | **chosen** |

`call_copilot` is the narrowest waist in the request path:

- **Tiny contract.** `call_copilot(messages, tools=None) -> (result_dict, model_id)`. Two
  arguments in, an OpenAI-shaped dict and the id of whatever actually answered out. There is
  very little here for an upstream refactor to break, and what little there is, is
  *checkable* — the graft anchors on the parameter names `messages` and `tools` and goes
  inert if either disappears.
- **Late binding, so no restart.** `chat()` resolves the name `call_copilot` from module
  globals on every call, so rebinding `__main__.call_copilot` takes effect on the very next
  message.
- **It already returns the receipt.** `call_copilot` returns `body["model"]` — the model that
  really answered — precisely so a silent substitution can be surfaced. Returning
  `"gpt-6-astra"` from the wrapper makes `/chat`'s existing `"model"` field the honest
  receipt with no change upstream.
- **One place, not many.** Every `/chat` round and the streaming fallback funnel through it.

Two supporting anchors, each independently gated:

- `AVAILABLE_MODELS` — inject the entry so the picker and `/models/set` accept it.
- `_fetch_copilot_models` — wrapped because it does `AVAILABLE_MODELS = new_models`, a
  **rebind, not a mutation**. Without this wrapper the entry silently vanishes the next time
  the catalog is fetched (which happens again whenever a login clears `_models_fetched`).
- `call_copilot_stream` — optional; raises `StreamingUnsupported` immediately for a
  responses-only model so `/chat/stream` falls back onto the wrapped `call_copilot` instead
  of burning a 400 first.

---

## 3. Ground truth: what the Responses API actually accepts

Before writing the client I probed the live enterprise endpoint with stdlib only. This
settled the translation rather than leaving it to inference:

```
endpoint: https://api.enterprise.githubcopilot.com

=== A. the shape docs/ASTRA-RESPONSES.md verified (plain text, no tools) ===
status 200
model=gpt-6-astra status=completed output_items=1
    message text='ASTRA-RESPONSES-ONLINE'

=== B. rich shape: top-level instructions + system-ish + assistant turn ===
status 200
model=gpt-6-astra status=completed output_items=1
    message text='PONG'

=== C. FLATTENED tool schema (Responses style: type/name/parameters at top level) ===
status 200
model=gpt-6-astra status=completed output_items=1
    function_call
       call_id=call_wXlokYK6jmfflceDfGPzvmCI name=get_weather args={"city":"Atlanta"}

=== D. NESTED (chat-completions style) tool schema — expected to be REJECTED ===
status 400
{"error":{"message":"Missing required parameter: 'tools[0].name'.","code":"invalid_request_body"}}

=== E. tool RESULT round-trip: function_call + function_call_output ===
status 200
model=gpt-6-astra status=completed output_items=1
    message text='It's currently 71°F and sunny in Atlanta.'
```

**D is the load-bearing one.** Handing the Responses API a chat-completions tool array is a
hard 400, so the translation in `to_responses_body()` is mandatory, not cosmetic. C and E
prove the tool round-trip works in both directions, which is what lets a grafted Astra drive
the brainstem's own agents.

The client keeps a fallback ladder anyway: on a 401 it refreshes the token once and retries
(mirroring `call_copilot`'s own 401 self-heal); on any other 4xx it retries once with the
degraded payload — the whole conversation flattened into one `input_text`, no tools — which
is exactly shape A, the one that is known to work.

---

## 4. Proofs

### 4.1 Baseline — the problem, on a pure grail

```
--- /health (pure grail) ---
{"agents":[], ... "model":"claude-haiku-4.5", "quarantined":[], "status":"ok","version":"0.6.16"}

--- models: astra present? ---
count 25
gpt-6-astra in list: False
current claude-haiku-4.5
```

### 4.2 The graft applies on boot; `/health` shows the engine, not a quarantine

```
=== /health WITH THE GRAFT ENGINE ===
{
    "agents": [ "Grafts" ],
    "quarantined": [],
    "status": "ok",
    "version": "0.6.16"
}
=== boot log (graft lines) ===
[brainstem] Agent loaded: Grafts
[brainstem] 1 agent(s) ready.
```

```
=== grafts.log ===
{"ts": "...", "event": "graft.applying", "graft": "astra-model", "file": ".../astra_model_graft.py"}
{"ts": "...", "event": "graft.applied",  "graft": "astra-model", "file": ".../astra_model_graft.py", "version": "1.0.0"}
{"ts": "...", "event": "graft.applying", "graft": "hello", "file": ".../hello_graft.py"}
{"ts": "...", "event": "graft.applied",  "graft": "hello", "file": ".../hello_graft.py", "version": "1.0.0"}
```

### 4.3 THE MONEY SHOT — `gpt-6-astra` is first-class and answers through `/responses`

```
=== 1. is gpt-6-astra in the live model picker? ===
catalog size: 26
current model: claude-haiku-4.5
gpt-6-astra entry: [{"available": true, "grafted_by": "astra-model", "id": "gpt-6-astra",
                     "name": "GPT-6 Astra", "supported_endpoints": ["/responses", "ws:/responses"]}]

=== 2. select it the way the UI does (POST /models/set) ===
{"model":"gpt-6-astra"}

=== 3. a real /chat turn ===
response      : 'I'm ChatGPT, but I can't verify my exact model ID or whether this request used the Responses API.'
model (receipt): gpt-6-astra
requested_model: gpt-6-astra

=== brainstem stdout during that turn ===
[graft:astra-model] routing model=gpt-6-astra via POST /responses (tools=1)
```

The catalog went 25 → 26. The model's own words are worthless as evidence (no model knows its
id), so read the two things that are not self-reported: `model (receipt)` is taken from the
`/responses` reply's own `model` field, and the stdout line is printed by the graft at the
moment it chooses the route. `tools=1` means the brainstem's own agent tool array was
translated and accepted — the rich path, not the degraded fallback.

### 4.4 50 consecutive `/chat` turns → exactly ONE wrap (counted, not assumed)

All 50 turns ran with `gpt-6-astra` selected, so this proves the wrap count *and* that the
route stays healthy for 50 real Astra turns. The count is measured **inside the live process**
by a read-only probe that walks the `__wrapped__` chain on every `load_agents()` sweep.

```
=== 50 CONSECUTIVE /chat TURNS, ALL ON gpt-6-astra ===
... 50 turns done (ok=50 fail=0)
TURNS=50 OK=50 FAIL=0
bash .scratch/turns50.sh 50  0.16s user 0.26s system 0% cpu 1:33.86 total

=== WRAP COUNT AFTER 50 TURNS ===
probe samples (one per load_agents() sweep): 50
call_copilot        wrap-depth histogram: {1: 50}
_fetch_copilot_models wrap-depth histogram: {1: 50}
call_copilot_stream wrap-depth histogram: {1: 50}
gpt-6-astra catalog entries histogram:     {1: 50}
hello marker present histogram:            {True: 50}
applied (last sample): ['astra-model', 'hello']
counters(last sample): {'astra_responses_calls': 53}
__grafts_applied__ (last sample): {'astra-model': '1.0.0', 'hello': '1.0.0'}
full chain (last sample): call_copilot=['astra-model', None]

=== grafts.log line count (must NOT grow per turn) ===
       4 .scratch/packs/grafts.log

=== routing lines in brainstem stdout ===
54
```

`{1: 50}` means every single one of the 50 samples measured depth exactly 1. `grafts.log`
stayed at 4 lines across 50 turns — the log-once guard works, or the file would have grown by
a line per message.

### 4.5 The thread race (`app.run(threaded=True)`)

The brainstem serves threaded, so `load_agents()` — and therefore the engine — runs
concurrently on several worker threads. Two threads both reading "not applied" and both
wrapping is the classic way this mechanism breaks. Hammered with 40 concurrent `/health` and
8 concurrent `/chat`:

```
--- 40 concurrent /health + 8 concurrent /chat, all at once ---
all concurrent requests finished
=== wrap depth under concurrent load ===
probe samples (one per load_agents() sweep): 48
call_copilot        wrap-depth histogram: {1: 48}
gpt-6-astra catalog entries histogram:     {1: 48}
```

The fix is in `graft_runtime`: every copy of the module registers under one stable name and
the engine uses the object `sys.modules.setdefault()` **returns**, which makes `ENGINE_LOCK` a
true process singleton even though the file is re-executed per turn.

### 4.6 A graft whose `apply()` raises — the brainstem runs ungrafted

Three adversarial grafts installed at once: one that raises, one declaring a different host,
one whose anchor was renamed out from under it.

```
=== /health WITH a raising graft, a wrong-host graft and a drifted graft all present ===
{ "agents": [ "Grafts" ], "quarantined": [], "status": "ok", "version": "0.6.16" }

=== the brainstem still answers a real /chat turn ===
response: "**ALIVE-DESPITE-BAD-GRAFTS** ..."
model: claude-haiku-4.5
```

```
{"event": "graft.failed", "graft": "boom", "error": "ZeroDivisionError('integer division or modulo by zero')"}
    TRACEBACK:
          result = mod.apply(main)
        File ".../boom_graft.py", line 12, in apply
          raise ZeroDivisionError("deliberate graft catastrophe: %d" % (1 // 0))
      ZeroDivisionError: integer division or modulo by zero
```

The healthy grafts were unaffected — `astra-model` and `hello` both still `applied`,
`verify=True`, wraps still 1.

### 4.7 Anchor miss → inert with a reason (§7); wrong host → refused (§10)

The drifted graft is the **real** astra graft with exactly one identifier changed
(`AVAILABLE_MODELS` → `AVAILABLE_MODELS_RENAMED_UPSTREAM`), which is what a grail rename
would look like.

```
alien                  state=inert    verify=False  reason=host mismatch: running on 'brainstem', graft declares ['hermes']
astra-model            state=applied  verify=True   reason=
astra-model-drifted    state=inert    verify=False  reason=anchor missing: AVAILABLE_MODELS_RENAMED_UPSTREAM is not an attribute of the host
boom                   state=failed   verify=False  reason={'error': "ZeroDivisionError(...)"}
hello                  state=applied  verify=True   reason=

host_marker        : {'astra-model': '1.0.0', 'hello': '1.0.0'}
unterminated_apply : []
astra wraps        : {'call_copilot': 1, '_fetch_copilot_models': 1, 'call_copilot_stream': 1, 'catalog_entries': 1}
```

The wrong-host graft sets a sentinel attribute the moment `apply()` runs, so its absence is
positive proof the gate fired first:

```
alien sentinel check: alien never produced a graft.applying line -> True
```

`alien` has no `graft.applying` line at all, because §10's host check runs **before** the
applying marker and before the graft touches the module.

### 4.8 Safe mode — indistinguishable from the pure grail

Every pack file plus all three adversarial grafts physically present in `agents/`:

```
--- agent files physically present while SAFE-MODE is on ---
alien_graft.py  astra_model_graft.py  astra_responses.py  basic_agent.py
boom_graft.py   graft_agent.py        graft_runtime.py    hello_graft.py  zdrift_graft.py

--- diff vs the PURE GRAIL baseline (captured before any pack existed) ---
IDENTICAL -- safe mode is indistinguishable from the pure grail
```

A clean safe-mode boot writes nothing and changes nothing:

```
=== grafts.log after a clean SAFE-MODE boot (must not exist / be empty) ===
ls: .scratch/packs/grafts.log: No such file or directory
NO grafts.log written at all

=== /models under SAFE-MODE: is gpt-6-astra there? ===
catalog size: 25  (pure-grail baseline was 25)
gpt-6-astra present: False
```

The `BRAINSTEM_SAFE=1` trigger is equally total:

```
=== BRAINSTEM_SAFE=1 (no SAFE-MODE file) ===
{"agents":[], ... "quarantined":[],"status":"ok","version":"0.6.16"}
IDENTICAL to the pure grail
```

This works because in safe mode `graft_agent.py` defines **no class at all**. A class that
merely declines to act would still register as an agent and show up in `/health`.

### 4.9 SPEC §4.1 — the markers disagree, and the engine re-wraps the fresh object

Staged from inside the process: a fixture that sorts after `graft_agent.py` rips the wrapper
off `call_copilot` mid-sweep while deliberately leaving `__grafts_applied__` intact — exactly
what a grail upgrade leaves behind.

```
--- state before the simulated upgrade ---
call_copilot chain: ['astra-model', None]
__grafts_applied__: {'astra-model': '1.0.0', 'hello': '1.0.0'}

--- arming the trigger and taking one turn ---
sim result: unwrapped call_copilot, host marker left as {'astra-model': '1.0.0', 'hello': '1.0.0'}

--- next sweep: does the engine notice the markers disagree and re-wrap? ---
chain: [None]                   marks: {'astra-model': '1.0.0', 'hello': '1.0.0'}
chain: ['astra-model', None]    marks: {'hello': '1.0.0', 'astra-model': '1.0.0'}

REMARK EVENT: {"event": "graft.remark", "graft": "astra-model",
               "detail": "host marker survived but verify() says not in effect - re-applying against the fresh objects"}
```

Depth came back to **1**, not 2.

### 4.10 The `Grafts` agent, driven through a real `/chat` tool call

Not called directly — the model chose the tool and the brainstem executed it:

```
[Grafts] {
  "engine": "1.0.0", "runtime": "1.0.0", "safe_mode": false,
  "host": "brainstem", "host_version": "0.6.16",
  "host_marker": { "astra-model": "1.0.0", "hello": "1.0.0" },
  "counters": { "astra_responses_calls": 54 },
  "unterminated_apply": [],
  "grafts": {
    "astra-model": { "declares_hosts": ["brainstem"], "verify": true,
      "wraps": { "call_copilot": 1, "_fetch_copilot_models": 1,
                 "call_copilot_stream": 1, "catalog_entries": 1 },
      "state": "applied" },
    "hello": { "verify": true, "wraps": { "marker_present": true, "wraps": 0 },
      "state": "applied" }
  }
}
```

### 4.11 Revert, live, with no restart

```
reverted: {"astra-model": {"call_copilot": true, "_fetch_copilot_models": true,
                           "call_copilot_stream": true, "catalog_entry_removed": true},
           "hello": true}
host_marker after: {}
astra wraps after: {'call_copilot': 0, '_fetch_copilot_models': 0, 'call_copilot_stream': 0, 'catalog_entries': 0}

=== is gpt-6-astra gone from the live picker, with no restart? ===
catalog size: 25 gpt-6-astra present: False
```

### 4.12 Not one byte of the grail changed

```
=== THE GRAIL, BEFORE vs AFTER ===
IDENTICAL — not one byte of the grail changed

=== the grail's own agents dir, untouched ===
666cce24f64715dfea31ed61cba94017634a26afe3df0bee2428396c48c40a82  -
(before: 666cce24f64715dfea31ed61cba94017634a26afe3df0bee2428396c48c40a82)

=== the live brainstem on :7071 is still the one that was there ===
Python  9520 kodywildfeuer    4u  IPv4 0x3312e1de8a9f1ad0      0t0  TCP 127.0.0.1:7071 (LISTEN)
```

Static check over everything in `packs/`: no reference to the grail tree, and the only write
mode used anywhere is append, into the packs dir.

---

## 5. Flags and surprises

**Read these before trusting anything above.**

1. **The failure shapes my `try/except` CANNOT catch** (the coordinator asked for this
   explicitly, and it is the honest answer to why W1's boot counter has to exist):
   - **a hang** — a graft whose `apply()` blocks forever (a socket with no timeout, a lock it
     will never get). `load_agents()` never returns, the worker thread is gone, and on the
     boot sweep the process comes up but never serves. No exception is ever raised.
   - **`os._exit()` / `sys.exit()` from a C extension / a segfault** — the process dies
     without unwinding. Nothing runs, including my `except`.
   - **memory exhaustion / the OOM killer** — same shape.
   - **a graft that corrupts the host and then returns cleanly.** My anchors stop a graft
     from wrapping the *wrong* thing; nothing stops a graft that is simply malicious or
     wrong about the *right* thing. `apply()` gets the live module.
   - What I *can* do, and did: `rt.log("graft.applying", …)` is written **before** every
     `apply()` call. A wedge therefore leaves an unterminated line naming the culprit, and
     `Grafts status` surfaces it as `unterminated_apply`. That turns "the brainstem is
     bricked" into "graft X was mid-apply when it died" — which is the attribution W1's
     counter needs to pick the right generation to roll back to.
   - I have **not** written to `~/.brainstem/packs/boot.json`. It is W1's file and I did not
     touch it.

2. **§4.4 — where my grafts brush the boot line.** `graft-astra-model` wraps
   `_fetch_copilot_models`, which the grail *also* calls at startup (line 3441) — before
   `load_agents()`. So on a cold boot the catalog is fetched by the **unwrapped** original,
   and the graft's own direct injection into `AVAILABLE_MODELS` is what makes Astra appear;
   the wrapper only matters for *later* refetches (e.g. after a login switch). That is the
   correct outcome and it is why step 1 and step 2 are separate steps rather than one. But it
   means a graft genuinely cannot influence the *first* catalog fetch, and no graft ever
   could. Neither pack needs `requires_grail_change`.

3. **`revert` is a live undo, not an uninstall.** After a successful revert the graft file is
   still on disk, so the next `load_agents()` sweep re-applies it (verified: depth returns to
   1, cleanly, not 2). To make a revert stick you remove the file or use safe mode. If the
   channel wants sticky reverts, the engine needs a persisted disable list — say the word and
   I will add one; I did not invent the file format unilaterally.

4. **Astra can be picked as a fallback model.** The injected entry carries
   `"available": true` so the picker offers it. `call_copilot`'s internal fallback loop builds
   its candidate list from `AVAILABLE_MODELS`, so if a chat model 400s, Astra *can* be tried —
   over `/chat/completions`, where it will fail. It is low risk (the loop hoists `gpt-4o` to
   the front, so Astra is only reached if the safety net also failed) and it costs one wasted
   request, not a wrong answer. Setting `"available": false` would fix it but may grey the
   model out in the picker. I left it visible and am flagging it rather than quietly choosing.

5. **A responses-route failure is surfaced, not papered over.** When `/responses` fails the
   wrapper raises a `RuntimeError` naming the graft instead of delegating to the original.
   Delegating would POST Astra to `/chat/completions`, get a 400, and let the grail's fallback
   loop answer as a *different* model under Astra's name. A clean error beats a silent
   misattribution — but it does mean a `/responses` outage is a visible 502 on that turn
   rather than a degraded answer.

6. **`pack.json` has no cross-pack dependency field.** `graft-hello` and `graft-astra-model`
   both require `graft-engine` to be installed, and the schema in §1 has nowhere to say so. I
   put it in each `summary` in prose rather than inventing a key W1's verifier would reject.
   This wants a real field (`requires.packs`), and that is W1's call.

7. **The verification rig deviates from the brief in one way, deliberately.** The brief's
   launch command runs `brainstem.py` from the real grail path. `brainstem.py` derives
   `_BASE_DIR` from its own `__file__` and **ignores `BRAINSTEM_STATE_DIR`**, so running it
   from that path writes `.copilot_session`, `.brainstem_book.json` and — critically —
   `.brainstem_model` **into the grail tree**, racing the live brainstem on :7071 and changing
   its sticky model. So I ran from a byte-identical copy instead
   (`sha256 35618683ebc3…` on both) with its own `agents/`. Same real code, same real process,
   zero collision. §4.12 proves the grail came out untouched. Worth telling W1: **the
   conformance suite cannot use `BRAINSTEM_STATE_DIR` for isolation — it does nothing.**

8. **`BRAINSTEM_PACKS_DIR` is an extension I introduced.** The spec fixes the packs dir at
   `~/.brainstem/packs`. Hermetic tests need an override, so `packs_dir()` honours that env
   var. Consequence worth knowing: safe mode is checked against the *overridden* dir, so a
   process with the override set does not see a `SAFE-MODE` file in the canonical location. I
   chose test isolation over a belt-and-braces double check; flagging it because it is a
   genuine trade-off, not an oversight.

9. **The model's self-description is not evidence.** In §4.3 Astra said "I'm ChatGPT". Models
   do not know their own ids. The receipt (`model: gpt-6-astra`, read from the `/responses`
   reply) and the graft's own stdout line are the evidence; the prose is not. Any future
   demo script that asserts on the model's self-description will be asserting on noise.

10. **Astra-as-muscle did not deliver this build.** I wrote the order, launched
    `copilot --model gpt-6-astra` twice, and both runs stalled at the same point — after
    orientation, before writing a byte — for 15+ minutes each, on a machine already running
    four other copilot jobs. I killed them and wrote the code directly (pure
    logic/state/lifecycle work). Astra *did* do the highest-leverage part: §3's live API
    probe is Astra-served ground truth that removed every guess from the client. Unverified
    claim I am not making: I cannot say whether the stalls were contention or something about
    the run itself.

11. **Not tested.** `/chat/stream` end-to-end with Astra selected (the stream wrapper's
    `StreamingUnsupported` path is anchored and installed — wrap count 1 — but I did not drive
    an SSE client through the fallback). Multi-round tool calling *through* Astra (probe E
    proves the wire shape round-trips, but I did not force a real brainstem agent loop to take
    two rounds on Astra). §5 case 10, upgrade survival, belongs to W1's install machinery and
    I did not exercise it.

---

## 6. Conformance-suite cases W2 covers

| §5 case | status |
|---|---|
| 7. graft, double-load: 50 turns → exactly one wrap | **passed**, counted — §4.4 |
| 8. graft, catastrophe: `apply()` raises → still boots and answers, logged | **passed** — §4.6 |
| 9. safe mode: `/health` identical to the pure grail | **passed**, byte diff — §4.8 |
| §7 anchor miss → inert with a reason | **passed** — §4.7 |
| §10 wrong host → refuses before touching the module | **passed** — §4.7 |
| §4.1 marker disagreement → re-wrap, depth 1 | **passed** — §4.9 |
| thread race under concurrent load | **passed** — §4.5 |
| 1–6, 10 (install / tamper / digest / update / remove / upgrade survival) | W1's install machinery, not exercised here |
