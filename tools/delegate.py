"""Delegate a coding task to ds/deepseek-v4-flash through 9Router.

Claude specifies and reviews; DeepSeek writes. The generated code is written
straight to disk and only a summary comes back, so the orchestrator's context
is not spent on code it is about to test anyway.

    python delegate.py --spec spec.md --out path/to/file.py [--context a.py b.py]

Running total of spend is kept in spend.json next to this script.
"""

import argparse
import io
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ENV = r"C:\Users\ADMIN\Desktop\ORCHESTRATOR\agent_runtime\.env"
SPEND = os.path.join(HERE, "spend.json")

# ds/deepseek-v4-flash pricing is not published through the router, so this is
# a deliberate over-estimate: DeepSeek's own flash tier is well under this.
USD_PER_1K_IN = 0.0003
USD_PER_1K_OUT = 0.0012


def load_env():
    cfg = {}
    for line in io.open(ENV, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


def record(usage):
    total = {"in": 0, "out": 0, "calls": 0}
    if os.path.exists(SPEND):
        total = json.load(open(SPEND))
    total["in"] += usage[0]
    total["out"] += usage[1]
    total["calls"] += 1
    json.dump(total, open(SPEND, "w"), indent=2)
    usd = total["in"] / 1000 * USD_PER_1K_IN + total["out"] / 1000 * USD_PER_1K_OUT
    return total, usd


SYSTEM = """You are a senior Python engineer writing production code for a local
agent runtime. You will be given a precise specification and sometimes reference
excerpts from the surrounding codebase.

Rules:
- Output ONLY the file content. No markdown fences, no commentary, no preamble.
- Target Python 3.13. Use modern typing (X | None, not Optional[X]).
- Follow the specification exactly. Do not invent extra features.
- The code must run on both Windows and Linux.
- Prefer the standard library. Only import third-party packages the spec names.
- Write real docstrings that explain why, not what.
- No TODO comments and no placeholder implementations."""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--context", nargs="*", default=[])
    ap.add_argument("--max-tokens", type=int, default=32000)
    args = ap.parse_args()

    cfg = load_env()
    from openai import OpenAI

    client = OpenAI(
        api_key=cfg["AGENTRT_9ROUTER_API_KEY"],
        base_url=cfg["AGENTRT_9ROUTER_BASE_URL"].rstrip("/"),
        timeout=600,
    )

    spec = io.open(args.spec, encoding="utf-8").read()
    parts = [spec]
    for c in args.context:
        body = io.open(c, encoding="utf-8", errors="replace").read()
        parts.append("\n\n=== reference: %s ===\n%s" % (os.path.basename(c), body))
    prompt = "".join(parts)

    t0 = time.time()
    # Streaming, because the router only reports usage reliably that way and a
    # long file can otherwise sit silent past a proxy timeout.
    stream = client.chat.completions.create(
        model=cfg["AGENTRT_DEFAULT_MODEL"],
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ],
        max_tokens=args.max_tokens,
        stream=True,
        stream_options={"include_usage": True},
    )
    # The model thinks in `reasoning_content` before answering in `content`.
    # Both are billed as completion tokens, so a budget that looks generous can
    # be spent entirely on thinking and return an empty file.
    body = ""
    reasoning_chars = 0
    finish = None
    usage_in = usage_out = 0
    for chunk in stream:
        if chunk.usage:
            usage_in = chunk.usage.prompt_tokens or 0
            usage_out = chunk.usage.completion_tokens or 0
        if not chunk.choices:
            continue
        choice = chunk.choices[0]
        if choice.finish_reason:
            finish = choice.finish_reason
        delta = choice.delta
        if delta.content:
            body += delta.content
        rc = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
        if rc:
            reasoning_chars += len(rc)

    # Models add fences despite instructions often enough to be worth stripping.
    body = body.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
    body = body.strip() + "\n"

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    io.open(args.out, "w", encoding="utf-8", newline="\n").write(body)

    total, usd = record((usage_in, usage_out))
    lines = body.count("\n")
    print("wrote %s  (%d lines, %d bytes)" % (args.out, lines, len(body)))
    print("tokens in=%d out=%d (thinking ~%d chars)  finish=%s  %.1fs"
          % (usage_in, usage_out, reasoning_chars, finish, time.time() - t0))
    if finish == "length":
        print("WARNING: hit the token ceiling; the file is probably truncated")
    if not body.strip():
        print("ERROR: model returned no content, only reasoning")
        sys.exit(2)
    print("running total: %d calls, ~$%.4f" % (total["calls"], usd))

    try:
        import ast

        ast.parse(body)
        print("syntax: OK")
    except SyntaxError as e:
        print("syntax: FAILED line %s: %s" % (e.lineno, e.msg))
        sys.exit(1)


if __name__ == "__main__":
    main()
