import smtplib
import json
import os
import yfinance as yf
from email.mime.text import MIMEText
from datetime import datetime, date, timedelta
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


def gather_market_news(limit=8):
    """Broad market headlines via SPY + macro tickers, deduped."""
    seen = set()
    out = []
    sources = ["SPY", "^GSPC", "QQQ", "TLT", "BZ=F"]
    for src in sources:
        for h in get_headlines(src, limit=limit):
            if h["title"] in seen:
                continue
            seen.add(h["title"])
            out.append(h)
    return out[:limit]


def gather_lookahead():
    """Pull upcoming earnings + macro data for next ~7 days."""
    lookahead = {"earnings": [], "macro": []}
    today = date.today()

    # Earnings: check each tracked ticker for upcoming earnings
    for t in config.TICKERS + config.PORTFOLIO:
        try:
            cal = yf.Ticker(t).calendar
            if cal is None:
                continue
            # yfinance returns either a dict or a DataFrame depending on version
            if isinstance(cal, dict):
                ed = cal.get("Earnings Date")
                if ed and isinstance(ed, list) and ed:
                    ed = ed[0]
                if ed and isinstance(ed, (date, datetime)):
                    ed = ed.date() if isinstance(ed, datetime) else ed
                    if today <= ed <= today + timedelta(days=7):
                        lookahead["earnings"].append(
                            {"ticker": t, "name": company_name(t), "date": ed.isoformat()}
                        )
        except Exception as e:
            print(f"Calendar check failed for {t}: {e}")

    # Macro: yfinance doesn't expose econ calendar cleanly.
    # Use SPY news filtered for macro keywords as a proxy.
    macro_keywords = ["fed", "inflation", "cpi", "jobs", "payroll", "rate", "treasury", "gdp"]
    for h in get_headlines("SPY", limit=15) + get_headlines("^TNX", limit=10):
        title_lower = h["title"].lower()
        if any(k in title_lower for k in macro_keywords):
            lookahead["macro"].append(h)
            if len(lookahead["macro"]) >= 5:
                break

    return lookahead


# ------------------------------------------------------------------
# Groq helper
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

    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].lstrip()
    last_brace = raw.rfind("}")
    if last_brace != -1:
        raw = raw[: last_brace + 1]

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Groq JSON parse failed: {e}")
        print(f"Raw content: {raw[:1000]}")
        return None


# ------------------------------------------------------------------
# AI summary — batched call for tickers + stories + lookahead
# ------------------------------------------------------------------
def build_ai_summary(indices, tickers, market_news, lookahead):
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

    def fmt_news(rows):
        lines = []
        for i, h in enumerate(rows, start=1):
            lines.append(f"[{i}] {h['title']} — {h['publisher']}")
        return "\n".join(lines) if lines else "(no broad-market headlines available)"

    def fmt_lookahead(la):
        lines = []
        if la["earnings"]:
            lines.append("Upcoming earnings this week:")
            for e in la["earnings"]:
                lines.append(f"  {e['date']}: {e['ticker']} ({e['name']})")
        if la["macro"]:
            lines.append("\nMacro headlines for context:")
            for h in la["macro"]:
                lines.append(f"  - {h['title']} — {h['publisher']}")
        return "\n".join(lines) if lines else "(no scheduled events found)"

    prompt = (
        "You are writing a nightly market review email for a retail investor. "
        "Respond with a single JSON object only. No prose outside the JSON.\n\n"
        "MARKET DATA (today):\n"
        f"{fmt_indices(indices)}\n\n"
        "WATCHLIST (with numbered headlines):\n"
        f"{fmt_tickers(tickers)}\n\n"
        "BROAD MARKET HEADLINES:\n"
        f"{fmt_news(market_news)}\n\n"
        "UPCOMING EVENTS / LOOKAHEAD:\n"
        f"{fmt_lookahead(lookahead)}\n\n"
        "Required JSON schema:\n"
        "{\n"
        '  "tickers": {\n'
        '    "<ticker>": {"note": "<1-3 sentences>", "cites": [1, 2]}\n'
        "  },\n"
        '  "stories": [\n'
        '    {"title": "<headline title>", "summary": "<~200 word summary>", '
        '"source_index": <int matching BROAD MARKET HEADLINES list>}\n'
        "  ],\n"
        '  "looking_ahead": "<3-5 sentence forward-looking paragraph covering '
        'upcoming earnings, economic data, and what to watch>"\n'
        "}\n\n"
        "Rules:\n"
        "- tickers: every watchlist ticker is a key. If headlines exist, write 1-3 "
        "sentences grounded in them and cite them inline as [1], [2], listing those "
        "numbers in the cites array. If daily move is under 0.75% and no notable "
        "headline exists, return note=\"\" and cites=[].\n"
        "- stories: select up to 5 distinct headlines from BROAD MARKET HEADLINES. "
        "Write ~200 words for each, expanding on the headline with context, "
        "implications, and what it means for investors. Cite the source_index "
        "(the number from the list). Do not invent stories.\n"
        "- looking_ahead: 3-5 sentences. Name specific companies reporting earnings, "
        "specific economic releases if visible in the lookahead data, and any "
        "macro themes worth watching. Be concrete, not generic.\n"
        "- Never invent sources. Only cite indices provided.\n"
    )

    parsed = _groq_call(prompt, max_tokens=3500, temperature=0.35)

    if parsed is None:
        return {"tickers": {}, "stories": [], "looking_ahead": ""}

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

    stories_raw = parsed.get("stories") or []
    stories = []
    if isinstance(stories_raw, list):
        for s in stories_raw[:5]:
            if not isinstance(s, dict):
                continue
            stories.append(
                {
                    "title": (s.get("title") or "").strip(),
                    "summary": (s.get("summary") or "").strip(),
                    "source_index": s.get("source_index"),
                }
            )

    looking_ahead = (parsed.get("looking_ahead") or "").strip()

    return {
        "tickers": ticker_notes,
        "stories": stories,
        "looking_ahead": looking_ahead,
    }


