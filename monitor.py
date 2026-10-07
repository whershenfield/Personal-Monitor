import smtplib
import json
import os
import csv
import io
from functools import lru_cache
from datetime import datetime, date, timedelta

import yfinance as yf
import pytz
import requests

from email.mime.text import MIMEText
from html import escape

import config


# ==================================================================
# CONFIG / ENVIRONMENT
# ==================================================================

# Override config with GitHub secrets if running in Actions
config.EMAIL_SENDER = os.environ.get(
    "EMAIL_SENDER", config.EMAIL_SENDER
)
config.EMAIL_PASSWORD = os.environ.get(
    "EMAIL_PASSWORD", config.EMAIL_PASSWORD
)
config.EMAIL_RECIPIENT = os.environ.get(
    "EMAIL_RECIPIENT", ",".join(config.EMAIL_RECIPIENT)
).split(",")
config.GROQ_API_KEY = os.environ.get(
    "GROQ_API_KEY", config.GROQ_API_KEY
)
config.GIST_ID = os.environ.get(
    "GIST_ID", config.GIST_ID
)
config.GIST_TOKEN = os.environ.get(
    "GIST_TOKEN_GITHUB", config.GIST_TOKEN
)

MARKET_TZ = pytz.timezone("America/New_York")


# Default sector ETF proxies.
# You can override these from config.py with:
# SECTOR_ETFS = {"Technology": "XLK", ...}
DEFAULT_SECTOR_ETFS = {
    "Information Technology": "XLK",
    "Communication Services": "XLC",
    "Consumer Discretionary": "XLY",
    "Industrials": "XLI",
    "Financials": "XLF",
    "Health Care": "XLV",
    "Consumer Staples": "XLP",
    "Energy": "XLE",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Materials": "XLB",
}


# Default factor/style ETF proxies.
DEFAULT_FACTOR_ETFS = {
    "Growth": "VUG",
    "Momentum": "MTUM",
    "Quality": "QUAL",
    "Value": "VTV",
    "Minimum Volatility": "USMV",
    "Small Cap": "IWM",
}


SECTOR_ETFS = getattr(
    config, "SECTOR_ETFS", DEFAULT_SECTOR_ETFS
)

FACTOR_ETFS = getattr(
    config, "FACTOR_ETFS", DEFAULT_FACTOR_ETFS
)

# Optional portfolio weights.
#
# Example in config.py:
# PORTFOLIO_WEIGHTS = {
#     "AAPL": 0.20,
#     "MSFT": 0.20,
#     "NVDA": 0.15,
#     ...
# }
#
# Weights do not need to total exactly 1.0.
PORTFOLIO_WEIGHTS = getattr(
    config, "PORTFOLIO_WEIGHTS", {}
)


# ==================================================================
# DATE HELPERS
# ==================================================================

def market_today():
    """Return the current date in New York time."""
    return datetime.now(MARKET_TZ).date()


def next_day(d):
    return d + timedelta(days=1)


# ==================================================================
# FRESH-DATA / RUN-GUARD HELPERS
# ==================================================================

def has_fresh_data(ticker):
    """Check that the latest Yahoo daily close is today's market date."""
    try:
        hist = yf.Ticker(ticker).history(period="5d")

        if hist.empty:
            return False

        latest_date = hist.index[-1].date()
        return latest_date == market_today()

    except Exception as e:
        print(f"Fresh-data check failed for {ticker}: {e}")
        return False


