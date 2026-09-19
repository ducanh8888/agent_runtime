"""Compatibility-test individual real models through a configured router,
against AgentRT's actual tool-call contract -- not the router's own
pre-built `auto/*` combos, and not a toy single call.

This talks directly to `AGENTRT_BASE_URL`'s OpenAI-compatible Chat
Completions endpoint with a real API key from the resolved router config
(`agentrt.runtime.config.load_router_config()`), independent of the AgentRT
daemon: the question this answers is "does this real backend model behave
correctly for AgentRT's tool loop", which is upstream of AgentRT itself and
is not something a config change in this repository can affect.

Five checks per model, matching AgentRT's own request/response shape
(preserve tool_call.id, function name, JSON arguments, role=tool,
tool_call_id, sequential tool history, provider finish_reason):

  A. basic function call        -- one call, valid JSON arguments
  B. tool-result continuation   -- feed the result back, must continue
  C. multi-turn loop            -- two sequential tool calls, then a final answer
  D. invalid-tool recovery      -- a malformed tool result; must not crash the turn
  E. parallel tool calls        -- one turn, two tools; not disqualifying if absent

Usage:

    ./packages/.venv/bin/python tools/omniroute_probe.py <model-id> [<model-id> ...]
    ./packages/.venv/bin/python tools/omniroute_probe.py --file candidates.txt

Prints one JSON object per model (stdout, one line each, so a long run can
be interrupted without losing completed results) and writes the full list to
tools/_omniroute_probe_results.json (gitignored -- these are one deployment's
live measurements, not a claim that ships with the repository).

Deliberately conservative with the account's quota: max_tokens is small,
and this makes exactly 4 requests per model (A, B, C's two steps) plus one
for E -- not a stress test.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from agentrt.runtime import config


READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read the contents of a file by path.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
}
WRITE_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Write text content to a file by path.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
}


class Router:
    def __init__(self) -> None:
        router = config.load_router_config()
        self.base = router.base_url.rstrip("/")
        self.key = router.api_key
        self.model_configured = router.model

    def call(self, model: str, messages: list[dict], tools: list[dict]) -> tuple[httpx.Response, float]:
        t0 = time.monotonic()
        r = httpx.post(
            f"{self.base}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
            },
            json={"model": model, "messages": messages, "tools": tools, "max_tokens": 300},
            timeout=60,
        )
        return r, time.monotonic() - t0


def _tool_calls(resp_json: dict) -> list[dict]:
    return resp_json["choices"][0]["message"].get("tool_calls") or []


def check_a_basic_function_call(router: Router, model: str) -> dict:
    """A. One supplied tool, one user turn -- must call it with valid JSON args."""
    out: dict[str, Any] = {}
    r, elapsed = router.call(
        model,
        [{"role": "user", "content": "Read the file at /tmp/foo.txt using the read_file tool."}],
        [READ_FILE_TOOL],
    )
    out["status"] = r.status_code
    out["latency_s"] = round(elapsed, 2)
    if r.status_code != 200:
        out["error"] = r.text[:300]
        return out
    data = r.json()
    calls = _tool_calls(data)
    out["finish_reason"] = data["choices"][0].get("finish_reason")
    out["made_call"] = bool(calls)
    if calls:
        tc = calls[0]
        out["tool_name"] = tc["function"]["name"]
        out["tool_call_id_present"] = bool(tc.get("id"))
        try:
            json.loads(tc["function"]["arguments"])
            out["args_valid_json"] = True
        except Exception as e:
            out["args_valid_json"] = False
            out["args_error"] = str(e)[:200]
        out["_first_call"] = data["choices"][0]["message"]
    else:
        out["raw_content"] = (data["choices"][0]["message"].get("content") or "")[:150]
    return out


def check_b_tool_result_continuation(router: Router, model: str, first_message: dict) -> dict:
    """B. Feed the tool result back; the model must continue, not repeat the call
    or lose the thread."""
    out: dict[str, Any] = {}
    calls = first_message.get("tool_calls") or []
    if not calls:
        out["skipped"] = "no tool call from step A to continue"
        return out
    tc = calls[0]
    messages = [
        {"role": "user", "content": "Read the file at /tmp/foo.txt using the read_file tool."},
        first_message,
        {"role": "tool", "tool_call_id": tc.get("id"), "content": "hello from foo.txt"},
    ]
    r, elapsed = router.call(model, messages, [READ_FILE_TOOL])
    out["status"] = r.status_code
    out["latency_s"] = round(elapsed, 2)
    if r.status_code != 200:
        out["error"] = r.text[:300]
        return out
    data = r.json()
    msg = data["choices"][0]["message"]
    out["finish_reason"] = data["choices"][0].get("finish_reason")
    out["repeated_same_call"] = bool(msg.get("tool_calls"))
    out["continuation_text"] = (msg.get("content") or "")[:150]
    out["_message"] = msg
    return out


def check_c_multiturn_loop(router: Router, model: str) -> dict:
    """C. user task -> tool call -> result -> second tool call -> result ->
    final answer. The minimum useful agent-loop sequence."""
    out: dict[str, Any] = {}
    messages = [
        {
            "role": "user",
            "content": (
                "First read /tmp/a.txt with read_file, then write its uppercased "
                "content to /tmp/b.txt with write_file, then tell me you are done."
            ),
        }
    ]
    tools = [READ_FILE_TOOL, WRITE_FILE_TOOL]
    steps = []
    for step in range(4):
        r, elapsed = router.call(model, messages, tools)
        if r.status_code != 200:
            out["status"] = r.status_code
            out["error"] = r.text[:300]
            out["steps_completed"] = step
            out["steps"] = steps
            return out
        data = r.json()
        msg = data["choices"][0]["message"]
        finish = data["choices"][0].get("finish_reason")
        calls = msg.get("tool_calls") or []
        steps.append(
            {
                "finish_reason": finish,
                "tool_name": calls[0]["function"]["name"] if calls else None,
                "latency_s": round(elapsed, 2),
            }
        )
        messages.append(msg)
        if not calls:
            out["final_text"] = (msg.get("content") or "")[:150]
            break
        tc = calls[0]
        fake_result = "AAA" if tc["function"]["name"] == "read_file" else "ok, written"
        messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": fake_result})
    out["status"] = 200
    out["steps"] = steps
    out["reached_final_answer"] = "final_text" in out
    out["distinct_tools_called"] = len({s["tool_name"] for s in steps if s["tool_name"]})
    return out


def check_d_invalid_tool_recovery(router: Router, model: str) -> dict:
    """D. Feed back a tool result that reports an error; the model must
    produce a usable next step, not repeat the identical call forever or
    break the turn."""
    out: dict[str, Any] = {}
    messages = [
        {"role": "user", "content": "Read the file at /tmp/does_not_exist.txt using the read_file tool."},
    ]
    r, elapsed = router.call(model, messages, [READ_FILE_TOOL])
    if r.status_code != 200:
        out["status"] = r.status_code
        out["error"] = r.text[:300]
        return out
    data = r.json()
    msg = data["choices"][0]["message"]
    calls = msg.get("tool_calls") or []
    if not calls:
        out["skipped"] = "no initial tool call to recover from"
        return out
    tc = calls[0]
    messages += [
        msg,
        {
            "role": "tool",
            "tool_call_id": tc.get("id"),
            "content": "Error: ENOENT: no such file or directory",
        },
    ]
    r2, elapsed2 = router.call(model, messages, [READ_FILE_TOOL])
    out["status"] = r2.status_code
    out["latency_s"] = round(elapsed2, 2)
    if r2.status_code != 200:
        out["error"] = r2.text[:300]
        return out
    data2 = r2.json()
    msg2 = data2["choices"][0]["message"]
    out["recovered_with_text"] = bool((msg2.get("content") or "").strip())
    out["retried_identical_call"] = bool(msg2.get("tool_calls"))
    out["response_text"] = (msg2.get("content") or "")[:150]
    return out


def check_e_parallel_tool_calls(router: Router, model: str) -> dict:
    """E. One turn, two independent tools offered -- not disqualifying if the
    model calls them sequentially instead, only recorded."""
    out: dict[str, Any] = {}
    r, elapsed = router.call(
        model,
        [
            {
                "role": "user",
                "content": (
                    "Read both /tmp/a.txt and /tmp/b.txt using read_file, in the "
                    "same turn if you can."
                ),
            }
        ],
        [READ_FILE_TOOL],
    )
    out["status"] = r.status_code
    out["latency_s"] = round(elapsed, 2)
    if r.status_code != 200:
        out["error"] = r.text[:300]
        return out
    data = r.json()
    calls = _tool_calls(data)
    out["num_calls_in_one_turn"] = len(calls)
    out["made_parallel_call"] = len(calls) > 1
    return out


def probe_model(router: Router, model: str) -> dict:
    result: dict[str, Any] = {"model": model}
    a = check_a_basic_function_call(router, model)
    result["A_basic_function_call"] = {k: v for k, v in a.items() if not k.startswith("_")}
    if a.get("made_call"):
        b = check_b_tool_result_continuation(router, model, a["_first_call"])
        result["B_tool_result_continuation"] = {k: v for k, v in b.items() if not k.startswith("_")}
    c = check_c_multiturn_loop(router, model)
    result["C_multiturn_loop"] = c
    d = check_d_invalid_tool_recovery(router, model)
    result["D_invalid_tool_recovery"] = d
    e = check_e_parallel_tool_calls(router, model)
    result["E_parallel_tool_calls"] = e
    return result


def main() -> None:
    args = sys.argv[1:]
    models: list[str] = []
    if args and args[0] == "--file":
        with open(args[1]) as f:
            models = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    else:
        models = args
    if not models:
        print(__doc__)
        sys.exit(1)

    router = Router()
    print(f"# base_url={router.base}", file=sys.stderr)
    results = []
    out_path = Path(__file__).with_name("_omniroute_probe_results.json")
    for model in models:
        print(f"# probing {model} ...", file=sys.stderr)
        result = probe_model(router, model)
        results.append(result)
        print(json.dumps(result))
        out_path.write_text(json.dumps(results, indent=2))
    print(f"# wrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
