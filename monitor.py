import smtplib
import json
import os
import yfinance as yf
from email.mime.text import MIMEText
from datetime import datetime, date
import pytz
import requests
import config

# Override config with GitHub secrets if running in Actions
config.EMAIL_SENDER    = os.environ.get("EMAIL_SENDER", config.EMAIL_SENDER)
config.EMAIL_PASSWORD  = os.environ.get("EMAIL_PASSWORD", config.EMAIL_PASSWORD)
config.EMAIL_RECIPIENT = os.environ.get(
    "EMAIL_RECIPIENT", ",".join(config.EMAIL_RECIPIENT)
).split(",")
config.GROQ_API_KEY    = os.environ.get("GROQ_API_KEY", config.GROQ_API_KEY)
config.GIST_ID         = os.environ.get("GIST_ID", config.GIST_ID)
config.GIST_TOKEN      = os.environ.get("GIST_TOKEN_GITHUB", config.GIST_TOKEN)


# ------------------------------------------------------------------
# Fresh-data / run-guard helpers
# ------------------------------------------------------------------
def has_fresh_data(ticker):
    try:
        hist = yf.Ticker(ticker).history(period="5d")
        if hist.empty:
            return False
        return hist.index[-1].date() == date.today()
    except Exception as e:
        print(f"Fresh-data check failed for {ticker}: {e}")
        return False


def get_run_state():
    headers = {
        "Authorization": f"token {config.GIST_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    r = requests.get(
        f"https://api.github.com/gists/{config.GIST_ID}", headers=headers
    )
    if r.status_code != 200:
        print(f"Failed to fetch Gist: {r.status_code}")
        return {}
    content = (
        r.json()
        .get("files", {})
        .get("run_state.json", {})
        .get("content", "{}")
    )
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return {}


def already_ran_today():
    return get_run_state().get("last_run_date") == date.today().isoformat()


def mark_run_success():
    headers = {
        "Authorization": f"token {config.GIST_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    payload = {
        "files": {
            "run_state.json": {
                "content": json.dumps(
                    {"last_run_date": date.today().isoformat()}
                )
            }
        }
    }
    r = requests.patch(
        f"https://api.github.com/gists/{config.GIST_ID}",
        headers=headers,
        data=json.dumps(payload),
    )
    if r.status_code != 200:
        print(f"Failed to save run state: {r.status_code}")


# ------------------------------------------------------------------
# Quote helpers
# ------------------------------------------------------------------
def get_quote_and_ytd(ticker):
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(start=f"{date.today().year}-01-01")
        if len(hist) < 2:
            return None, None, None

        current = stock.fast_info.last_price
        prev_close = hist["Close"].iloc[-2]
        ytd_start = hist["Close"].iloc[0]

        if not current or not prev_close or not ytd_start:
            return None, None, None

        daily_pct = ((current - prev_close) / prev_close) * 100
        ytd_pct = ((current - ytd_start) / ytd_start) * 100
        return current, daily_pct, ytd_pct
    except Exception as e:
        print(f"Error fetching {ticker}: {e}")
        return None, None, None


def get_headlines(ticker, limit=5):
    """Returns list of dicts: {title, publisher, url}."""
    try:
        news = yf.Ticker(ticker).news or []
    except Exception:
        return []

    out = []
    for a in news[:limit]:
        content = a.get("content", {}) or {}
        title = content.get("title")
        if not title:
            continue
        publisher = (
            content.get("provider", {}).get("displayName")
            or a.get("publisher")
            or "Unknown"
        )
        url = (
            content.get("canonicalUrl", {}).get("url")
            or content.get("clickThroughUrl", {}).get("url")
            or a.get("link")
            or ""
        )
        out.append({"title": title, "publisher": publisher, "url": url})
    return out


def company_name(ticker):
    try:
        return yf.Ticker(ticker).info.get("longName", ticker)
    except Exception:
        return ticker


# ------------------------------------------------------------------
# Data gathering
# ------------------------------------------------------------------
def gather_group(tickers, with_headlines=False):
    rows = []
    for t in tickers:
        current, daily, ytd = get_quote_and_ytd(t)
        if current is None:
            print(f"Could not fetch data for {t}")
            continue
        rows.append(
            {
                "ticker": t,
                "name": company_name(t),
                "price": current,
                "daily": daily,
                "ytd": ytd,
                "headlines": get_headlines(t) if with_headlines else [],
            }
        )
    return rows


