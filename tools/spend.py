"""Total what the daemon's own sessions have cost.

`delegate.py` tracks only what it spends directly. Sessions dispatched into the
daemon bill through the agent profile and never pass through it, so without this
the visible total is wrong by exactly the amount the product itself uses.

The daemon reports usage but not price. `accumulated_cost` is 0.0 and
`metrics` is null; real figures live under
`stats.usage_to_metrics.<service>.accumulated_token_usage`. 9Router's /models
endpoint publishes capabilities but no prices, and /pricing and /model/info do
not exist, so cost here is an estimate from published DeepSeek rates and
nothing more.

CACHING IS THE WHOLE GAME. An agent session re-sends its transcript every turn,
and nearly all of it is a cache hit -- in one measured session, 1,983,744 of
2,134,026 prompt tokens. Cache reads bill at roughly a tenth of fresh input, so
pricing prompt tokens at one flat rate overstates a long session several times
over and makes an agentic loop look far more expensive than it is.
"""

from __future__ import annotations

import json
import sys

import httpx

from agentrt.runtime import daemon

# USD per million tokens. DeepSeek's published rates; 9Router serves the model
# but does not publish its own, so treat these as an upper bound rather than a
# bill. Change them here if the real rate is known.
USD_PER_M_CACHE_MISS = 0.28
USD_PER_M_CACHE_HIT = 0.028
USD_PER_M_OUTPUT = 0.42


def session_usage(stats: dict | None) -> dict[str, int]:
    """Sum token usage across every LLM service a session used."""
    total = {"prompt": 0, "completion": 0, "cache_read": 0, "reasoning": 0}
    for service in (stats or {}).get("usage_to_metrics", {}).values():
        usage = service.get("accumulated_token_usage") or {}
        total["prompt"] += usage.get("prompt_tokens") or 0
        total["completion"] += usage.get("completion_tokens") or 0
        total["cache_read"] += usage.get("cache_read_tokens") or 0
        total["reasoning"] += usage.get("reasoning_tokens") or 0
    return total


def cost(prompt: int, cache_read: int, completion: int) -> float:
    """Price usage, charging cache reads at the cache rate.

    ``prompt_tokens`` is the whole prompt including anything served from cache,
    so the fresh portion is the difference. Clamped at zero: a provider that
    reports them separately rather than inclusively would otherwise produce a
    negative charge.
    """
    fresh = max(prompt - cache_read, 0)
    return (
        fresh / 1_000_000 * USD_PER_M_CACHE_MISS
        + cache_read / 1_000_000 * USD_PER_M_CACHE_HIT
        + completion / 1_000_000 * USD_PER_M_OUTPUT
    )


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    info = daemon.ensure_running()
    headers = {"X-Session-API-Key": info.token}

    listing = httpx.get(
        f"{info.base_url}/api/conversations/search",
        headers=headers,
        params={"limit": 100},
        timeout=60,
    ).json()

    grand = {"prompt": 0, "completion": 0, "cache_read": 0, "reasoning": 0}
    for item in listing.get("items", []):
        full = httpx.get(
            f"{info.base_url}/api/conversations/{item['id']}",
            headers=headers,
            timeout=60,
        ).json()
        usage = session_usage(full.get("stats"))
        for key in grand:
            grand[key] += usage[key]
        print(
            "%s  fresh=%-7d cached=%-8d out=%-6d ~$%.4f  %s"
            % (
                item["id"][:8],
                max(usage["prompt"] - usage["cache_read"], 0),
                usage["cache_read"],
                usage["completion"],
                cost(usage["prompt"], usage["cache_read"], usage["completion"]),
                (item.get("title") or "")[:34],
            )
        )

    sessions = cost(grand["prompt"], grand["cache_read"], grand["completion"])
    hit_rate = grand["cache_read"] / grand["prompt"] * 100 if grand["prompt"] else 0
    print(
        "\ntokens : fresh=%d cached=%d (%.0f%% hit) out=%d reasoning=%d"
        % (
            max(grand["prompt"] - grand["cache_read"], 0),
            grand["cache_read"],
            hit_rate,
            grand["completion"],
            grand["reasoning"],
        )
    )
    print("daemon sessions ~$%.4f" % sessions)

    try:
        with open("tools/spend.json", encoding="utf-8") as fh:
            d = json.load(fh)
        # delegate.py makes single-shot calls with no cache reuse between them.
        delegated = cost(d["in"], 0, d["out"])
        print("delegate.py     ~$%.4f  (%d calls)" % (delegated, d["calls"]))
        print("COMBINED        ~$%.4f" % (sessions + delegated))
    except OSError:
        pass

    print("\nEstimate only: 9Router publishes no prices. Rates assumed are")
    print("$%.3f/M fresh input, $%.3f/M cached, $%.2f/M output."
          % (USD_PER_M_CACHE_MISS, USD_PER_M_CACHE_HIT, USD_PER_M_OUTPUT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
