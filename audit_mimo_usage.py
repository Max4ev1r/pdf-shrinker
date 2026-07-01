#!/usr/bin/env python3
"""Audit local Hermes MiMo Token Plan usage.

The script reconciles two local views:

* agent logs: per-call prompt/output/cache counters when logs are still present
* state.db: persisted per-session counters for the whole selected period

MiMo Credits are estimated only for known MiMo Token Plan rates.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import glob
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo


HERMES_HOME = Path.home() / ".hermes"
TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_CONTROL_CREDITS = 11_000_000_000

# MiMo Token Plan credits per token for mimo-v2.5-pro.
# Input includes cached+uncached tokens in agent logs; cache_read receives the
# cached-input rate and the remaining input receives the uncached-input rate.
RATES = {
    "mimo-v2.5-pro": {
        "input": 300.0,
        "cache_read": 2.5,
        "output": 600.0,
    },
}

API_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ "
    r"\S+ \[(?P<session>[^\]]+)\] agent\.conversation_loop: "
    r"API call #(?P<call>\d+): model=(?P<model>\S+) provider=(?P<provider>\S+) "
    r"in=(?P<input>\d+) out=(?P<output>\d+) total=(?P<total>\d+) "
    r"latency=(?P<latency>[0-9.]+)s"
    r"(?: cache=(?P<cache>\d+)/(?P<cache_base>\d+) \((?P<cache_pct>\d+)%\))?"
)
BG_TURN_RE = re.compile(
    r"\[(?P<session>[^\]]+)\] agent\.turn_context: conversation turn: .*"
    r"msg='Review the conversation above"
)
BG_END_RE = re.compile(
    r"\[(?P<session>[^\]]+)\] run_agent: OpenAI client closed .*thread=bg-review:"
)


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    known_credits: float = 0.0
    unknown_rate_calls: int = 0

    def add_call(self, model: str, input_tokens: int, output_tokens: int, cache_read: int) -> None:
        self.calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_read_tokens += cache_read
        self.known_credits += estimate_credits(model, input_tokens, output_tokens, cache_read)
        if model not in RATES:
            self.unknown_rate_calls += 1

    def add_db_session(
        self,
        model: str,
        calls: int,
        input_tokens: int,
        output_tokens: int,
        cache_read: int,
    ) -> None:
        self.calls += calls
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_read_tokens += cache_read
        self.known_credits += estimate_db_credits(model, input_tokens, output_tokens, cache_read)
        if model not in RATES and calls:
            self.unknown_rate_calls += calls

    @property
    def raw_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def uncached_input_tokens(self) -> int:
        return max(self.input_tokens - self.cache_read_tokens, 0)


def estimate_credits(model: str, input_tokens: int, output_tokens: int, cache_read: int) -> float:
    rates = RATES.get(model)
    if not rates:
        return 0.0
    uncached = max(input_tokens - cache_read, 0)
    return (
        uncached * rates["input"]
        + cache_read * rates["cache_read"]
        + output_tokens * rates["output"]
    )


def estimate_db_credits(model: str, input_tokens: int, output_tokens: int, cache_read: int) -> float:
    rates = RATES.get(model)
    if not rates:
        return 0.0
    # state.db stores cache_read_tokens separately; input_tokens is treated as
    # non-cached input for the persisted-session estimate.
    return (
        input_tokens * rates["input"]
        + cache_read * rates["cache_read"]
        + output_tokens * rates["output"]
    )


def parse_day(value: str) -> dt.datetime:
    return dt.datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=TZ)


def in_window(ts_text: str, start: dt.datetime, end: dt.datetime) -> bool:
    ts = dt.datetime.strptime(ts_text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
    return start <= ts < end


def category_for(session_id: str, bg_active: collections.Counter[str]) -> str:
    if bg_active[session_id] > 0:
        return "background_review"
    if session_id.startswith("cron_"):
        return "cron"
    return "foreground"


def iter_log_paths(log_dir: Path) -> Iterable[Path]:
    for raw in sorted(glob.glob(str(log_dir / "agent.log*"))):
        path = Path(raw)
        if path.is_file():
            yield path


def audit_logs(log_dir: Path, start: dt.datetime, end: dt.datetime):
    by_day: dict[str, Usage] = collections.defaultdict(Usage)
    by_category: dict[str, Usage] = collections.defaultdict(Usage)
    by_session: dict[str, Usage] = collections.defaultdict(Usage)
    low_output = Usage()
    bg_active: collections.Counter[str] = collections.Counter()
    first_ts = None
    last_ts = None
    parsed_calls = 0

    for path in iter_log_paths(log_dir):
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                bg_turn = BG_TURN_RE.search(line)
                if bg_turn:
                    bg_active[bg_turn.group("session")] += 1

                match = API_RE.search(line)
                if match:
                    ts_text = match.group("ts")
                    if in_window(ts_text, start, end):
                        parsed_calls += 1
                        first_ts = min(first_ts, ts_text) if first_ts else ts_text
                        last_ts = max(last_ts, ts_text) if last_ts else ts_text

                        session_id = match.group("session")
                        model = match.group("model")
                        input_tokens = int(match.group("input"))
                        output_tokens = int(match.group("output"))
                        cache_read = int(match.group("cache") or 0)
                        day = ts_text[:10]
                        category = category_for(session_id, bg_active)

                        by_day[day].add_call(model, input_tokens, output_tokens, cache_read)
                        by_category[category].add_call(model, input_tokens, output_tokens, cache_read)
                        by_session[session_id].add_call(model, input_tokens, output_tokens, cache_read)
                        if input_tokens >= 10_000 and output_tokens <= 120:
                            low_output.add_call(model, input_tokens, output_tokens, cache_read)

                bg_end = BG_END_RE.search(line)
                if bg_end:
                    session_id = bg_end.group("session")
                    if bg_active[session_id] > 0:
                        bg_active[session_id] -= 1

    return {
        "parsed_calls": parsed_calls,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "by_day": by_day,
        "by_category": by_category,
        "by_session": by_session,
        "low_output": low_output,
    }


def audit_db(db_path: Path, start: dt.datetime, end: dt.datetime):
    start_epoch = start.timestamp()
    end_epoch = end.timestamp()
    by_source: dict[str, Usage] = collections.defaultdict(Usage)
    by_day: dict[str, Usage] = collections.defaultdict(Usage)

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select
              id,
              source,
              model,
              started_at,
              coalesce(api_call_count, 0) as calls,
              coalesce(input_tokens, 0) as input_tokens,
              coalesce(output_tokens, 0) as output_tokens,
              coalesce(cache_read_tokens, 0) as cache_read_tokens
            from sessions
            where started_at >= ? and started_at < ?
            """,
            (start_epoch, end_epoch),
        ).fetchall()

    for row in rows:
        source = row["source"] or "unknown"
        model = row["model"] or ""
        calls = int(row["calls"] or 0)
        input_tokens = int(row["input_tokens"] or 0)
        output_tokens = int(row["output_tokens"] or 0)
        cache_read = int(row["cache_read_tokens"] or 0)
        day = dt.datetime.fromtimestamp(float(row["started_at"]), TZ).strftime("%Y-%m-%d")
        by_source[source].add_db_session(model, calls, input_tokens, output_tokens, cache_read)
        by_day[day].add_db_session(model, calls, input_tokens, output_tokens, cache_read)

    return {"sessions": len(rows), "by_source": by_source, "by_day": by_day}


