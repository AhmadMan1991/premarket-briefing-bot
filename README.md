# premarket-briefing-bot

Sends a daily pre-market briefing (XAUUSD, US100, SP500, EURUSD) to a Telegram
channel every morning at ~09:00 Europe/Amsterdam time, in Arabic, with tickers
and financial terms kept in English. Uses **Ollama** (via a reachable Ollama
API endpoint, not a local-only install) — no Anthropic/OpenAI API costs —
plus free DuckDuckGo search for the research step. Runs entirely on GitHub
Actions' normal hosted runners; no self-hosted runner or local machine needed.

Each day it sends **5 Telegram messages**: one cross-asset summary, then one
message per instrument (XAUUSD, US100, SP500, EURUSD).

## How it works

1. `scripts/briefing.py` runs a handful of DuckDuckGo searches (gold,
   equities, EURUSD/ECB/Fed, the week's economic calendar, oil/geopolitics).
2. It sends those search snippets to your Ollama model (over HTTP, via
   `OLLAMA_HOST`), which writes an English research draft — sourced, with
   confirmed facts separated from analysis.
3. A second Ollama call (JSON-constrained, with automatic retry on malformed
   output) turns that into 5 Telegram-ready Arabic messages.
4. Each message is posted to your Telegram chat/channel via the Bot API.
5. `.github/workflows/daily-briefing.yml` runs this on a schedule. GitHub
   Actions cron is UTC-only and doesn't track daylight saving, so the
   workflow fires at both 07:00 and 08:00 UTC; the script checks the real
   Amsterdam clock and only sends if it's actually 09:xx there — exactly one
   send per day, correct through both CEST and CET.

## One-time setup

### 1. Your Ollama API details

You said you already have an API for Ollama. I need to confirm two things
before this will work end-to-end:

- **Base URL** — e.g. `https://your-endpoint.example.com` (the script calls
  `POST {OLLAMA_HOST}/api/chat`, the native Ollama chat format — not an
  OpenAI-compatible `/v1/chat/completions` path; let me know if yours is
  OpenAI-style instead and I'll adjust the request format).
- **Auth** — does it require a header? The script sends
  `Authorization: Bearer <OLLAMA_API_KEY>` if `OLLAMA_API_KEY` is set, and no
  auth header at all if it's left blank.
- **Model name** — whatever model is available on that endpoint (defaults to
  `qwen2.5:14b`; override with `OLLAMA_MODEL`). Qwen models tend to have
  solid Arabic output quality; smaller models (7B and under) are noticeably
  less reliable at following the JSON-formatting rules in step 3.

### 2. Create the Telegram bot and get your chat ID

1. Message [@BotFather](https://t.me/BotFather) on Telegram, run `/newbot`,
   follow the prompts. You'll get a token like `123456789:AAxxxxxxxxxxxx`.
2. Create (or pick) the channel you want the briefing posted to, and add the
   bot as an **admin** of that channel.
3. Get the channel's chat ID:
   - Post any message in the channel.
   - Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a
     browser and find `"chat":{"id": ...}` in the response (channel IDs are
     negative numbers, e.g. `-1001234567890`).

### 3. Create the GitHub repo and push this code

Run from inside this folder:

```bash
git init
git add -A
git commit -m "Initial commit: daily pre-market briefing bot (Ollama)"
```

Then create an empty repo on GitHub (via the website, or `gh repo create
premarket-briefing-bot --private --source=. --remote=origin` if you have the
GitHub CLI authenticated), and push:

```bash
git remote add origin https://github.com/<your-username>/premarket-briefing-bot.git
git branch -M main
git push -u origin main
```

### 4. Add secrets to the GitHub repo

In the repo on GitHub: **Settings → Secrets and variables → Actions → New
repository secret**, add:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `OLLAMA_HOST`
- `OLLAMA_API_KEY` (add it even if blank isn't allowed — set to any
  placeholder if your endpoint truly needs no auth, since an unset secret
  just resolves to an empty string, which is fine)

Optionally add a repo **variable** (Settings → Secrets and variables →
Actions → Variables tab) named `OLLAMA_MODEL` if you're not using the
default `qwen2.5:14b`.

### 5. Test it

- **Locally:**
  ```bash
  cp .env.example .env
  # fill in real values in .env
  pip install -r requirements.txt
  export $(grep -v '^#' .env | xargs)
  python scripts/briefing.py --force
  ```
- **On GitHub:** Actions tab → "Daily Pre-Market Briefing" → **Run
  workflow** → `force: true` → Run. Check the logs and your Telegram
  channel.

Once confirmed working, leave it alone — it'll run automatically every
morning around 09:00 Amsterdam time.

## Notes / limits

- DuckDuckGo search has no API key but is rate-limited and can occasionally
  block/return empty results — the script treats a failed search as
  "no results" and carries on rather than crashing; the model is instructed
  to say so rather than invent sources.
- Local/open models are meaningfully weaker than frontier cloud models at
  multi-source synthesis and reliably following complex formatting rules —
  expect more variance in output quality. The structuring step retries up
  to 3 times on invalid JSON before giving up; a small/weak model may still
  fail all 3 attempts, in which case the run fails loudly rather than
  posting garbage to the channel.
- Weekends: markets are largely closed Sat/Sun; the bot still runs and will
  say so in the content rather than skip — remove the weekend behavior by
  editing the cron lines if you'd rather it stay silent then.
- If a Telegram message fails to send as HTML (malformed tags from the
  model), the script automatically retries as plain text so delivery never
  silently fails.
