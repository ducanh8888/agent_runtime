#!/usr/bin/env python3
"""Report what AgentRT sessions have cost, and what has been archived.

Two things here are deliberately separate from a bill:

* the rate table is versioned, and a model that is not in it is reported as
  **unpriced** rather than being charged at another model's rate -- a made-up
  price is worse than an honest gap;
* archived totals are sessions that have already been deleted. They are folded
  into a lifetime ledger before the directory goes, because a total measured
  from the conversation store falls when sessions are removed and the money does
  not come back.

Nothing here is provider-confirmed. Rates are published rates, the tokens are
whatever the provider reported, and the two are combined into an estimate. A
provider-confirmed charge, if one is ever recorded, is a different number and is
never conflated with this one.
"""

from __future__ import annotations

import json
import sys

import httpx

from agentrt.runtime import daemon


#: Versioned rate cards, USD per million tokens. Each card records where the
#: rate came from and which period it applies to, so a figure can say which
#: prices it used.
PRICE_TABLE: dict[str, dict] = {
    "deepseek-flash": {
        "version": "deepseek-2026-09",
        "source": "https://api-docs.deepseek.com/quick_start/pricing/",
        "cache_miss": 0.28,
        "cache_hit": 0.028,
        "output": 0.42,
    },
}

TOKEN_FIELDS = ("prompt", "completion", "cache_read", "cache_write", "reasoning")


def rates_for(model: str | None) -> dict | None:
    """The rate card for a model, or None when the table does not cover it."""
    if not model:
        return None
    name = model.casefold()
    for key, card in PRICE_TABLE.items():
        if key in name:
            return card
    return None


def usage_by_model(stats: dict | None) -> dict[str, dict[str, int]]:
    """Per-model token totals, summed over every service a session used.

    Per call rather than per accumulated bucket, because the model is recorded
    on the call: pricing a session at one model's rate when it used two is the
    error this exists to avoid.
    """
    totals: dict[str, dict[str, int]] = {}
    for service in (stats or {}).get("usage_to_metrics", {}).values():
        calls = service.get("token_usages") or []
        if calls:
            for call in calls:
                model = call.get("model") or "unknown"
                bucket = totals.setdefault(model, dict.fromkeys(TOKEN_FIELDS, 0))
                for field in TOKEN_FIELDS:
                    bucket[field] += call.get(f"{field}_tokens") or 0
            continue
        # Older records only carry the accumulated bucket, with no model.
        usage = service.get("accumulated_token_usage") or {}
        bucket = totals.setdefault("unknown", dict.fromkeys(TOKEN_FIELDS, 0))
        for field in TOKEN_FIELDS:
            bucket[field] += usage.get(f"{field}_tokens") or 0
    return totals


def cost_for(model: str | None, usage: dict[str, int]) -> tuple[float, bool]:
    """Price one model's usage. Returns ``(usd, priced)``.

    ``priced`` is False when the table does not cover the model; the caller
    reports those tokens separately instead of folding them into a total that
    would look complete.
    """
    card = rates_for(model)
    if card is None:
        return 0.0, False
    prompt = usage.get("prompt", 0)
    cache_read = usage.get("cache_read", 0)
    # ``prompt`` includes anything served from cache, so the fresh portion is
    # the difference. Clamped at zero for a provider that reports them
    # separately rather than inclusively.
    fresh = max(prompt - cache_read, 0)
    return (
        fresh / 1_000_000 * card["cache_miss"]
        + cache_read / 1_000_000 * card["cache_hit"]
        + usage.get("completion", 0) / 1_000_000 * card["output"],
        True,
    )


def price_session(stats: dict | None) -> tuple[float, dict[str, int], set[str]]:
    """Total cost, unpriced token totals and the unpriced model names."""
    total = 0.0
    unpriced: dict[str, int] = dict.fromkeys(TOKEN_FIELDS, 0)
    unpriced_models: set[str] = set()
    for model, usage in usage_by_model(stats).items():
        usd, priced = cost_for(model, usage)
        if priced:
            total += usd
        else:
            unpriced_models.add(model)
            for field in TOKEN_FIELDS:
                unpriced[field] += usage.get(field, 0)
    return total, unpriced, unpriced_models


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
    sessions = 0.0
    unpriced_grand: dict[str, int] = dict.fromkeys(TOKEN_FIELDS, 0)
    unpriced_models: set[str] = set()

    for item in listing.get("items", []):
        full = httpx.get(
            f"{info.base_url}/api/conversations/{item['id']}",
            headers=headers,
            timeout=60,
        ).json()
        stats = full.get("stats")
        by_model = usage_by_model(stats)
        session = dict.fromkeys(TOKEN_FIELDS, 0)
        for usage in by_model.values():
            for field in TOKEN_FIELDS:
                session[field] += usage.get(field, 0)
        for field in ("prompt", "completion", "cache_read", "reasoning"):
            grand[field] += session[field]
        usd, unpriced, models = price_session(stats)
        sessions += usd
        unpriced_models |= models
        for field in TOKEN_FIELDS:
            unpriced_grand[field] += unpriced[field]
        marker = " (unpriced: %s)" % ",".join(sorted(models)) if models else ""
        print(
            "%s  fresh=%-7d cached=%-8d out=%-6d ~$%.4f  %s%s"
            % (
                item["id"][:8],
                max(session["prompt"] - session["cache_read"], 0),
                session["cache_read"],
                session["completion"],
                usd,
                (item.get("title") or "")[:28],
                marker,
            )
        )

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
    if unpriced_models:
        print(
            "                 %d tokens at an unknown rate: %s"
            % (
                sum(unpriced_grand.values()),
                ", ".join(sorted(unpriced_models)),
            )
        )

    archive = httpx.get(
        f"{info.base_url}/api/conversations/spend-archive",
        headers=headers,
        timeout=60,
    ).json()
    archived_total = 0.0
    archived_unpriced = 0
    for model, usage in (archive.get("by_model") or {}).items():
        usd, priced = cost_for(model, usage)
        if priced:
            archived_total += usd
        else:
            archived_unpriced += sum(usage.values())
    print(
        "archived       ~$%.4f  (%d deleted sessions%s)"
        % (
            archived_total,
            archive.get("sessions", 0),
            (
                "; %d tokens at an unknown rate" % archived_unpriced
                if archived_unpriced
                else ""
            ),
        )
    )

    try:
        with open("tools/spend.json", encoding="utf-8") as fh:
            d = json.load(fh)
        # delegate.py makes single-shot calls with no cache reuse between them.
        delegated, _ = cost_for(
            d.get("model", "deepseek-flash"),
            {"prompt": d["in"], "completion": d["out"]},
        )
        print("delegate.py    ~$%.4f  (%d calls)" % (delegated, d["calls"]))
        print("COMBINED       ~$%.4f" % (sessions + archived_total + delegated))
    except OSError:
        print("COMBINED       ~$%.4f" % (sessions + archived_total))

    versions = ", ".join(
        f"{name}@{card['version']}" for name, card in PRICE_TABLE.items()
    )
    print(
        "\nEstimate only. Prices: %s. These are published rates applied to "
        "reported tokens, not provider-confirmed charges." % versions
    )
    print("A model missing from the table is reported as a token count, not a price.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