def get_run_state():
    headers = {
        "Authorization": f"token {config.GIST_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }

    try:
        r = requests.get(
            f"https://api.github.com/gists/{config.GIST_ID}",
            headers=headers,
            timeout=15,
        )
    except Exception as e:
        print(f"Failed to fetch Gist: {e}")
        return {}

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
    return get_run_state().get("last_run_date") == market_today().isoformat()


def mark_run_success():
    headers = {
        "Authorization": f"token {config.GIST_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }

    payload = {
        "files": {
            "run_state.json": {
                "content": json.dumps(
                    {"last_run_date": market_today().isoformat()}
                )
            }
        }
    }

    try:
        r = requests.patch(
            f"https://api.github.com/gists/{config.GIST_ID}",
            headers=headers,
            data=json.dumps(payload),
            timeout=15,
        )
    except Exception as e:
        print(f"Failed to save run state: {e}")
        return

    if r.status_code != 200:
        print(f"Failed to save run state: {r.status_code}")


# ==================================================================
# QUOTE HELPERS
# ==================================================================

def get_quote_and_ytd(ticker):
    """
    Returns:
        current_close, daily_pct, ytd_pct
    using daily closing prices rather than fast_info.last_price.
    """
    try:
        stock = yf.Ticker(ticker)

        start_date = date(market_today().year, 1, 1)
        end_date = next_day(market_today())

        hist = stock.history(
            start=start_date.isoformat(),
            end=end_date.isoformat(),
            auto_adjust=False,
        )

        if hist.empty or len(hist) < 2:
            return None, None, None

        hist = hist.dropna(subset=["Close"])

        if len(hist) < 2:
            return None, None, None

        current = float(hist["Close"].iloc[-1])
        prev_close = float(hist["Close"].iloc[-2])
        ytd_start = float(hist["Close"].iloc[0])

        if prev_close == 0 or ytd_start == 0:
            return None, None, None

        daily_pct = ((current - prev_close) / prev_close) * 100
        ytd_pct = ((current - ytd_start) / ytd_start) * 100

        return current, daily_pct, ytd_pct

    except Exception as e:
        print(f"Error fetching {ticker}: {e}")
        return None, None, None


@lru_cache(maxsize=None)
def company_name(ticker):
    try:
        info = yf.Ticker(ticker).info
        return info.get("longName") or info.get("shortName") or ticker
    except Exception:
        return ticker


# ==================================================================
# NEWS HELPERS
# ==================================================================

def get_headlines(ticker, limit=5):
    """
    Returns:
        [{"title": ..., "publisher": ..., "url": ...}, ...]
    """
    try:
        news = yf.Ticker(ticker).news or []
    except Exception:
        return []

    out = []
    seen = set()

    for a in news:
        content = a.get("content", {}) or {}

        title = content.get("title")
        if not title:
            continue

        normalized_title = title.strip().lower()

        if normalized_title in seen:
            continue

        seen.add(normalized_title)

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

        out.append(
            {
                "title": title.strip(),
                "publisher": publisher,
                "url": url,
            }
        )

        if len(out) >= limit:
            break

    return out


def gather_market_news(limit=10):
    """
    Gather broad market headlines from index, rates, commodity,
    and major market ETFs. Deduplicate by title.
    """
    seen = set()
    out = []

    sources = [
        "SPY",
        "^GSPC",
        "QQQ",
        "TLT",
        "^TNX",
        "^VIX",
        "DX-Y.NYB",
        "GC=F",
        "CL=F",
    ]

    for src in sources:
        for h in get_headlines(src, limit=8):
            title_key = h["title"].strip().lower()

            if title_key in seen:
                continue

            seen.add(title_key)
            out.append(h)

            if len(out) >= limit:
                return out

    return out


# ==================================================================
# GROUP / DASHBOARD DATA
# ==================================================================

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
                "headlines": (
                    get_headlines(t, limit=5)
                    if with_headlines
                    else []
                ),
            }
        )

    return rows


def gather_labeled_etfs(mapping):
    """
    Used for sectors and factor/style proxies.
    Returns label, ticker, daily %, YTD %.
    """
    rows = []

    for label, ticker in mapping.items():
        current, daily, ytd = get_quote_and_ytd(ticker)

        if current is None:
            print(f"Could not fetch ETF data for {label} ({ticker})")
            continue

        rows.append(
            {
                "label": label,
                "ticker": ticker,
                "price": current,
                "daily": daily,
                "ytd": ytd,
            }
        )

    return rows


# ==================================================================
# PORTFOLIO HELPERS
# ==================================================================

def calculate_weighted_portfolio_return(rows, field):
    """
    Optional weighted portfolio return.

    Returns None if no valid weights are available.
    """
    if not PORTFOLIO_WEIGHTS:
        return None

    weighted_sum = 0.0
    total_weight = 0.0

    for row in rows:
        ticker = row["ticker"]
        value = row.get(field)
        weight = PORTFOLIO_WEIGHTS.get(ticker)

        if value is None or weight is None:
            continue

        weighted_sum += value * weight
        total_weight += weight

    if total_weight == 0:
        return None

    return weighted_sum / total_weight


# ==================================================================
# FRED MACRO DATA
# ==================================================================

def get_fred_series(series_id):
    """
    Pull the latest two available observations from FRED's public CSV.
    No API key is required.
    """
    url = (
        "https://fred.stlouisfed.org/graph/"
        f"fredgraph.csv?id={series_id}"
    )

    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()

        reader = csv.DictReader(io.StringIO(r.text))

        observations = []

        for row in reader:
            raw_value = row.get(series_id)

            if raw_value in (None, "", "."):
                continue

            try:
                value = float(raw_value)
            except ValueError:
                continue

            observations.append(
                {
                    "date": row.get("observation_date"),
                    "value": value,
                }
            )

        if len(observations) < 2:
            return None

        return observations[-2], observations[-1]

    except Exception as e:
        print(f"FRED {series_id} failed: {e}")
        return None


def gather_macro_data():
    """
    Returns a cross-asset macro snapshot including:
    2Y / 10Y / 30Y yields, DXY, gold, WTI, and VIX.
    """

    data = {
        "rates": [],
        "cross_asset": [],
    }

    # Treasury yields from FRED
    treasury_series = {
        "2Y Treasury": "DGS2",
        "10Y Treasury": "DGS10",
        "30Y Treasury": "DGS30",
    }

    for label, series_id in treasury_series.items():
        result = get_fred_series(series_id)

        if not result:
            continue

        previous, latest = result

        change_bps = (
            (latest["value"] - previous["value"]) * 100
        )

        data["rates"].append(
            {
                "label": label,
                "value": latest["value"],
                "daily_bps": change_bps,
                "as_of": latest["date"],
            }
        )

    # Cross-asset market data from Yahoo Finance
    cross_asset_map = {
        "DXY": "DX-Y.NYB",
        "Gold": "GC=F",
        "WTI Crude": "CL=F",
        "VIX": "^VIX",
    }

    for label, ticker in cross_asset_map.items():
        current, daily, ytd = get_quote_and_ytd(ticker)

        if current is None:
            continue

        data["cross_asset"].append(
            {
                "label": label,
                "ticker": ticker,
                "value": current,
                "daily": daily,
                "ytd": ytd,
            }
        )

    return data


