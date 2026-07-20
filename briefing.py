#!/usr/bin/env python3
"""
Daily pre-market briefing generator + Telegram publisher (Ollama edition).

Pipeline:
  1. Web search (DuckDuckGo, free/no key) for XAUUSD, US100, SP500, EURUSD
     catalysts + this week's economic calendar.
  2. Research call to a local Ollama model -> English draft synthesizing the
     search results, with sources cited and confirmed-vs-analysis labeled.
  3. Structuring call to Ollama (JSON-constrained) -> Arabic messages, one
     per instrument plus a summary, Telegram-ready, tickers/terms in English.
  4. Post each message to a Telegram chat/channel, in order.

Requires a running Ollama server reachable at OLLAMA_HOST (defaults to
http://localhost:11434) with OLLAMA_MODEL already pulled, e.g.:
    ollama pull qwen2.5:14b

Run manually:  python scripts/briefing.py --force
Run on schedule: a cron job or a GitHub Actions self-hosted runner on the
same machine as Ollama (cloud runners can't reach your local Ollama server).
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from ddgs import DDGS

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:14b")
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", "")
AMSTERDAM = ZoneInfo("Europe/Amsterdam")
INSTRUMENT_ORDER = ["XAUUSD", "US100", "SP500", "EURUSD"]

SEARCH_QUERIES = [
    "XAUUSD gold price news {date}",
    "S&P 500 Nasdaq 100 stock market news {date}",
    "EURUSD forecast ECB Fed news {date}",
    "economic calendar this week {date} Fed ECB earnings",
    "oil price geopolitics market news {date}",
]

JSON_SCHEMA_HINT = """{
  "summary_ar": "<Telegram HTML string in Arabic>",
  "instruments": [
    {"ticker": "XAUUSD", "message_ar": "<Telegram HTML string in Arabic>"},
    {"ticker": "US100", "message_ar": "<Telegram HTML string in Arabic>"},
    {"ticker": "SP500", "message_ar": "<Telegram HTML string in Arabic>"},
    {"ticker": "EURUSD", "message_ar": "<Telegram HTML string in Arabic>"}
  ]
}"""


def local_now():
    return datetime.now(AMSTERDAM)


def gate_on_time(force: bool):
    now = local_now()
    if force:
        print(f"[gate] --force set, skipping time gate (local time {now:%H:%M} Amsterdam)")
        return
    if now.hour != 9:
        print(f"[gate] local Amsterdam time is {now:%H:%M}, not 09:xx — exiting without sending.")
        sys.exit(0)
    print(f"[gate] local Amsterdam time is {now:%H:%M} — proceeding.")


def require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        print(f"ERROR: missing required environment variable {name}", file=sys.stderr)
        sys.exit(1)
    return val


def run_searches(date_str: str) -> str:
    """Collect DuckDuckGo results for each query, formatted as plain text context."""
    blocks = []
    for template in SEARCH_QUERIES:
        query = template.format(date=date_str)
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=6))
        except Exception as exc:  # noqa: BLE001 - search is best-effort
            print(f"[search] '{query}' failed: {exc}")
            results = []

        lines = [f"### Search: {query}"]
        if not results:
            lines.append("(no results returned)")
        for r in results:
            title = r.get("title", "").strip()
            href = r.get("href", "").strip()
            body = r.get("body", "").strip()
            lines.append(f"- {title}\n  URL: {href}\n  Snippet: {body}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def ollama_chat(messages, force_json: bool = False, max_tokens: int = 4000) -> str:
    payload = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "options": {"num_predict": max_tokens},
    }
    if force_json:
        payload["format"] = "json"

    headers = {}
    if OLLAMA_API_KEY:
        headers["Authorization"] = f"Bearer {OLLAMA_API_KEY}"

    resp = requests.post(f"{OLLAMA_HOST}/api/chat", json=payload, headers=headers, timeout=600)
    resp.raise_for_status()
    data = resp.json()
    return data["message"]["content"]


def research_draft(search_context: str, report_date: str) -> str:
    system = (
        "You are an institutional markets analyst. You only state what the "
        "provided search snippets support. If a snippet lacks a clear date/time "
        "or source name, say so explicitly rather than inventing one. Never "
        "fabricate a headline, price, or link that isn't in the source material."
    )
    prompt = f"""Produce an institutional pre-market briefing draft for {report_date}
(Europe/Amsterdam timezone), covering exactly four instruments: XAUUSD (gold),
US100 (Nasdaq 100), SP500 (S&P 500), and EURUSD.

Use ONLY the search results below as your factual source. For each instrument:
- List the most relevant recent catalysts/events, citing source name and URL
  from the snippets. Note the snippet's date if available; say "date not
  confirmed in source" if not.
- Separate confirmed facts (explicitly reported) from inference/analysis/
  forward-looking statements — label each explicitly.
- Give key technical levels (support/resistance) if the snippets mention any.
- Note relevant session times in Amsterdam (CET/CEST) and New York (EST/EDT).
- Add a brief independent/contrarian challenge to the consensus view implied
  by the snippets.
- If the snippets show no significant catalyst in the next 24h for an
  instrument, say so plainly and lean on technical/macro context instead.

Write the full draft in English; it will be translated and reformatted
afterwards, so prioritize accuracy and source citations over prose polish.