# ------------------------------------------------------------------
# AI summary — single batched call with citations
# ------------------------------------------------------------------
def build_ai_summary(indices, tickers):
    """
    Returns dict:
      {
        "market_overview": str,
        "indices":  {ticker: note},
        "tickers":  {ticker: {"note": str, "cites": [int, ...]}}
      }
    where cites are 1-based indices into that ticker's headline list.
    """

    def fmt_indices(rows):
        lines = []
        for r in rows:
            lines.append(
                f"{r['ticker']} ({r['name']}) | "
                f"{r['price']:.2f} | daily {r['daily']:+.2f}% | "
                f"YTD {r['ytd']:+.2f}%"
            )
        return "\n".join(lines)

    def fmt_tickers(rows):
        lines = []
        for r in rows:
            lines.append(
                f"\n{r['ticker']} ({r['name']}) | "
                f"{r['price']:.2f} | daily {r['daily']:+.2f}% | "
                f"YTD {r['ytd']:+.2f}%"
            )
            if r["headlines"]:
                for i, h in enumerate(r["headlines"], start=1):
                    lines.append(
                        f"  [{i}] {h['title']} — {h['publisher']}"
                    )
            else:
                lines.append("  (no headlines available)")
        return "\n".join(lines)

    prompt = (
        "You are writing a nightly market review email for a retail investor.\n\n"
        "MARKET DATA (today):\n"
        f"{fmt_indices(indices)}\n\n"
        "WATCHLIST (with numbered headlines):\n"
        f"{fmt_tickers(tickers)}\n\n"
        "Return ONLY valid JSON matching this schema exactly:\n"
        "{\n"
        '  "market_overview": "<2-4 sentence paragraph summarizing the day\'s tape, '
        'referencing the index moves above. NEVER empty.>",\n'
        '  "indices": {"<ticker>": "<one-line note, never empty>", ...},\n'
        '  "tickers": {\n'
        '    "<ticker>": {"note": "<1-3 sentence note>", "cites": [<int>, ...]},\n'
        "    ...\n"
        "  }\n"
        "}\n\n"
        "Rules:\n"
        "- market_overview: MUST be 2-4 sentences. NEVER return an empty string. "
        "Describe the overall direction, mention at least two named indices, "
        "and note any obvious sector or macro theme visible in the data.\n"
        "- indices: every index ticker appears as a key. One sentence each. Never empty.\n"
        "- tickers: every watchlist ticker appears as a key.\n"
        "    * If there are headlines, write 1-3 sentences grounded strictly in them.\n"
        "    * If there are no headlines, write a brief sector/macro observation.\n"
        "    * If the daily move is small (|daily| < 0.75%) and there is no notable "
        "      headline, return note = \"\" (empty string) and cites = [].\n"
        "    * When a claim is supported by a headline, cite it inline as [1], [2], "
        "      matching the numbered headlines. List those numbers in the cites array.\n"
        "- Do not invent URLs or publishers. Only cite headline indices you were given.\n"
        "- No markdown, no commentary outside the JSON."
    )

    groq_url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "llama-3.3-70b-versatile",
        "max_tokens": 2000,
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": prompt}],
    }

    fallback = {"market_overview": "", "indices": {}, "tickers": {}}

    try:
        r = requests.post(groq_url, headers=headers, data=json.dumps(payload))
        raw = r.json()["choices"][0]["message"]["content"]
        parsed = json.loads(raw)
    except Exception as e:
        print(f"AI summary failed: {e}")
        try:
            print(f"Raw Groq response: {raw}")
        except NameError:
            pass
        return fallback

    # Normalize / validate
    overview = (parsed.get("market_overview") or "").strip()
    if not overview:
        overview = (
            "Market overview unavailable — the model returned an empty "
            "response. See index rows below for the day's moves."
        )

    indices_notes = parsed.get("indices") or {}
    if not isinstance(indices_notes, dict):
        indices_notes = {}

    ticker_notes_raw = parsed.get("tickers") or {}
    ticker_notes = {}
    if isinstance(ticker_notes_raw, dict):
        for k, v in ticker_notes_raw.items():
            if isinstance(v, dict):
                note = (v.get("note") or "").strip()
                cites = v.get("cites") or []
                if not isinstance(cites, list):
                    cites = []
                cites = [c for c in cites if isinstance(c, int)]
            else:
                note = (v or "").strip() if isinstance(v, str) else ""
                cites = []
            ticker_notes[k] = {"note": note, "cites": cites}

    return {
        "market_overview": overview,
        "indices": indices_notes,
        "tickers": ticker_notes,
    }