# ==================================================================
# LOOKAHEAD
# ==================================================================

def normalize_date_value(value):
    if value is None:
        return None

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    # Handles pandas Timestamp without importing pandas explicitly.
    if hasattr(value, "to_pydatetime"):
        try:
            return value.to_pydatetime().date()
        except Exception:
            pass

    if hasattr(value, "date") and callable(value.date):
        try:
            return value.date()
        except Exception:
            pass

    return None


def extract_earnings_dates(calendar):
    """
    Handles common yfinance calendar formats.
    """
    results = []

    if calendar is None:
        return results

    # Dict format
    if isinstance(calendar, dict):
        raw = calendar.get("Earnings Date")

        if raw is None:
            return results

        if not isinstance(raw, (list, tuple)):
            raw = [raw]

        for value in raw:
            d = normalize_date_value(value)
            if d:
                results.append(d)

        return results

    # DataFrame-like format
    try:
        if hasattr(calendar, "index") and "Earnings Date" in calendar.index:
            row = calendar.loc["Earnings Date"]

            if hasattr(row, "tolist"):
                values = row.tolist()
            else:
                values = [row]

            for value in values:
                d = normalize_date_value(value)
                if d:
                    results.append(d)

    except Exception:
        pass

    return results


def gather_lookahead():
    """
    Pull upcoming tracked-company earnings plus recent macro headlines
    relevant to the coming week.
    """
    lookahead = {
        "earnings": [],
        "macro": [],
    }

    today = market_today()
    horizon = today + timedelta(days=7)

    # Avoid duplicated tickers
    tracked_for_earnings = list(
        dict.fromkeys(config.TICKERS + config.PORTFOLIO)
    )

    for t in tracked_for_earnings:
        try:
            cal = yf.Ticker(t).calendar
            earnings_dates = extract_earnings_dates(cal)

            for ed in earnings_dates:
                if today <= ed <= horizon:
                    lookahead["earnings"].append(
                        {
                            "ticker": t,
                            "name": company_name(t),
                            "date": ed.isoformat(),
                        }
                    )
                    break

        except Exception as e:
            print(f"Calendar check failed for {t}: {e}")

    # Recent macro headlines for context.
    macro_keywords = [
        "fed",
        "inflation",
        "cpi",
        "ppi",
        "jobs",
        "payroll",
        "rate",
        "rates",
        "treasury",
        "yield",
        "gdp",
        "employment",
        "consumer",
        "economic",
    ]

    macro_sources = [
        "SPY",
        "^TNX",
        "^VIX",
        "TLT",
    ]

    seen = set()

    for src in macro_sources:
        for h in get_headlines(src, limit=10):
            title_lower = h["title"].lower()

            if not any(
                keyword in title_lower
                for keyword in macro_keywords
            ):
                continue

            title_key = h["title"].lower()

            if title_key in seen:
                continue

            seen.add(title_key)
            lookahead["macro"].append(h)

            if len(lookahead["macro"]) >= 6:
                return lookahead

    return lookahead


# ==================================================================
# GROQ
# ==================================================================

def _groq_call(prompt, max_tokens=6000, temperature=0.3):
    """
    Single Groq call. Returns parsed dict or None.
    """
    groq_url = "https://api.groq.com/openai/v1/chat/completions"

    headers = {
        "Authorization": f"Bearer {config.GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": "openai/gpt-oss-120b",
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [
            {
                "role": "user",
                "content": prompt,
            }
        ],
    }

    try:
        r = requests.post(
            groq_url,
            headers=headers,
            data=json.dumps(payload),
            timeout=120,
        )
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
        print(
            f"Groq non-JSON body: {r.text[:500]} ({e})"
        )
        return None

    if "choices" not in body or not body["choices"]:
        print(
            "Groq response missing choices: "
            f"{json.dumps(body)[:1000]}"
        )
        return None

    raw = body["choices"][0]["message"]["content"].strip()

    # Remove markdown code fences if returned.
    if raw.startswith("```"):
        lines = raw.splitlines()

        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]

        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]

        raw = "\n".join(lines).strip()

    # Try to isolate the JSON object.
    first_brace = raw.find("{")
    last_brace = raw.rfind("}")

    if first_brace != -1 and last_brace != -1:
        raw = raw[first_brace:last_brace + 1]

    try:
        return json.loads(raw)

    except json.JSONDecodeError as e:
        print(f"Groq JSON parse failed: {e}")
        print(f"Raw content: {raw[:2000]}")
        return None


