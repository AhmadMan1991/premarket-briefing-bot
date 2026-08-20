#!/usr/bin/env python3
"""
Daily pre-market briefing generator + Telegram publisher (Gemini edition).

REBUILT 2026-08 — was local-Ollama + DuckDuckGo, which meant it could only run
on the one machine hosting Ollama, via an external host cron. That cron was
misconfigured to fire hourly (or with --force), so the briefing went out every
hour instead of once each morning. This version:

  * Runs entirely on cloud APIs (Google Gemini) — no local server — so it runs
    hands-off on GitHub Actions.
  * Uses ONE self-gating schedule: the workflow fires at 07:00 and 08:00 UTC on
    weekdays, and gate_on_time() lets exactly ONE of them through — whichever
    lands on 09:00 Europe/Amsterdam. That handles CET/CEST daylight-saving
    automatically (GitHub cron is UTC-only and DST-blind) and makes hourly
    spam structurally impossible: there is no hourly schedule to misconfigure.

Pipeline:
  1. Gemini (Google Search grounding) -> a live, cited English research brief
     for XAUUSD, US100, SP500, EURUSD + this week's high-impact calendar.
  2. Gemini (JSON mode) -> Telegram-ready Arabic messages, one per instrument
     plus a cross-asset summary. Tickers/terms stay in English.
  3. Post each message to Telegram, in order.

Env (set as GitHub repo secrets):
    GEMINI_API_KEY       - Google AI Studio key
    TELEGRAM_BOT_TOKEN   - bot token
    TELEGRAM_CHAT_ID     - target chat/channel id

Run manually:  python briefing.py --force     # bypass the 09:xx Amsterdam gate
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

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
)
AMSTERDAM = ZoneInfo("Europe/Amsterdam")
INSTRUMENT_ORDER = ["XAUUSD", "US100", "SP500", "EURUSD"]

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
    """Only proceed at 09:xx Europe/Amsterdam. The workflow fires at 07:00 and
    08:00 UTC; exactly one of those is 09:xx Amsterdam depending on DST, so
    exactly one run sends and the other exits here. --force bypasses (manual)."""
    now = local_now()
    if force:
        print(f"[gate] --force set, skipping time gate (local {now:%H:%M} Amsterdam)")
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


# ── Gemini calls ──────────────────────────────────────────────────────────

def _gemini(payload: dict, timeout: int = 120) -> dict:
    r = requests.post(
        f"{GEMINI_URL}?key={GEMINI_API_KEY}",
        headers={"content-type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    if not r.ok:
        raise RuntimeError(f"Gemini {r.status_code}: {r.text[:300]}")
    data = r.json()
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts)


RESEARCH_PROMPT = """Use your web search tool to gather the latest (last 3 days) market catalysts \
and this week's high-impact economic calendar, then produce an institutional pre-market briefing \
draft for {report_date} (Europe/Amsterdam timezone), covering exactly four instruments: \
XAUUSD (gold), US100 (Nasdaq 100), SP500 (S&P 500), and EURUSD.

For each instrument:
- List the most relevant recent catalysts/events, citing source name and date. If a fact isn't
  clearly dated in what you found, say "date not confirmed".
- Separate confirmed facts (explicitly reported) from inference/analysis — label each explicitly.
- Give key technical levels (support/resistance) if available.
- Note relevant session times in Amsterdam (CET/CEST) and New York (EST/EDT).
- Add a brief independent/contrarian challenge to the consensus view.
- If there's no significant catalyst in the next 24h for an instrument, say so plainly and lean
  on technical/macro context.

Also include a short "This week's high-impact US/EU calendar" section (date, time ET, event,
forecast, previous) for the next 5 trading days.

Be factual and cite sources. Write the full draft in English; it will be translated and reformatted
afterwards, so prioritize accuracy and citations over polish."""


def research_draft(report_date: str) -> str:
    """Gemini with Google Search grounding -> cited English draft."""
    text = _gemini({
        "contents": [{"parts": [{"text": RESEARCH_PROMPT.format(report_date=report_date)}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"maxOutputTokens": 4000, "temperature": 0.2},
    })
    if not text.strip():
        raise RuntimeError("research draft came back empty")
    return text.strip()


STRUCTURE_SYSTEM = (
    "You output ONLY valid JSON matching the exact schema you are given. "
    "No prose before or after, no markdown code fences."
)


def structure_and_translate(draft: str, report_date: str) -> dict:
    base_prompt = f"""Below is an English research draft for a {report_date} pre-market briefing