# ------------------------------------------------------------------
# Email composition
# ------------------------------------------------------------------
def _fmt_quote_row(r, note=None):
    arrow = "▲" if r["daily"] >= 0 else "▼"
    line = (
        f"{r['ticker']:<8} {r['name'][:22]:<22} "
        f"{r['price']:>10.2f}   "
        f"{r['daily']:>+6.2f}% {arrow}   "
        f"YTD {r['ytd']:>+6.2f}%"
    )
    if note:
        line += f"\n    {note}"
    return line


def _fmt_ticker_block(r, ai_entry):
    """Ticker quote + optional written note + sources block."""
    arrow = "▲" if r["daily"] >= 0 else "▼"
    header = (
        f"{r['ticker']:<8} {r['name'][:22]:<22} "
        f"{r['price']:>10.2f}   "
        f"{r['daily']:>+6.2f}% {arrow}   "
        f"YTD {r['ytd']:>+6.2f}%"
    )

    note = (ai_entry or {}).get("note", "").strip()
    cites = (ai_entry or {}).get("cites", []) or []

    if not note:
        return header

    lines = [header, f"    {note}"]

    # Build sources list from cited headline indices
    cited_headlines = []
    for idx in cites:
        if 1 <= idx <= len(r["headlines"]):
            cited_headlines.append(r["headlines"][idx - 1])

    if cited_headlines:
        lines.append("    Sources:")
        for h in cited_headlines:
            pub = h.get("publisher", "Unknown")
            url = h.get("url", "")
            if url:
                lines.append(f"      • {pub} — {url}")
            else:
                lines.append(f"      • {pub}")

    return "\n".join(lines)


def compose_email(indices, tickers, portfolio, ai):
    now = datetime.now(pytz.timezone("America/New_York")).strftime(
        "%A, %B %d, %Y"
    )
    parts = [f"Daily Market Review — {now}", "=" * 60, ""]

    # Overview
    parts.append("MARKET OVERVIEW")
    parts.append("-" * 60)
    parts.append(ai.get("market_overview", "").strip() or "(unavailable)")
    parts.append("")

    # Indices
    parts.append("INDICES")
    parts.append("-" * 60)
    for r in indices:
        parts.append(_fmt_quote_row(r, ai.get("indices", {}).get(r["ticker"])))
    parts.append("")

    # Tracked tickers
    parts.append("TRACKED TICKERS")
    parts.append("-" * 60)
    ticker_ai = ai.get("tickers", {})
    for r in tickers:
        parts.append(_fmt_ticker_block(r, ticker_ai.get(r["ticker"], {})))
        parts.append("")  # spacing between blocks

    # Portfolio (no AI notes)
    parts.append("PORTFOLIO")
    parts.append("-" * 60)
    for r in portfolio:
        parts.append(_fmt_quote_row(r))
    parts.append("")

    parts.append("-" * 60)
    parts.append("Generated automatically via Yahoo Finance & Groq")
    return "\n".join(parts)


def send_email(subject, body):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = config.EMAIL_SENDER
    msg["To"] = ", ".join(config.EMAIL_RECIPIENT)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(config.EMAIL_SENDER, config.EMAIL_PASSWORD)
        server.sendmail(config.EMAIL_SENDER, config.EMAIL_RECIPIENT, msg.as_string())


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
def main():
    if not has_fresh_data("^GSPC"):
        print("No fresh market data for today — exiting.")
        return

    if already_ran_today():
        print("Already ran today — exiting.")
        return

    indices = gather_group(config.INDICES, with_headlines=False)
    tickers = gather_group(config.TICKERS, with_headlines=True)
    portfolio = gather_group(config.PORTFOLIO, with_headlines=False)

    if not indices and not tickers and not portfolio:
        print("No data gathered — exiting without email.")
        return

    ai = build_ai_summary(indices, tickers)

    today_str = date.today().strftime("%a, %b %d %Y")
    subject = f"Daily Market Review — {today_str}"
    body = compose_email(indices, tickers, portfolio, ai)
    send_email(subject, body)
    print("Daily review email sent.")

    mark_run_success()


if __name__ == "__main__":
    main()