# ==================================================================
# AI SUMMARY
# ==================================================================

def build_ai_summary(
    indices,
    sectors,
    factors,
    tickers,
    portfolio,
    macro,
    market_news,
    lookahead,
):
    def fmt_indices(rows):
        return "\n".join(
            f"{r['ticker']} ({r['name']}) | "
            f"daily {r['daily']:+.2f}% | "
            f"YTD {r['ytd']:+.2f}%"
            for r in rows
        )

    def fmt_sectors(rows):
        return "\n".join(
            f"{r['label']} ({r['ticker']}) | "
            f"daily {r['daily']:+.2f}% | "
            f"YTD {r['ytd']:+.2f}%"
            for r in rows
        )

    def fmt_factors(rows):
        return "\n".join(
            f"{r['label']} ({r['ticker']}) | "
            f"daily {r['daily']:+.2f}% | "
            f"YTD {r['ytd']:+.2f}%"
            for r in rows
        )

    def fmt_tickers(rows):
        lines = []

        for r in rows:
            lines.append(
                f"\n{r['ticker']} ({r['name']}) | "
                f"daily {r['daily']:+.2f}% | "
                f"YTD {r['ytd']:+.2f}%"
            )

            if r["headlines"]:
                for i, h in enumerate(
                    r["headlines"],
                    start=1
                ):
                    lines.append(
                        f"  [{i}] {h['title']} — "
                        f"{h['publisher']}"
                    )
            else:
                lines.append(
                    "  (no company-specific headlines available)"
                )

        return "\n".join(lines)

    def fmt_portfolio(rows):
        return "\n".join(
            f"{r['ticker']} ({r['name']}) | "
            f"daily {r['daily']:+.2f}% | "
            f"YTD {r['ytd']:+.2f}%"
            for r in rows
        )

    def fmt_macro(m):
        lines = []

        if m.get("rates"):
            lines.append("TREASURY RATES")
            for r in m["rates"]:
                lines.append(
                    f"{r['label']} | "
                    f"{r['value']:.2f}% | "
                    f"{r['daily_bps']:+.1f} bps | "
                    f"as of {r['as_of']}"
                )

        if m.get("cross_asset"):
            lines.append("\nCROSS-ASSET")
            for r in m["cross_asset"]:
                lines.append(
                    f"{r['label']} | "
                    f"value {r['value']:.2f} | "
                    f"daily {r['daily']:+.2f}% | "
                    f"YTD {r['ytd']:+.2f}%"
                )

        return "\n".join(lines) if lines else "(no macro data)"

    def fmt_news(rows):
        lines = []

        for i, h in enumerate(rows, start=1):
            lines.append(
                f"[{i}] {h['title']} — {h['publisher']}"
            )

        return "\n".join(lines) if lines else "(no market headlines)"

    def fmt_lookahead(la):
        lines = []

        if la["earnings"]:
            lines.append("UPCOMING EARNINGS")
            for e in sorted(
                la["earnings"],
                key=lambda x: x["date"]
            ):
                lines.append(
                    f"{e['date']}: {e['ticker']} "
                    f"({e['name']})"
                )

        if la["macro"]:
            lines.append("\nRECENT MACRO HEADLINES")
            for h in la["macro"]:
                lines.append(
                    f"- {h['title']} — {h['publisher']}"
                )

        return "\n".join(lines) if lines else "(no lookahead data)"

    portfolio_daily = calculate_weighted_portfolio_return(
        portfolio, "daily"
    )

    portfolio_ytd = calculate_weighted_portfolio_return(
        portfolio, "ytd"
    )

    portfolio_summary = (
        f"Weighted portfolio daily return: "
        f"{portfolio_daily:+.2f}%\n"
        f"Weighted portfolio YTD return: "
        f"{portfolio_ytd:+.2f}%"
        if portfolio_daily is not None
        and portfolio_ytd is not None
        else
        "No portfolio weights supplied; "
        "use holdings-level portfolio movements only."
    )

    prompt = f"""
You are writing a professional daily investment review for someone
building experience for a career in wealth management and asset management.

The report should read like a concise internal investment note:
data first, analysis second, implications third.

Respond with ONE JSON OBJECT ONLY.
Do not include markdown outside the JSON.

========================
MARKET DATA
========================

INDICES:
{fmt_indices(indices)}

SECTORS:
{fmt_sectors(sectors)}

FACTORS / STYLE:
{fmt_factors(factors)}

TRACKED TICKERS:
{fmt_tickers(tickers)}

PORTFOLIO MOVEMENTS:
{fmt_portfolio(portfolio)}

{portfolio_summary}

========================
MACRO / CROSS-ASSET
========================

{fmt_macro(macro)}

========================
MARKET HEADLINES
========================

{fmt_news(market_news)}

========================
LOOKING AHEAD DATA
========================

{fmt_lookahead(lookahead)}

========================
REQUIRED JSON
========================

{{
  "executive_summary": [
    "<bullet 1>",
    "<bullet 2>",
    "<bullet 3>",
    "<bullet 4>",
    "<bullet 5>"
  ],

  "tickers": {{
    "<ticker>": {{
      "note": "<2-4 sentence company-specific summary>",
      "cites": [1, 2]
    }}
  }},

  "stories": [
    {{
      "title": "<headline title>",
      "summary": "<roughly 180-250 words>",
      "source_index": 1
    }}
  ],

  "investment_implications": {{
    "bull_case": [
      "<point 1>",
      "<point 2>",
      "<point 3>"
    ],
    "bear_case": [
      "<point 1>",
      "<point 2>",
      "<point 3>"
    ],
    "key_question": "<one central investment question>"
  }},

  "looking_ahead": "<3-5 sentence forward-looking summary>",

  "bottom_line": "<2-4 sentence final assessment>"
}}

========================
RULES
========================

EXECUTIVE SUMMARY
- Write 4-5 bullets.
- Summarize the most important developments across equities,
  rates, macro, sectors, factors and cross-asset markets.
- Do NOT simply repeat every number.
- Each bullet should communicate a meaningful takeaway.

TICKER REVIEW
- Every tracked ticker MUST receive a summary.
- Do not omit small movers.
- Focus on company-specific developments, earnings, guidance,
  products, regulation, industry developments, or other
  identifiable catalysts.
- If headlines exist, ground the summary in those headlines.
- Cite headline numbers from that ticker's headline list.
- If no relevant headline exists, explicitly say that no material
  company-specific headline was surfaced in the available feed.
- Do not fabricate company events, facts or sources.
- Do not spend the entire note repeating the daily percentage move.

MARKET STORIES
- Select up to 5 of the most important DISTINCT stories.
- Each summary should be roughly 180-250 words.
- These should be meaningfully larger than the ticker summaries.
- Explain what actually happened, relevant context, why it matters,
  and what investors should monitor next.
- Do NOT duplicate the ticker-review content.
- Do NOT simply restate index, sector, factor, Treasury, commodity,
  or portfolio movements already shown elsewhere.
- Prefer stories involving macro developments, policy, geopolitics,
  market structure, major industry developments, or material
  developments that affect multiple assets.
- A company-specific story can be used only when it adds material
  information not already covered in the ticker review.
- Never invent facts or sources.
- source_index must correspond to the numbered MARKET HEADLINES list.

INVESTMENT IMPLICATIONS
- Bull case: the strongest arguments supporting current conditions.
- Bear case: the strongest risks or challenges.
- Key question: one specific question that should guide monitoring.
- These should be analytical rather than repeating the news.

LOOKING AHEAD
- Mention specific upcoming earnings where available.
- Mention specific macro developments only when visible in the
  supplied lookahead data.
- Explain what matters and why, rather than providing only a calendar.
- Be concrete rather than generic.

BOTTOM LINE
- Give a professional 2-4 sentence overall assessment.
- State whether today's market was broadly constructive,
  mixed, or defensive and explain why.
- Identify the single most important thing to monitor next.
"""

    parsed = _groq_call(
        prompt,
        max_tokens=6500,
        temperature=0.3,
    )

    if parsed is None:
        return {
            "executive_summary": [],
            "tickers": {},
            "stories": [],
            "investment_implications": {
                "bull_case": [],
                "bear_case": [],
                "key_question": "",
            },
            "looking_ahead": "",
            "bottom_line": "",
        }

    # ------------------------------
    # Executive summary
    # ------------------------------
    executive_summary = parsed.get(
        "executive_summary"
    ) or []

    if not isinstance(executive_summary, list):
        executive_summary = []

    executive_summary = [
        str(x).strip()
        for x in executive_summary
        if str(x).strip()
    ][:5]

    # ------------------------------
    # Ticker summaries
    # ------------------------------
    ticker_notes_raw = parsed.get("tickers") or {}
    ticker_notes = {}

    if isinstance(ticker_notes_raw, dict):
        for k, v in ticker_notes_raw.items():

            if isinstance(v, dict):
                note = str(
                    v.get("note") or ""
                ).strip()

                cites = v.get("cites") or []

                if not isinstance(cites, list):
                    cites = []

                cites = [
                    c for c in cites
                    if isinstance(c, int)
                ]

            else:
                note = (
                    str(v).strip()
                    if isinstance(v, str)
                    else ""
                )
                cites = []

            ticker_notes[k] = {
                "note": note,
                "cites": cites,
            }

    # ------------------------------
    # Stories
    # ------------------------------
    stories_raw = parsed.get("stories") or []
    stories = []

    if isinstance(stories_raw, list):

        for s in stories_raw[:5]:

            if not isinstance(s, dict):
                continue

            stories.append(
                {
                    "title": str(
                        s.get("title") or ""
                    ).strip(),

                    "summary": str(
                        s.get("summary") or ""
                    ).strip(),

                    "source_index": s.get(
                        "source_index"
                    ),
                }
            )

    # ------------------------------
    # Investment implications
    # ------------------------------
    implications_raw = (
        parsed.get("investment_implications")
        or {}
    )

    if not isinstance(
        implications_raw, dict
    ):
        implications_raw = {}

    bull_case = implications_raw.get(
        "bull_case"
    ) or []

    bear_case = implications_raw.get(
        "bear_case"
    ) or []

    if not isinstance(bull_case, list):
        bull_case = []

    if not isinstance(bear_case, list):
        bear_case = []

    investment_implications = {
        "bull_case": [
            str(x).strip()
            for x in bull_case
            if str(x).strip()
        ][:3],

        "bear_case": [
            str(x).strip()
            for x in bear_case
            if str(x).strip()
        ][:3],

        "key_question": str(
            implications_raw.get(
                "key_question"
            ) or ""
        ).strip(),
    }

    looking_ahead = str(
        parsed.get("looking_ahead") or ""
    ).strip()

    bottom_line = str(
        parsed.get("bottom_line") or ""
    ).strip()

    return {
        "executive_summary": executive_summary,
        "tickers": ticker_notes,
        "stories": stories,
        "investment_implications": investment_implications,
        "looking_ahead": looking_ahead,
        "bottom_line": bottom_line,
    }