covering XAUUSD, US100, SP500, and EURUSD. Convert it into Telegram-ready Arabic messages as a
single JSON object matching this schema exactly:

{JSON_SCHEMA_HINT}

Rules:
- Modern Standard Arabic prose. Keep tickers and financial/institutional terms in English exactly:
  XAUUSD, US100, SP500, EURUSD, Fed, FOMC, ECB, CPI, PMI, VIX, support/resistance, basis points,
  and source names (CNBC, FXStreet, Reuters, etc.). Numbers, prices, percentages, dates stay in
  Western numerals.
- Telegram HTML only: <b>, <i>, <a href="...">text</a>, and line breaks (\\n). No markdown asterisks.
- summary_ar is a standalone cross-asset overview opening with the report date and Amsterdam/New York
  time. Each instrument message_ar is self-contained: headline, key levels, confirmed catalysts vs
  analysis clearly labeled (مؤكد / تحليل-توقع), a one-line contrarian take, and source links.
- Each message must stay under ~3500 characters, trimming detail rather than truncating mid-sentence.
- The instruments array must contain exactly these 4 tickers, one each: XAUUSD, US100, SP500, EURUSD.

DRAFT:
---
{draft}
---
"""
    last_error = None
    convo = f"{STRUCTURE_SYSTEM}\n\n{base_prompt}"
    for attempt in range(1, 4):
        try:
            raw = _gemini({
                "contents": [{"parts": [{"text": convo}]}],
                "generationConfig": {
                    "maxOutputTokens": 6000, "temperature": 0.3,
                    "response_mime_type": "application/json",
                },
            })
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            last_error = f"invalid JSON: {exc}"
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        else:
            if validate_payload(payload):
                return payload
            last_error = "JSON did not match schema (missing keys or wrong instrument set)"
        print(f"[structure] attempt {attempt} failed: {last_error}")
        convo = (f"{STRUCTURE_SYSTEM}\n\n{base_prompt}\n\nYour previous output was invalid: "
                 f"{last_error}. Re-output the ENTIRE corrected JSON object, nothing else.")

    print(f"ERROR: structuring never produced valid JSON: {last_error}", file=sys.stderr)
    sys.exit(1)


def validate_payload(payload: dict) -> bool:
    if not isinstance(payload, dict):
        return False
    if not isinstance(payload.get("summary_ar"), str) or not payload["summary_ar"].strip():
        return False
    instruments = payload.get("instruments")
    if not isinstance(instruments, list) or len(instruments) != 4:
        return False
    seen = set()
    for item in instruments:
        if not isinstance(item, dict):
            return False
        ticker = item.get("ticker")
        message = item.get("message_ar")
        if ticker not in INSTRUMENT_ORDER or not isinstance(message, str) or not message.strip():
            return False
        seen.add(ticker)
    return seen == set(INSTRUMENT_ORDER)


# ── Telegram ──────────────────────────────────────────────────────────────

def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def send_telegram_message(token: str, chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    resp = requests.post(url, json=payload, timeout=30)
    if resp.status_code != 200:
        print(f"[telegram] HTML send failed ({resp.status_code}): {resp.text[:300]}")
        print("[telegram] retrying as plain text")
        payload = {"chat_id": chat_id, "text": strip_html(text), "disable_web_page_preview": True}
        resp = requests.post(url, json=payload, timeout=30)
        resp.raise_for_status()
    print(f"[telegram] sent message ({len(text)} chars)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="bypass the 09:xx Amsterdam time gate")
    args = parser.parse_args()

    gate_on_time(args.force)

    require_env("GEMINI_API_KEY")
    bot_token = require_env("TELEGRAM_BOT_TOKEN")
    chat_id = require_env("TELEGRAM_CHAT_ID")

    now = local_now()
    report_date = now.strftime("%A, %d %B %Y")

    print(f"[1/3] researching via Gemini ({GEMINI_MODEL}, Google Search grounded)...")
    draft = research_draft(report_date)

    print("[2/3] structuring + translating to Arabic via Gemini (JSON mode)...")
    payload = structure_and_translate(draft, report_date)

    print("[3/3] posting to Telegram...")
    send_telegram_message(bot_token, chat_id, payload["summary_ar"])
    time.sleep(1)
    by_ticker = {item["ticker"]: item["message_ar"] for item in payload["instruments"]}
    for ticker in INSTRUMENT_ORDER:
        message = by_ticker.get(ticker)
        if not message:
            print(f"[warn] no message for {ticker}, skipping")
            continue
        send_telegram_message(bot_token, chat_id, message)
        time.sleep(1)
    print("done.")


if __name__ == "__main__":
    main()