SEARCH RESULTS:
---
{search_context}
---
"""

    return ollama_chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        force_json=False,
        max_tokens=4000,
    ).strip()


def validate_payload(payload: dict) -> bool:
    if not isinstance(payload, dict):
        return False
    if not isinstance(payload.get("summary_ar"), str) or not payload["summary_ar"].strip():
        return False
    instruments = payload.get("instruments")
    if not isinstance(instruments, list) or len(instruments) != 4:
        return False
    tickers_seen = set()
    for item in instruments:
        if not isinstance(item, dict):
            return False
        ticker = item.get("ticker")
        message = item.get("message_ar")
        if ticker not in INSTRUMENT_ORDER or not isinstance(message, str) or not message.strip():
            return False
        tickers_seen.add(ticker)
    return tickers_seen == set(INSTRUMENT_ORDER)


def structure_and_translate(draft: str, report_date: str) -> dict:
    system = (
        "You output ONLY valid JSON matching the exact schema you are given. "
        "No prose before or after, no markdown code fences."
    )
    base_prompt = f"""Below is an English research draft for a {report_date} pre-market
briefing covering XAUUSD, US100, SP500, and EURUSD. Convert it into Telegram-ready
Arabic messages as a single JSON object matching this schema exactly:

{JSON_SCHEMA_HINT}

Rules:
- Modern Standard Arabic prose. Keep tickers and financial/institutional terms in
  English exactly as written: XAUUSD, US100, SP500, EURUSD, Fed, FOMC, ECB, CPI,
  PMI, VIX, support/resistance, basis points, source names (e.g. CNBC, FXStreet,
  Reuters), and any other standard trading jargon. Numbers, prices, percentages,
  and dates stay in English/Western numerals.
- Telegram HTML formatting only: <b>, <i>, <a href="...">text</a>, and line
  breaks (\\n). No markdown asterisks, no unsupported tags.
- summary_ar is a standalone cross-asset overview opening with the report date
  and Amsterdam/New York time. Each instrument message_ar is fully
  self-contained: headline, key levels, confirmed catalysts vs analysis
  clearly labeled (مؤكد / تحليل-توقع), a one-line contrarian take, and source
  links.
- Each message must stay well under Telegram's ~4096 character limit — aim
  under 3500 characters per message, trimming detail rather than truncating
  mid-sentence.
- The instruments array must contain exactly these 4 tickers, one each:
  XAUUSD, US100, SP500, EURUSD.

DRAFT:
---
{draft}
---
"""

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": base_prompt},
    ]

    last_error = None
    for attempt in range(1, 4):
        raw = ollama_chat(messages, force_json=True, max_tokens=6000)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            last_error = f"invalid JSON: {exc}"
        else:
            if validate_payload(payload):
                return payload
            last_error = "JSON did not match the required schema (missing keys or wrong instrument set)"

        print(f"[structure] attempt {attempt} failed: {last_error}")
        messages.append({"role": "assistant", "content": raw})
        messages.append(
            {
                "role": "user",
                "content": (
                    f"That output was invalid: {last_error}. Re-output the ENTIRE "
                    f"corrected JSON object, matching the schema exactly, nothing else."
                ),
            }
        )

    print(f"ERROR: structuring call never produced valid JSON: {last_error}", file=sys.stderr)
    sys.exit(1)


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def send_telegram_message(token: str, chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, json=payload, timeout=30)
    if resp.status_code != 200:
        print(f"[telegram] HTML send failed ({resp.status_code}): {resp.text[:300]}")
        print("[telegram] retrying as plain text")
        payload = {
            "chat_id": chat_id,
            "text": strip_html(text),
            "disable_web_page_preview": True,
        }
        resp = requests.post(url, json=payload, timeout=30)
        resp.raise_for_status()
    print(f"[telegram] sent message ({len(text)} chars)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="bypass the 09:xx Amsterdam time gate")
    args = parser.parse_args()

    gate_on_time(args.force)

    bot_token = require_env("TELEGRAM_BOT_TOKEN")
    chat_id = require_env("TELEGRAM_CHAT_ID")

    now = local_now()
    report_date = now.strftime("%A, %d %B %Y")
    date_str = now.strftime("%Y-%m-%d")

    print(f"[0/4] searching (DuckDuckGo) for {date_str}...")
    search_context = run_searches(date_str)

    print(f"[1/4] researching via Ollama ({OLLAMA_MODEL} @ {OLLAMA_HOST})...")
    draft = research_draft(search_context, report_date)

    print("[2/4] structuring + translating to Arabic via Ollama...")
    payload = structure_and_translate(draft, report_date)

    print("[3/4] posting to Telegram...")
    send_telegram_message(bot_token, chat_id, payload["summary_ar"])
    time.sleep(1)

    by_ticker = {item["ticker"]: item["message_ar"] for item in payload["instruments"]}
    for ticker in INSTRUMENT_ORDER:
        message = by_ticker.get(ticker)
        if not message:
            print(f"[warn] no message returned for {ticker}, skipping")
            continue
        send_telegram_message(bot_token, chat_id, message)
        time.sleep(1)

    print("[4/4] done.")


if __name__ == "__main__":
    main()