# ==================================================================
# EMAIL FORMATTING
# ==================================================================

def wrap_text(text, width=78):
    words = text.split()
    lines = []
    line = ""

    for word in words:

        if len(line) + len(word) + 1 > width:
            if line:
                lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()

    if line:
        lines.append(line)

    return lines


def _fmt_quote_row(r):
    arrow = "▲" if r["daily"] >= 0 else "▼"

    name = r["name"][:24]

    return (
        f"{r['ticker']:<8} "
        f"{name:<24} "
        f"{r['daily']:>+7.2f}% {arrow}   "
        f"YTD {r['ytd']:>+7.2f}%"
    )


def _fmt_labeled_row(r):
    arrow = "▲" if r["daily"] >= 0 else "▼"

    return (
        f"{r['label']:<24} "
        f"{r['daily']:>+7.2f}% {arrow}   "
        f"YTD {r['ytd']:>+7.2f}%"
    )


def _fmt_macro_data(macro):
    lines = []

    if macro.get("rates"):
        lines.append("TREASURY RATES")
        lines.append("-" * 60)

        for r in macro["rates"]:
            arrow = "▲" if r["daily_bps"] >= 0 else "▼"

            lines.append(
                f"{r['label']:<18} "
                f"{r['value']:>6.2f}%   "
                f"{r['daily_bps']:>+6.1f} bps {arrow}"
            )

        lines.append("")

    if macro.get("cross_asset"):
        lines.append("CROSS-ASSET")
        lines.append("-" * 60)

        for r in macro["cross_asset"]:
            arrow = "▲" if r["daily"] >= 0 else "▼"

            lines.append(
                f"{r['label']:<18} "
                f"{r['value']:>10.2f}   "
                f"{r['daily']:>+7.2f}% {arrow}   "
                f"YTD {r['ytd']:>+7.2f}%"
            )

        lines.append("")

    return lines