def fmt_int(value: float | int) -> str:
    return f"{value:,.0f}"


def print_usage_table(title: str, rows: Iterable[tuple[str, Usage]], limit: int | None = None) -> None:
    rows = list(rows)
    if limit is not None:
        rows = rows[:limit]
    print(f"\n{title}")
    print("-" * len(title))
    print(f"{'name':<28} {'calls':>7} {'input':>14} {'cache':>14} {'output':>10} {'credits':>16}")
    for name, usage in rows:
        print(
            f"{name:<28} {usage.calls:>7} {fmt_int(usage.input_tokens):>14} "
            f"{fmt_int(usage.cache_read_tokens):>14} {fmt_int(usage.output_tokens):>10} "
            f"{fmt_int(usage.known_credits):>16}"
        )


def main() -> None:
    today = dt.datetime.now(TZ).date()
    default_start = today.replace(day=1).isoformat()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", default=str(HERMES_HOME), help="Hermes home directory")
    parser.add_argument("--from", dest="start", default=default_start, help="Start date YYYY-MM-DD, inclusive")
    parser.add_argument("--to", dest="end", default=None, help="End date YYYY-MM-DD, exclusive")
    parser.add_argument(
        "--control-credits",
        type=float,
        default=DEFAULT_CONTROL_CREDITS,
        help="MiMo console Credits for reconciliation",
    )
    parser.add_argument("--top", type=int, default=12, help="Top sessions to print")
    args = parser.parse_args()

    home = Path(args.home).expanduser()
    start = parse_day(args.start)
    end = parse_day(args.end) if args.end else (
        dt.datetime.combine(today + dt.timedelta(days=1), dt.time.min).replace(tzinfo=TZ)
    )

    log_result = audit_logs(home / "logs", start, end)
    db_result = audit_db(home / "state.db", start, end)

    log_total = Usage()
    for usage in log_result["by_day"].values():
        log_total.calls += usage.calls
        log_total.input_tokens += usage.input_tokens
        log_total.output_tokens += usage.output_tokens
        log_total.cache_read_tokens += usage.cache_read_tokens
        log_total.known_credits += usage.known_credits
        log_total.unknown_rate_calls += usage.unknown_rate_calls

    db_total = Usage()
    for usage in db_result["by_source"].values():
        db_total.calls += usage.calls
        db_total.input_tokens += usage.input_tokens
        db_total.output_tokens += usage.output_tokens
        db_total.cache_read_tokens += usage.cache_read_tokens
        db_total.known_credits += usage.known_credits
        db_total.unknown_rate_calls += usage.unknown_rate_calls

    print("Hermes MiMo Token Plan usage audit")
    print(f"Window: {start.date()} <= usage < {end.date()} ({TZ.key})")
    print("Known rate: mimo-v2.5-pro input=300, cache_read=2.5, output=600 Credits/token")
    print()
    print(f"Agent-log exact calls parsed: {log_result['parsed_calls']:,}")
    print(f"Agent-log coverage: {log_result['first_ts'] or 'none'} .. {log_result['last_ts'] or 'none'}")
    print(f"Agent-log known Credits: {fmt_int(log_total.known_credits)}")
    print(f"state.db sessions: {db_result['sessions']:,}")
    print(f"state.db estimated Credits: {fmt_int(db_total.known_credits)}")
    if args.control_credits:
        print(f"MiMo console Credits: {fmt_int(args.control_credits)}")
        print(f"Unexplained vs agent logs: {fmt_int(args.control_credits - log_total.known_credits)}")
        print(f"Unexplained vs state.db: {fmt_int(args.control_credits - db_total.known_credits)}")

    print_usage_table(
        "Agent logs by category",
        sorted(log_result["by_category"].items(), key=lambda kv: kv[1].known_credits, reverse=True),
    )
    print_usage_table(
        "Agent logs by day",
        sorted(log_result["by_day"].items()),
    )
    print_usage_table(
        "state.db by source",
        sorted(db_result["by_source"].items(), key=lambda kv: kv[1].known_credits, reverse=True),
    )
    print_usage_table(
        "Top agent-log sessions by Credits",
        sorted(log_result["by_session"].items(), key=lambda kv: kv[1].known_credits, reverse=True),
        limit=args.top,
    )
    print_usage_table("Low-output high-input calls", [("input>=10k output<=120", log_result["low_output"])])

    if log_total.unknown_rate_calls or db_total.unknown_rate_calls:
        print()
        print(
            "Warning: unknown-rate calls were excluded from Credits totals "
            f"(logs={log_total.unknown_rate_calls}, db={db_total.unknown_rate_calls})."
        )


if __name__ == "__main__":
    main()
