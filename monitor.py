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
def _groq_call(prompt, max_tokens=2000, temperature=0.3):
    """Single Groq call. Returns parsed dict or None. Logs raw response on failure."""
    groq_url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "openai/gpt-oss-120b",
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
    }

    try:
        r = requests.post(groq_url, headers=headers, data=json.dumps(payload))
    except Exception as e:
        print(f"Groq request exception: {e}")
        return None

    print(f"Groq HTTP status: {r.status_code}")

    if r.status_code != 200:
        print(f"Groq error body: {r.text[:1000]}")
        return None

    try:
        body = r.json()
    except Exception as e:
        print(f"Groq non-JSON body: {r.text[:500]} ({e})")
        return None

    if "choices" not in body:
        print(f"Groq response missing 'choices': {json.dumps(body)[:1000]}")
        return None

    raw = body["choices"][0]["message"]["content"]

    # Strip markdown fences if model added them
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].lstrip()
    # Trim anything after the last closing brace
    last_brace = raw.rfind("}")
    if last_brace != -1:
        raw = raw[: last_brace + 1]

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Groq JSON parse failed: {e}")
        print(f"Raw content: {raw[:1000]}")
        return None


def build_ai_summary(indices, tickers):
    def fmt_indices(rows):
        return "\n".join(
            f"{r['ticker']} ({r['name']}) | {r['price']:.2f} | "
            f"daily {r['daily']:+.2f}% | YTD {r['ytd']:+.2f}%"
            for r in rows
        )

    def fmt_tickers(rows):
        lines = []
        for r in rows:
            lines.append(
                f"\n{r['ticker']} ({r['name']}) | {r['price']:.2f} | "
                f"daily {r['daily']:+.2f}% | YTD {r['ytd']:+.2f}%"
            )
            if r["headlines"]:
                for i, h in enumerate(r["headlines"], start=1):
                    lines.append(f"  [{i}] {h['title']} — {h['publisher']}")
            else:
                lines.append("  (no headlines available)")
        return "\n".join(lines)

    prompt = (
        "You are writing a nightly market review email for a retail investor. "
        "Respond with a single JSON object only. No prose outside the JSON.\n\n"
        "MARKET DATA (today):\n"
        f"{fmt_indices(indices)}\n\n"
        "WATCHLIST (with numbered headlines):\n"
        f"{fmt_tickers(tickers)}\n\n"
        "Required JSON schema:\n"
        "{\n"
        '  "market_overview": "2-4 sentence paragraph. Never empty.",\n'
        '  "indices": {"<ticker>": "<one-line note>", ...},\n'
        '  "tickers": {\n'
        '    "<ticker>": {"note": "<1-3 sentences>", "cites": [1, 2]}\n'
        "  }\n"
        "}\n\n"
        "Rules:\n"
        "- market_overview must be 2-4 sentences, name at least two indices, "
        "and note a sector or macro theme.\n"
        "- indices: one sentence per index ticker. Never empty.\n"
        "- tickers: every watchlist ticker is a key. If headlines exist, write 1-3 "
        "sentences grounded in them and cite them inline as [1], [2], listing those "
        "numbers in the cites array. If daily move is under 0.75% and no notable "
        "headline exists, return note=\"\" and cites=[].\n"
        "- Never invent sources. Only cite indices provided.\n"
    )

    parsed = _groq_call(prompt, max_tokens=2000, temperature=0.3)

    if parsed is None:
        return {
            "market_overview": (
                "Market overview unavailable — Groq call failed. "
                "Check workflow logs for details."
            ),
            "indices": {},
            "tickers": {},
        }

    # Validate
    overview = (parsed.get("market_overview") or "").strip()
    if not overview:
        overview = (
            "Market overview unavailable — model returned empty text. "
            "See index rows for the day's moves."
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