def _fmt_ticker_block(r, ai_entry):
    header = _fmt_quote_row(r)

    note = (
        ai_entry or {}
    ).get("note", "").strip()

    cites = (
        ai_entry or {}
    ).get("cites", []) or []

    lines = [header]

    if note:
        for paragraph_line in wrap_text(
            note,
            width=74
        ):
            lines.append(
                f"    {paragraph_line}"
            )

    cited_headlines = []

    for idx in cites:

        if 1 <= idx <= len(r["headlines"]):
            cited_headlines.append(
                r["headlines"][idx - 1]
            )

    if cited_headlines:
        lines.append("    Sources:")

        for h in cited_headlines:
            publisher = h.get(
                "publisher",
                "Unknown"
            )

            url = h.get("url", "")

            if url:
                lines.append(
                    f"      • {publisher} — {url}"
                )
            else:
                lines.append(
                    f"      • {publisher}"
                )

    return "\n".join(lines)


def add_bullets(parts, items, indent="• "):
    for item in items:
        for i, line in enumerate(
            wrap_text(item)
        ):
            if i == 0:
                parts.append(
                    f"{indent}{line}"
                )
            else:
                parts.append(
                    f"  {line}"
                )


# ==================================================================
# EMAIL COMPOSITION
# ==================================================================

