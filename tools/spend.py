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
from datetime import datetime, timedelta, timezone

import httpx

from agentrt.runtime import daemon


#: The lifetime cache-hit figure is dominated by whatever the store has
#: accumulated, so it reads as "caching is broken" long after it was fixed: the
#: reasoning-content resend bug (H8 item 1) left hundreds of sessions at a 0%
#: hit rate, and they sit in the same total as healthy ones. The report
#: therefore prints a second figure over sessions created in this window, which
#: is the one that describes the code as it is now.
RECENT_WINDOW_DAYS = 1


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

#: The ledger and the live records both use the full field names; a report
#: must not be right for one and silently zero for the other.
TOKEN_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)


def rates_for(model: str | None) -> dict | None:
    """The rate card for a model, or None when the table does not cover it."""
    if not model:
        return None
    name = model.casefold()
    # Exact, after dropping a provider-routing prefix: `openai/deepseek-flash`
    # is the same billed model as `deepseek-flash`, but `deepseek-flash-lite`
    # is not, and charging it the flash rate would be a guess.
    leaf = name.rsplit("/", 1)[-1]
    for key, card in PRICE_TABLE.items():
        if leaf == key:
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
                    bucket[field] += call.get(field) or 0
            continue
        # Older records only carry the accumulated bucket, with no model.
        usage = service.get("accumulated_token_usage") or {}
        bucket = totals.setdefault("unknown", dict.fromkeys(TOKEN_FIELDS, 0))
        for field in TOKEN_FIELDS:
            bucket[field] += usage.get(field) or 0
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
    prompt = usage.get("prompt_tokens", 0)
    cache_read = usage.get("cache_read_tokens", 0)
    # ``prompt`` includes anything served from cache, so the fresh portion is
    # the difference. Clamped at zero for a provider that reports them
    # separately rather than inclusively.
    fresh = max(prompt - cache_read, 0)
    return (
        fresh / 1_000_000 * card["cache_miss"]
        + cache_read / 1_000_000 * card["cache_hit"]
        + usage.get("completion_tokens", 0) / 1_000_000 * card["output"],
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

    grand = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cache_read_tokens": 0,
        "reasoning_tokens": 0,
    }
    sessions = 0.0
    unpriced_grand: dict[str, int] = dict.fromkeys(TOKEN_FIELDS, 0)
    unpriced_models: set[str] = set()
    recent = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cache_read_tokens": 0,
        "reasoning_tokens": 0,
        "sessions": 0,
    }
    cutoff = datetime.now(timezone.utc) - timedelta(days=RECENT_WINDOW_DAYS)

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
        for field in (
            "prompt_tokens",
            "completion_tokens",
            "cache_read_tokens",
            "reasoning_tokens",
        ):
            grand[field] += session[field]
        usd, unpriced, models = price_session(stats)
        sessions += usd
        unpriced_models |= models
        for field in TOKEN_FIELDS:
            unpriced_grand[field] += unpriced[field]
        marker = f" (unpriced: {','.join(sorted(models))})" if models else ""
        if _created_within(item.get("created_at"), cutoff):
            recent["sessions"] += 1
            for field in (
                "prompt_tokens",
                "completion_tokens",
                "cache_read_tokens",
                "reasoning_tokens",
            ):
                recent[field] += session[field]
        fresh = max(session["prompt_tokens"] - session["cache_read_tokens"], 0)
        title = (item.get("title") or "")[:28]
        print(
            f"{item['id'][:8]}  fresh={fresh:<7d} "
            f"cached={session['cache_read_tokens']:<8d} "
            f"out={session['completion_tokens']:<6d} ~${usd:.4f}  {title}{marker}"
        )

    hit_rate = (
        grand["cache_read_tokens"] / grand["prompt_tokens"] * 100
        if grand["prompt_tokens"]
        else 0
    )
    fresh = max(grand["prompt_tokens"] - grand["cache_read_tokens"], 0)
    print(
        f"\ntokens : fresh={fresh} cached={grand['cache_read_tokens']} "
        f"({hit_rate:.0f}% hit) out={grand['completion_tokens']} "
        f"reasoning={grand['reasoning_tokens']}"
    )
    # Say which number describes the code as it is now. The lifetime figure is
    # a ledger: it keeps sessions that ran before a caching fix, so it stays low
    # long after the fix and is not evidence about the current build.
    if recent["sessions"]:
        recent_rate = (
            recent["cache_read_tokens"] / recent["prompt_tokens"] * 100
            if recent["prompt_tokens"]
            else 0
        )
        recent_fresh = max(recent["prompt_tokens"] - recent["cache_read_tokens"], 0)
        print(
            f"        lifetime and predate any fix; the last "
            f"{RECENT_WINDOW_DAYS}d ({recent['sessions']} sessions) is "
            f"fresh={recent_fresh} cached={recent['cache_read_tokens']} "
            f"({recent_rate:.0f}% hit)"
        )
    print(f"daemon sessions ~${sessions:.4f}")
    if unpriced_models:
        print(
            f"                 {sum(unpriced_grand.values())} tokens at an "
            f"unknown rate: {', '.join(sorted(unpriced_models))}"
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
    unarchived = len(archive.get("unarchived") or [])
    if unarchived:
        print(
            f"                 {unarchived} deleted session(s) could not be "
            "archived; the lifetime total is short by them"
        )
    unpriced_note = (
        f"; {archived_unpriced} tokens at an unknown rate" if archived_unpriced else ""
    )
    print(
        f"archived       ~${archived_total:.4f}  "
        f"({archive.get('sessions', 0)} deleted sessions{unpriced_note})"
    )

    try:
        with open("tools/spend.json", encoding="utf-8") as fh:
            d = json.load(fh)
        # delegate.py makes single-shot calls with no cache reuse between them.
        delegated, _ = cost_for(
            d.get("model", "deepseek-flash"),
            {"prompt_tokens": d["in"], "completion_tokens": d["out"]},
        )
        print(f"delegate.py    ~${delegated:.4f}  ({d['calls']} calls)")
        print(f"COMBINED       ~${sessions + archived_total + delegated:.4f}")
    except OSError:
        print(f"COMBINED       ~${sessions + archived_total:.4f}")

    versions = ", ".join(
        f"{name}@{card['version']}" for name, card in PRICE_TABLE.items()
    )
    print(
        f"\nEstimate only. Prices: {versions}. These are published rates applied "
        "to reported tokens, not provider-confirmed charges."
    )
    print("A model missing from the table is reported as a token count, not a price.")
    return 0


def _created_within(value: object, cutoff: datetime) -> bool:
    """Was this session created at or after ``cutoff``?

    An unparsable or absent timestamp counts as *not* recent: the recent figure
    is the one that claims to describe the current build, so it should not
    absorb sessions whose age is unknown.
    """
    if not isinstance(value, str) or not value:
        return False
    try:
        created = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return created >= cutoff


if __name__ == "__main__":
    raise SystemExit(main())
