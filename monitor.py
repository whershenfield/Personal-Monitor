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
    """True if yfinance returns a bar dated today (i.e. market traded today)."""
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
    """Return (current, daily_pct, ytd_pct) or (None, None, None) on failure."""
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
    try:
        news = yf.Ticker(ticker).news or []
        return [
            a["content"]["title"]
            for a in news[:limit]
            if a.get("content", {}).get("title")
        ]
    except Exception:
        return []


def company_name(ticker):
    try:
        return yf.Ticker(ticker).info.get("longName", ticker)
    except Exception:
        return ticker


# ------------------------------------------------------------------
# Data gathering
# ------------------------------------------------------------------
def gather_group(tickers, with_headlines=False):
    """Returns list of dicts: {ticker, name, price, daily, ytd, headlines}."""
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
# AI summary — single batched call
# ------------------------------------------------------------------
def build_ai_summary(indices, tickers):
    def fmt_rows(rows, include_headlines):
        lines = []
        for r in rows:
            lines.append(
                f"{r['ticker']} ({r['name']}) | "
                f"{r['price']:.2f} | daily {r['daily']:+.2f}% | "
                f"YTD {r['ytd']:+.2f}%"
            )
            if include_headlines and r["headlines"]:
                for h in r["headlines"]:
                    lines.append(f"    - {h}")
        return "\n".join(lines)

    prompt = (
        "You are writing a concise nightly market review email.\n\n"
        "MARKET DATA (today):\n"
        f"{fmt_rows(indices, False)}\n\n"
        "WATCHLIST:\n"
        f"{fmt_rows(tickers, True)}\n\n"
        "Return ONLY valid JSON matching this schema exactly:\n"
        "{\n"
        '  "market_overview": "<2-4 sentence paragraph on the day\'s tape>",\n'
        '  "indices": {"<ticker>": "<one-line note>", ...},\n'
        '  "tickers": {"<ticker>": "<one-line note grounded in the headlines>", ...}\n'
        "}\n\n"
        "Rules:\n"
        "- Every ticker in the input must appear in the corresponding object.\n"
        "- One-line notes: factual, grounded in the provided headlines when available; "
        "if no headlines, give a brief plausible sector/macro observation.\n"
        "- No markdown, no commentary outside the JSON."
    )

    groq_url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "llama-3.3-70b-versatile",
        "max_tokens": 1500,
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": prompt}],
    }

    try:
        r = requests.post(groq_url, headers=headers, data=json.dumps(payload))
        content = r.json()["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception as e:
        print(f"AI summary failed: {e}")
        return {"market_overview": "", "indices": {}, "tickers": {}}


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


def compose_email(indices, tickers, portfolio, ai):
    now = datetime.now(pytz.timezone("America/New_York")).strftime(
        "%A, %B %d, %Y"
    )
    parts = [f"Daily Market Review — {now}", "=" * 60, ""]

    # Overview
    parts.append("MARKET OVERVIEW")
    parts.append("-" * 60)
    parts.append(ai.get("market_overview", "No overview available."))
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
    for r in tickers:
        parts.append(_fmt_quote_row(r, ai.get("tickers", {}).get(r["ticker"])))
    parts.append("")

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
    # 1. Market open today?
    if not has_fresh_data("^GSPC"):
        print("No fresh market data for today — exiting.")
        return

    # 2. Catch-up guard (11 PM second run bails if 9:30 PM already succeeded)
    if already_ran_today():
        print("Already ran today — exiting.")
        return

    # 3. Gather
    indices = gather_group(config.INDICES, with_headlines=False)
    tickers = gather_group(config.TICKERS, with_headlines=True)
    portfolio = gather_group(config.PORTFOLIO, with_headlines=False)

    if not indices and not tickers and not portfolio:
        print("No data gathered — exiting without email.")
        return

    # 4. AI (indices + tickers only)
    ai = build_ai_summary(indices, tickers)

    # 5. Email
    today_str = date.today().strftime("%a, %b %d %Y")
    subject = f"Daily Market Review — {today_str}"
    body = compose_email(indices, tickers, portfolio, ai)
    send_email(subject, body)
    print("Daily review email sent.")

    # 6. Mark success
    mark_run_success()


if __name__ == "__main__":
    main()
