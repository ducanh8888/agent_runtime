#!/usr/bin/env python3
"""Does prompt caching still work, session by session?

Costs nothing: it reads the daemon's own usage records and dispatches no work.

Why this exists as a check rather than a note. Prompt caching failed silently
for weeks and the failure was expensive: on this provider the cached portion is
roughly an order of magnitude cheaper, and sessions across three days ran at a
0% hit rate -- hundreds of millions of prompt tokens paid in full -- while every
test passed and nothing in the product said so. The cause was the
reasoning-content resend bug (H8 item 1, `39b89ea`): prior assistant tool-call
turns were rebuilt without their `reasoning_content`, so the prompt differed
from the one before it on every call and the provider's prefix cache could
never hit. Fixed, the same sessions report 0.86-0.97.

A cache fail has no symptom other than the bill, so the guard is here.

Thresholds, and why they are where they are: a healthy session on this
deployment measures 0.86-0.97, and a broken one measures 0.00, so the gap is
wide and the boundary can be generous. A session with fewer than three provider
calls is skipped -- its first call cannot hit, and a two-call session is a cold
start rather than a defect.

Usage:  python3 tools/probe_cache.py [--days N] [--all]
Exit code 0 when nothing in the window looks broken, 1 when something does.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

from agentrt.runtime.client import Client

#: A call count below this cannot tell a cold start from a defect.
MIN_CALLS = 3
#: Below this, a session is broken rather than merely cold.
BROKEN = 0.10
#: Below this, it is worth looking at even though it is not the known failure.
SUSPECT = 0.50


def _created(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _session_calls_and_rates(usage: dict) -> tuple[int, list[float]]:
    calls = 0
    rates: list[float] = []
    for service in usage.get("services") or []:
        if not isinstance(service, dict):
            continue
        calls += len(service.get("calls") or [])
        rate = service.get("cache_hit_rate")
        if rate is not None:
            rates.append(float(rate))
    return calls, rates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days",
        type=float,
        default=0.5,
        help=(
            "how far back to look (default 0.5 days, i.e. the last 12 hours). "
            "The window is deliberately short: the question this guard answers "
            "is whether the *current* code caches, and this deployment's "
            "history contains a long stretch of sessions that legitimately "
            "scored 0.00 before H8 item 1 was fixed. Widen it (or use --all) to "
            "study that history -- expect the old breakage to be reported, and "
            "read the timestamps rather than concluding the fix has regressed."
        ),
    )
    parser.add_argument(
        "--all", action="store_true", help="ignore --days and inspect everything"
    )
    args = parser.parse_args()

    client = Client()
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)

    checked = 0
    skipped_cold = 0
    skipped_unknown = 0
    broken: list[str] = []
    suspect: list[str] = []

    # 100 is the server's ceiling: a larger limit is rejected rather than
    # clamped (`?limit=101` is a 500), so this asks for exactly what it can get.
    for item in client.list_sessions(limit=100):
        created = _created(item.get("created_at"))
        if not args.all:
            if created is None or created < cutoff:
                continue
        short = item.get("short_id") or item.get("id", "")[:8]
        try:
            usage = client.usage(item["id"])
        except Exception as exc:  # noqa: BLE001 -- a deleted session is not a fail
            print(f"  {short}: usage unavailable ({type(exc).__name__})")
            continue

        calls, rates = _session_calls_and_rates(usage)
        if not rates:
            # The provider reported no cache fields at all: an older daemon, or
            # a session that never completed a call. Not evidence either way.
            skipped_unknown += 1
            continue
        if calls < MIN_CALLS:
            skipped_cold += 1
            continue

        checked += 1
        rate = sum(rates) / len(rates)
        line = (
            f"  {short}  calls={calls:<4} hit_rate={rate:.2f}  "
            f"{(item.get('title') or '')[:40]}"
        )
        if rate <= BROKEN:
            broken.append(line)
        elif rate < SUSPECT:
            suspect.append(line)

    print(f"\nchecked {checked} session(s) with >={MIN_CALLS} provider calls")
    print(f"  skipped {skipped_cold} too few calls to judge (cold start)")
    print(f"  skipped {skipped_unknown} with no provider cache figures")

    if suspect:
        print("\nSUSPECT -- caching is working poorly, not absent:")
        for line in suspect:
            print(line)
    if broken:
        print("\nBROKEN -- these look like the resend bug:")
        for line in broken:
            print(line)
        print(
            "\nA near-zero hit rate on a session with many calls means the "
            "prompt prefix changes between calls. Check what is being rebuilt "
            "into the history -- H8 item 1 in the hardening plan is the "
            "worked example, including what the fix was."
        )
        return 1

    if not checked:
        print("\nNothing in the window had enough calls to judge.")
        print("PASS (vacuously)")
        return 0

    print("\nPASS -- every judged session is caching")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
