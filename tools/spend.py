"""Total what the daemon's own sessions have cost.

`delegate.py` tracks only what it spends directly. Sessions dispatched into the
daemon bill through the agent profile and never pass through it, so without this
the visible total is wrong by exactly the amount the product itself uses.

The daemon reports usage but not price: `accumulated_cost` is 0.0 because
9Router returns no cost field, and the top-level `metrics` key is null. Real
figures live under `stats.usage_to_metrics.<service>.accumulated_token_usage`.
Prices below are the same deliberate over-estimate delegate.py uses.
"""

from __future__ import annotations

import json
import sys

import httpx

from agentrt.runtime import daemon

USD_PER_1K_IN = 0.0003
USD_PER_1K_OUT = 0.0012


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
    rows = []
    for item in listing.get("items", []):
        full = httpx.get(
            f"{info.base_url}/api/conversations/{item['id']}",
            headers=headers,
            timeout=60,
        ).json()
        usage = session_usage(full.get("stats"))
        for key in grand:
            grand[key] += usage[key]
        rows.append((item["id"][:8], (item.get("title") or "")[:38], usage))

    for short, title, usage in rows:
        print(
            "%s  in=%-8d out=%-6d cached=%-8d  %s"
            % (short, usage["prompt"], usage["completion"], usage["cache_read"], title)
        )

    usd = grand["prompt"] / 1000 * USD_PER_1K_IN + grand["completion"] / 1000 * USD_PER_1K_OUT
    print("\nsessions: %d" % len(rows))
    print("tokens  : in=%d out=%d cached=%d reasoning=%d"
          % (grand["prompt"], grand["completion"], grand["cache_read"], grand["reasoning"]))
    print("daemon sessions cost ~$%.4f" % usd)

    try:
        with open("tools/spend.json", encoding="utf-8") as fh:
            d = json.load(fh)
        delegated = d["in"] / 1000 * USD_PER_1K_IN + d["out"] / 1000 * USD_PER_1K_OUT
        print("delegate.py cost     ~$%.4f  (%d calls)" % (delegated, d["calls"]))
        print("COMBINED             ~$%.4f" % (usd + delegated))
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