# ------------------------------------------------------------------
# Email composition
# ------------------------------------------------------------------
def _fmt_quote_row(r):
    arrow = "▲" if r["daily"] >= 0 else "▼"
    return (
        f"{r['ticker']:<8} {r['name'][:22]:<22} "
        f"{r['price']:>10.2f}   "
        f"{r['daily']:>+6.2f}% {arrow}   "
        f"YTD {r['ytd']:>+6.2f}%"
    )


def _fmt_ticker_block(r, ai_entry):
    header = _fmt_quote_row(r)
    note = (ai_entry or {}).get("note", "").strip()
    cites = (ai_entry or {}).get("cites", []) or []

    if not note:
        return header

    lines = [header, f"    {note}"]

    cited_headlines = []
    for idx in cites:
        if 1 <= idx <= len(r["headlines"]):
            cited_headlines.append(r["headlines"][idx - 1])

    if cited_headlines:
        lines.append("    Sources:")
        for h in cited_headlines:
            pub = h.get("publisher", "Unknown")
            url = h.get("url", "")
            lines.append(f"      • {pub} — {url}" if url else f"      • {pub}")

    return "\n".join(lines)


def compose_email(indices, tickers, portfolio, market_news, lookahead, ai):
    now = datetime.now(pytz.timezone("America/New_York")).strftime(
        "%A, %B %d, %Y"
    )
    parts = [f"Daily Market Review — {now}", "=" * 60, ""]

    # Indices — quote rows only
    parts.append("INDICES")
    parts.append("-" * 60)
    for r in indices:
        parts.append(_fmt_quote_row(r))
    parts.append("")

    # Tracked tickers — with AI notes + sources
    parts.append("TRACKED TICKERS")
    parts.append("-" * 60)
    ticker_ai = ai.get("tickers", {})
    for r in tickers:
        parts.append(_fmt_ticker_block(r, ticker_ai.get(r["ticker"], {})))
        parts.append("")

    # Portfolio — quote rows only
    parts.append("PORTFOLIO")
    parts.append("-" * 60)
    for r in portfolio:
        parts.append(_fmt_quote_row(r))
    parts.append("")

    # Market stories — ~5 headlines with ~200 word summaries
    parts.append("MARKET STORIES")
    parts.append("-" * 60)
    for s in ai.get("stories", []):
        title = s.get("title", "").strip()
        summary = s.get("summary", "").strip()
        src_idx = s.get("source_index")
        if title:
            parts.append(title.upper())
        if summary:
            # Wrap at ~78 chars for readability
            words = summary.split()
            line = ""
            for w in words:
                if len(line) + len(w) + 1 > 78:
                    parts.append(line)
                    line = w
                else:
                    line = f"{line} {w}".strip()
            if line:
                parts.append(line)
        if isinstance(src_idx, int) and 1 <= src_idx <= len(market_news):
            h = market_news[src_idx - 1]
            parts.append(f"Source: {h['publisher']} — {h.get('url', '')}")
        parts.append("")

    # Looking ahead
    parts.append("LOOKING AHEAD")
    parts.append("-" * 60)
    la = ai.get("looking_ahead", "").strip()
    parts.append(la if la else "(unavailable)")
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
    market_news = gather_market_news(limit=8)
    lookahead = gather_lookahead()

    if not indices and not tickers and not portfolio:
        print("No data gathered — exiting without email.")
        return

    ai = build_ai_summary(indices, tickers, market_news, lookahead)

    today_str = date.today().strftime("%a, %b %d %Y")
    subject = f"Daily Market Review — {today_str}"
    body = compose_email(indices, tickers, portfolio, market_news, lookahead, ai)
    send_email(subject, body)
    print("Daily review email sent.")

    mark_run_success()


if __name__ == "__main__":
    main()