def compose_email(
    indices,
    sectors,
    factors,
    tickers,
    portfolio,
    macro,
    market_news,
    lookahead,
    ai,
):
    now = datetime.now(
        MARKET_TZ
    ).strftime("%A, %B %d, %Y")

    parts = [
        f"DAILY INVESTMENT REVIEW — {now}",
        "=" * 60,
        "",
    ]

    # ==============================================================
    # 1. EXECUTIVE SUMMARY
    # ==============================================================

    parts.append("1. EXECUTIVE SUMMARY")
    parts.append("-" * 60)

    summary = ai.get(
        "executive_summary",
        []
    )

    if summary:
        add_bullets(
            parts,
            summary
        )
    else:
        parts.append("(unavailable)")

    parts.append("")

    # ==============================================================
    # 2. MARKET DASHBOARD
    # ==============================================================

    parts.append("2. MARKET DASHBOARD")
    parts.append("-" * 60)

    # ------------------------------
    # Indices
    # ------------------------------
    parts.append("MAJOR INDICES")
    parts.append("-" * 60)

    for r in indices:
        parts.append(
            _fmt_quote_row(r)
        )

    parts.append("")

    # ------------------------------
    # Sectors
    # ------------------------------
    parts.append("SECTOR PERFORMANCE")
    parts.append("-" * 60)

    sector_rows = sorted(
        sectors,
        key=lambda x: x["daily"],
        reverse=True,
    )

    for r in sector_rows:
        parts.append(
            _fmt_labeled_row(r)
        )

    parts.append("")

    # ------------------------------
    # Factors
    # ------------------------------
    parts.append("FACTOR / STYLE PERFORMANCE")
    parts.append("-" * 60)

    factor_rows = sorted(
        factors,
        key=lambda x: x["daily"],
        reverse=True,
    )

    for r in factor_rows:
        parts.append(
            _fmt_labeled_row(r)
        )

    parts.append("")

    # ------------------------------
    # Tracked tickers
    # ------------------------------
    parts.append("TRACKED TICKERS")
    parts.append("-" * 60)

    for r in tickers:
        parts.append(
            _fmt_quote_row(r)
        )

    parts.append("")

    # ------------------------------
    # Portfolio movements
    # ------------------------------
    parts.append("PORTFOLIO MOVEMENTS")
    parts.append("-" * 60)

    portfolio_daily = (
        calculate_weighted_portfolio_return(
            portfolio,
            "daily"
        )
    )

    portfolio_ytd = (
        calculate_weighted_portfolio_return(
            portfolio,
            "ytd"
        )
    )

    if (
        portfolio_daily is not None
        and portfolio_ytd is not None
    ):
        parts.append(
            f"Weighted portfolio: "
            f"{portfolio_daily:+.2f}% today | "
            f"YTD {portfolio_ytd:+.2f}%"
        )
        parts.append("")

    for r in portfolio:
        parts.append(
            _fmt_quote_row(r)
        )

    parts.append("")

    # ==============================================================
    # 3. RATES, MACRO & CROSS-ASSET
    # ==============================================================

    parts.append("3. RATES, MACRO & CROSS-ASSET")
    parts.append("-" * 60)

    macro_lines = _fmt_macro_data(
        macro
    )

    if macro_lines:
        parts.extend(macro_lines)
    else:
        parts.append("(unavailable)")
        parts.append("")

    # ==============================================================
    # 4. TICKER REVIEW
    # ==============================================================

    parts.append("4. TICKER REVIEW")
    parts.append("-" * 60)

    ticker_ai = ai.get(
        "tickers",
        {}
    )

    for r in tickers:
        parts.append(
            _fmt_ticker_block(
                r,
                ticker_ai.get(
                    r["ticker"],
                    {}
                ),
            )
        )
        parts.append("")

    # ==============================================================
    # 5. TOP MARKET STORIES
    # ==============================================================

    parts.append("5. TOP MARKET STORIES")
    parts.append("-" * 60)

    stories = ai.get(
        "stories",
        []
    )

    if not stories:
        parts.append("(unavailable)")
        parts.append("")

    for story in stories:

        title = story.get(
            "title",
            ""
        ).strip()

        summary = story.get(
            "summary",
            ""
        ).strip()

        source_index = story.get(
            "source_index"
        )

        if title:
            parts.append(
                title.upper()
            )
            parts.append("")

        if summary:
            parts.extend(
                wrap_text(
                    summary,
                    width=78
                )
            )
            parts.append("")

        if (
            isinstance(source_index, int)
            and 1 <= source_index
            <= len(market_news)
        ):
            source = market_news[
                source_index - 1
            ]

            publisher = source.get(
                "publisher",
                "Unknown"
            )

            url = source.get(
                "url",
                ""
            )

            if url:
                parts.append(
                    f"Source: {publisher} — {url}"
                )
            else:
                parts.append(
                    f"Source: {publisher}"
                )

        parts.append("")
        parts.append("-" * 60)
        parts.append("")

    # ==============================================================
    # 6. INVESTMENT IMPLICATIONS
    # ==============================================================

    parts.append("6. INVESTMENT IMPLICATIONS")
    parts.append("-" * 60)

    implications = ai.get(
        "investment_implications",
        {}
    )

    parts.append("BULL CASE")
    parts.append("-" * 30)

    bull_case = implications.get(
        "bull_case",
        []
    )

    if bull_case:
        add_bullets(
            parts,
            bull_case
        )
    else:
        parts.append("(unavailable)")

    parts.append("")
    parts.append("BEAR CASE")
    parts.append("-" * 30)

    bear_case = implications.get(
        "bear_case",
        []
    )

    if bear_case:
        add_bullets(
            parts,
            bear_case
        )
    else:
        parts.append("(unavailable)")

    parts.append("")

    parts.append("KEY QUESTION")
    parts.append("-" * 30)

    key_question = implications.get(
        "key_question",
        ""
    ).strip()

    if key_question:
        parts.extend(
            wrap_text(
                key_question
            )
        )
    else:
        parts.append("(unavailable)")

    parts.append("")

    # ==============================================================
    # 7. LOOKING AHEAD
    # ==============================================================

    parts.append("7. LOOKING AHEAD")
    parts.append("-" * 60)

    if lookahead["earnings"]:
        parts.append("UPCOMING EARNINGS")
        parts.append("-" * 30)

        for e in sorted(
            lookahead["earnings"],
            key=lambda x: x["date"]
        ):
            parts.append(
                f"{e['date']}: "
                f"{e['ticker']} "
                f"({e['name']})"
            )

        parts.append("")

    if lookahead["macro"]:
        parts.append(
            "MACRO DEVELOPMENTS TO MONITOR"
        )
        parts.append("-" * 30)

        for h in lookahead["macro"]:
            parts.append(
                f"• {h['title']} — "
                f"{h['publisher']}"
            )

        parts.append("")

    looking_ahead_text = ai.get(
        "looking_ahead",
        ""
    ).strip()

    if looking_ahead_text:
        parts.extend(
            wrap_text(
                looking_ahead_text,
                width=78
            )
        )
    else:
        parts.append("(unavailable)")

    parts.append("")

    # ==============================================================
    # 8. BOTTOM LINE
    # ==============================================================

    parts.append("8. BOTTOM LINE")
    parts.append("-" * 60)

    bottom_line = ai.get(
        "bottom_line",
        ""
    ).strip()

    if bottom_line:
        parts.extend(
            wrap_text(
                bottom_line,
                width=78
            )
        )
    else:
        parts.append("(unavailable)")

    parts.append("")
    parts.append("-" * 60)
    parts.append(
        "Generated automatically via Yahoo Finance, FRED & Groq"
    )

    return "\n".join(parts)


