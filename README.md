# premarket-briefing-bot

Sends a daily pre-market briefing (XAUUSD, US100, SP500, EURUSD) to a Telegram
channel every weekday at **09:00 Europe/Amsterdam**, in Arabic, with tickers and
financial terms kept in English. Runs entirely on **Google Gemini** (Google
Search–grounded) and stock **GitHub Actions** hosted runners — no local server,
no self-hosted runner, no machine to keep on.

Each morning it sends **5 Telegram messages**: one cross-asset summary, then one
per instrument (XAUUSD, US100, SP500, EURUSD).

> **Rebuilt 2026-08.** This used to run on a *local* Ollama server driven by an
> external host cron, and DuckDuckGo for search. That cron was misconfigured to
> fire hourly, so the briefing went out every hour. It now runs on cloud APIs
> with a single self-gating schedule (below), which makes hourly spam
> structurally impossible.

## How it works

1. **Research** — one Gemini call with Google Search grounding produces a cited,
   dated English draft: recent catalysts per instrument + this week's
   high-impact US/EU calendar, confirmed facts separated from analysis.
2. **Structure + translate** — a second Gemini call (JSON mode, retries on
   malformed output) turns the draft into 5 Telegram-ready Arabic messages,
   tickers/terms left in English.
3. **Send** — each message is posted to your Telegram chat/channel via the Bot API.
4. **Schedule** — `.github/workflows/briefing.yml` fires at **07:00 and 08:00
   UTC** on weekdays. GitHub cron is UTC-only and DST-blind, so `briefing.py`'s
   `gate_on_time()` checks the real Amsterdam clock and lets through only the run
   that is actually 09:xx there — one send per day, correct through both CEST and
   CET. A manual **Run workflow** uses `--force` to bypass the gate for testing.

## One-time setup

### 1. Get a Gemini API key
Create one at [Google AI Studio](https://aistudio.google.com/apikey). The bot
uses the **google-genai** SDK (Interactions API); default model
`gemini-3.7-flash` (override with the `GEMINI_MODEL` env/var).

### 2. Create the Telegram bot and get your chat ID
1. Message [@BotFather](https://t.me/BotFather), run `/newbot`, follow the
   prompts — you'll get a token like `123456789:AAxxxxxxxxxxxx`.
2. Add the bot as an **admin** of the target channel.
3. Post any message in the channel, then open
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` and find
   `"chat":{"id": ...}` (channel IDs are negative, e.g. `-1001234567890`).

### 3. Add GitHub repo secrets
**Settings → Secrets and variables → Actions → New repository secret:**
- `GEMINI_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

### 4. Test it
- **On GitHub:** Actions tab → *Daily Pre-Market Briefing* → **Run workflow**.
  A manual run forces a send regardless of time; check the logs and your channel.
- **Locally:**
  ```bash
  pip install -r requirements.txt
  export GEMINI_API_KEY=...  TELEGRAM_BOT_TOKEN=...  TELEGRAM_CHAT_ID=...
  python briefing.py --force
  ```

Once confirmed, leave it — it runs automatically every weekday at 09:00 Amsterdam.

## Notes / limits
- **Weekends:** the schedule is weekdays only (`* * 1-5`). Edit the cron lines to change.
- **Grounding accuracy:** Gemini is instructed to cite sources and separate
  confirmed facts from analysis, and to say "date not confirmed" rather than
  invent one — but always sanity-check specific numbers before acting on them.
- **Delivery:** if a message fails to send as HTML (malformed tags), the script
  retries it as plain text, so a formatting glitch never silently drops a message.
- If the structuring step can't produce valid JSON after 3 tries, the run fails
  loudly rather than posting garbage.
