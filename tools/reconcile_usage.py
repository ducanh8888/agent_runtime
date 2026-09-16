#!/usr/bin/env python3
"""Compare the daemon's own usage records against a provider billing export.

Costs nothing: it reads the daemon and some CSV files, and dispatches nothing.

Why one comparison and not the other. The export is per *account key*, and on
this deployment the orchestrator uses the same key as the daemon -- so a key's
prompt totals mix both, and comparing the daemon's prompt total against the
bill's proves nothing. What survives is the **miss count**: a daemon call is a
subset of the key's calls, so the daemon cannot have billed more misses than the
key did. That is the check here, and it is the one that settled a real question
on 2026-09-17 (see docs/research/friction-log.md): the daemon recorded 26.7M
misses for a day the whole account billed ~2.2M.

Usage:
    python3 tools/reconcile_usage.py ~/Downloads/usage_data_2026-09-16_2026-09-17
Exit code 0 when the daemon's misses fit inside the bill, 1 when they cannot.

Two things to read before acting on a failure. The daemon figures are a **lower
bound**: `list_sessions` is capped at 100 items, so an old period may have
sessions the listing no longer reaches. That weakens the totals but not the
check -- a lower bound that already exceeds the bill is still a contradiction.
And an export from **before** the cache recording improved will legitimately
fail, for the same reason `probe_cache.py`'s default window is short. Check the
period before concluding the current build is at fault.
"""

from __future__ import annotations

import collections
import csv
import sys
from pathlib import Path

from agentrt.runtime.client import Client

#: The export writes one row per (period, key, model, metric).
_AMOUNT_GLOB = "amount-*.csv"
_COST_GLOB = "cost-*.csv"


def _read_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _billed(export_dir: Path) -> tuple[dict, float]:
    """Per (period, key, model) token totals, and the total billed cost."""
    totals: dict[tuple[str, str, str], dict[str, int]] = collections.defaultdict(
        lambda: collections.defaultdict(int)
    )
    for path in sorted(export_dir.glob(_AMOUNT_GLOB)):
        for row in _read_rows(path):
            key = (row["start_time_iso"][:10], row["api_key_name"], row["model"])
            metric = row["type"]
            if metric == "request_count":
                totals[key]["requests"] += int(row["amount"])
            elif row["price"]:
                totals[key][metric] += int(row["amount"])
    cost = 0.0
    for path in sorted(export_dir.glob(_COST_GLOB)):
        for row in _read_rows(path):
            cost += float(row["cost"])
    return dict(totals), cost


def _daemon_totals(client: Client, periods: set[str]) -> dict[str, dict[str, int]]:
    """The daemon's own records for sessions created on each period."""
    out: dict[str, dict[str, int]] = collections.defaultdict(
        lambda: collections.defaultdict(int)
    )
    for item in client.list_sessions(limit=100):
        created = str(item.get("created_at") or "")
        if created[:10] not in periods:
            continue
        try:
            usage = client.usage(item["id"])
        except Exception:  # noqa: BLE001 -- a deleted session is not a failure
            continue
        for service in usage.get("services") or []:
            accumulated = service.get("accumulated_token_usage") or {}
            out[created[:10]]["prompt"] += accumulated.get("prompt_tokens", 0)
            out[created[:10]]["hit"] += accumulated.get("cache_read_tokens", 0)
    for day, totals in out.items():
        totals["miss"] = max(totals["prompt"] - totals["hit"], 0)
    return dict(out)


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    export_dir = Path(sys.argv[1]).expanduser()
    if not export_dir.is_dir():
        print(f"not a directory: {export_dir}")
        return 2

    totals, cost = _billed(export_dir)
    periods = {key[0] for key in totals}

    print(f"provider bill, {export_dir}")
    print(
        f"  {'period':12} {'key':22} {'model':20} {'requests':>9} {'prompt':>13} {'hit':>13} {'miss':>10} {'hit%':>6}"
    )
    billed_miss: dict[str, int] = collections.defaultdict(int)
    for (period, key, model), values in sorted(totals.items()):
        hit = values.get("input_cache_hit_tokens", 0)
        miss = values.get("input_cache_miss_tokens", 0)
        total = hit + miss
        billed_miss[period] += miss
        rate = f"{hit / total * 100:.1f}" if total else "-"
        print(
            f"  {period:12} {key:22} {model:20} {values.get('requests', 0):>9} "
            f"{total:>13,} {hit:>13,} {miss:>10,} {rate:>6}"
        )
    print(f"  billed cost: ${cost:.4f}")

    daemon = _daemon_totals(Client(), periods)
    print(
        "\nthe daemon's own records, for sessions created in those periods"
        "\n(a lower bound: the session listing stops at 100 items)"
    )
    print(f"  {'period':12} {'prompt':>13} {'hit':>13} {'miss':>10}")
    for period in sorted(periods):
        values = daemon.get(period)
        if not values:
            print(f"  {period:12} {'(no sessions recorded)':>37}")
            continue
        print(
            f"  {period:12} {values['prompt']:>13,} {values['hit']:>13,} "
            f"{values['miss']:>10,}"
        )

    print(
        "\nOnly the miss counts are comparable: the export is per account key, and\n"
        "an orchestrator sharing that key contributes prompt tokens the daemon\n"
        "never recorded. A daemon call is a subset of the key's calls, though, so\n"
        "the daemon's misses must fit inside the bill's."
    )
    contradictions = [
        period
        for period, values in daemon.items()
        if values["miss"] > billed_miss.get(period, 0)
    ]
    if contradictions:
        print("\nCONTRADICTION -- the daemon recorded more misses than were billed:")
        for period in sorted(contradictions):
            print(
                f"  {period}: daemon {daemon[period]['miss']:,} vs "
                f"billed {billed_miss.get(period, 0):,}"
            )
        print(
            "\nThat cannot happen if the daemon is recording what the provider\n"
            "reported. It means the recorded cache figures are wrong for that\n"
            "period -- see the friction log's cache entry for what that looked\n"
            "like the last time, and check whether the daemon in use is the\n"
            "build you think it is."
        )
        return 1

    print("\nPASS -- the daemon's recorded misses fit inside what was billed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