# ==================================================================
# EMAIL
# ==================================================================

def send_email(subject, body):
    """
    Send the report as HTML using a monospace font so all
    fixed-width columns remain aligned.
    """

    html_body = f"""
    <html>
        <body>
            <pre style="
                font-family: Menlo, Monaco, Consolas, 'Courier New', monospace;
                font-size: 13px;
                line-height: 1.45;
                white-space: pre-wrap;
            ">{escape(body)}</pre>
        </body>
    </html>
    """

    msg = MIMEText(html_body, "html")
    msg["Subject"] = subject
    msg["From"] = config.EMAIL_SENDER
    msg["To"] = ", ".join(config.EMAIL_RECIPIENT)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(
            config.EMAIL_SENDER,
            config.EMAIL_PASSWORD
        )
        server.sendmail(
            config.EMAIL_SENDER,
            config.EMAIL_RECIPIENT,
            msg.as_string()
        )


# ==================================================================
# MAIN
# ==================================================================

def main():

    # --------------------------------------------------------------
    # Run guard
    # --------------------------------------------------------------

    if not has_fresh_data("^GSPC"):
        print(
            "No fresh market data for today — exiting."
        )
        return

    if already_ran_today():
        print(
            "Already ran today — exiting."
        )
        return

    # --------------------------------------------------------------
    # Gather data
    # --------------------------------------------------------------

    print("Gathering indices...")
    indices = gather_group(
        config.INDICES,
        with_headlines=False
    )

    print("Gathering tracked tickers...")
    tickers = gather_group(
        config.TICKERS,
        with_headlines=True
    )

    print("Gathering portfolio...")
    portfolio = gather_group(
        config.PORTFOLIO,
        with_headlines=False
    )

    print("Gathering sectors...")
    sectors = gather_labeled_etfs(
        SECTOR_ETFS
    )

    print("Gathering factors...")
    factors = gather_labeled_etfs(
        FACTOR_ETFS
    )

    print("Gathering macro data...")
    macro = gather_macro_data()

    print("Gathering market headlines...")
    market_news = gather_market_news(
        limit=10
    )

    print("Gathering lookahead...")
    lookahead = gather_lookahead()

    # --------------------------------------------------------------
    # Basic data check
    # --------------------------------------------------------------

    if not indices and not tickers and not portfolio:
        print(
            "No data gathered — exiting without email."
        )
        return

    # --------------------------------------------------------------
    # AI analysis
    # --------------------------------------------------------------

    print("Building AI analysis...")

    ai = build_ai_summary(
        indices=indices,
        sectors=sectors,
        factors=factors,
        tickers=tickers,
        portfolio=portfolio,
        macro=macro,
        market_news=market_news,
        lookahead=lookahead,
    )

    # --------------------------------------------------------------
    # Compose and send
    # --------------------------------------------------------------

    today_str = market_today().strftime(
        "%a, %b %d %Y"
    )

    subject = (
        f"Daily Investment Review — "
        f"{today_str}"
    )

    body = compose_email(
        indices=indices,
        sectors=sectors,
        factors=factors,
        tickers=tickers,
        portfolio=portfolio,
        macro=macro,
        market_news=market_news,
        lookahead=lookahead,
        ai=ai,
    )

    send_email(
        subject,
        body
    )

    print(
        "Daily investment review email sent."
    )

    mark_run_success()


if __name__ == "__main__":
    main()
