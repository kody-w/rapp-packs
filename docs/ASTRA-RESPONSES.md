# GPT-6 Astra over the Copilot Responses API — verified facts

Everything here was observed on this machine on 2026-09-20. Build on it; do not re-derive it.

## Why the brainstem cannot see Astra

From the live catalog (`GET {endpoint}/models`):

    "id": "gpt-6-astra", "name": "GPT-6 Astra", "vendor": "OpenAI",
    "policy": {"state": "enabled"}, "model_picker_enabled": true, "preview": false,
    "model_picker_category": "powerful",
    "supported_endpoints": ["/responses", "ws:/responses"],       <-- NO /chat/completions
    "capabilities": {"type": "chat", "family": "gpt-6-astra", "tokenizer": "o200k_base",
      "limits": {"max_context_window_tokens": 1178000, "max_output_tokens": 128000,
                 "max_prompt_tokens": 1050000,
                 "vision": {"max_prompt_images": 1, "max_prompt_image_size": 3145728,
                            "supported_media_types": ["image/jpeg","image/png","image/webp",
                                                      "image/gif","application/pdf"]}},
      "supports": {"tool_calls": true, "parallel_tool_calls": true, "streaming": true,
                   "structured_outputs": true, "vision": true,
                   "reasoning_effort": ["low","medium","high","xhigh","max"]}}

`brainstem.py` drives `/chat/completions` and skips any model whose `supported_endpoints`
lacks it. That filter is CORRECT — it is what stops `gpt-5.5` and `*-codex` from failing with
`unsupported_api_for_model`. Do not weaken it. Route around it.

## Token

Mirror the brainstem's own resolution — never invent a second scheme:

1. `~/.brainstem/state/.copilot_token`  (JSON, key `access_token`)
2. `<brainstem_dir>/.copilot_token`     (legacy in-tree; where it lives on this machine)
3. `os.environ["GITHUB_TOKEN"]`

Exchange it:

    GET https://api.github.com/copilot_internal/v2/token
    headers: Authorization: token <gh_oauth_token>, Editor-Version: vscode/1.95.0
    -> {"token": "...", "expires_at": <unix>, "endpoints": {"api": "https://api.enterprise.githubcopilot.com"}}

Cache until `expires_at - 60`. Read the endpoint from the reply; never hardcode it (it is the
enterprise host on this account). A 401 on a call means the cached token died: re-exchange
once, retry once.

## The call — verified 200

    POST {endpoint}/responses
    headers: Authorization: Bearer <copilot_token>, Content-Type: application/json,
             Editor-Version: vscode/1.95.0, Copilot-Integration-Id: vscode-chat
    body: {"model": "gpt-6-astra",
           "input": [{"role": "user", "content": [{"type": "input_text", "text": "..."}]}],
           "reasoning": {"effort": "low"}, "max_output_tokens": 2000, "stream": false}

Reply shape:

    {"object": "response", "model": "gpt-6-astra", "id": "...", "created_at": ...,
     "max_output_tokens": 2000, "incomplete_details": null, "instructions": null,
     "output": [{"content": [{"type": "output_text", "text": "ASTRA-RESPONSES-ONLINE",
                              "annotations": [], "logprobs": []}], "id": "..."}],
     "copilot_usage": {"total_nano_aiu": 78000000,
       "token_details": [{"model": "gpt-6-astra", "token_type": "input|output|cache_read|cache_write",
                          "token_count": N, "batch_size": ..., "cost_per_batch": ...}]}}

Parse by walking `output[]` and concatenating every `content[]` item with
`type == "output_text"`. Reasoning items carry no `output_text` — skip them without failing.

## What an agent can see of its host

A read-only probe agent dropped into a live brainstem's `agents/` and called over `/chat`
reported, verbatim:

    __main__file=/Users/kodywildfeuer/.brainstem/src/rapp_brainstem/brainstem.py
    MODEL=YES | AVAILABLE_MODELS=YES | RAR_REVISION=YES | AGENTS_PATH=YES
    load_agents=YES | _fetch_copilot_models=YES | get_copilot_token=YES | app=YES
    models_len=25 | writable=YES

That is the whole basis of the graft mechanism: an agent runs INSIDE the brainstem process
and can rewrite the live module. Nothing on disk needs to change.
