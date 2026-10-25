import smtplib
import json
import os
import csv
import io
import re
from functools import lru_cache
from datetime import datetime, date, time as dtime, timedelta
from email.utils import parsedate_to_datetime

import yfinance as yf
import pytz
import requests

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from urllib.parse import urlparse

import config


# Configuration and environment overrides

config.EMAIL_SENDER = os.environ.get(
    "EMAIL_SENDER", config.EMAIL_SENDER
)

config.EMAIL_PASSWORD = os.environ.get(
    "EMAIL_PASSWORD", config.EMAIL_PASSWORD
)

config.EMAIL_RECIPIENT = os.environ.get(
    "EMAIL_RECIPIENT",
    ",".join(config.EMAIL_RECIPIENT)
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

config.TAVILY_API_KEY = os.environ.get(
    "TAVILY_API_KEY",
    getattr(config, "TAVILY_API_KEY", ""),
)

MARKET_TZ = pytz.timezone("America/New_York")


# Default sector ETF proxies; can be overridden in config.py.
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


# Default factor/style ETF proxies; can be overridden in config.py.
# AUDIT FIX: the previous map mixed index families (CRSP growth/value via
# VUG/VTV next to MSCI factor funds). Style now uses the Russell 1000
# Growth/Value pair (the institutional style benchmarks), factors use the
# iShares MSCI USA factor family, and equal weight is added as a breadth /
# concentration gauge. Each row is also shown relative to FACTOR_BENCHMARK so
# the factor effect is separated from market beta.
DEFAULT_FACTOR_ETFS = {
    "Growth (Russell 1000 Growth)": "IWF",
    "Value (Russell 1000 Value)": "IWD",
    "Momentum (MSCI USA Momentum)": "MTUM",
    "Quality (MSCI USA Quality)": "QUAL",
    "Minimum Volatility (MSCI USA Min Vol)": "USMV",
    "Small Cap (Russell 2000)": "IWM",
    "Equal Weight (S&P 500 Equal Weight)": "RSP",
}


SECTOR_ETFS = getattr(
    config,
    "SECTOR_ETFS",
    DEFAULT_SECTOR_ETFS,
)

FACTOR_ETFS = getattr(
    config,
    "FACTOR_ETFS",
    DEFAULT_FACTOR_ETFS,
)


# Optional custom security names.
TICKER_NAMES = getattr(
    config,
    "TICKER_NAMES",
    {},
)


# Optional portfolio weights for weighted returns.
PORTFOLIO_WEIGHTS = getattr(
    config,
    "PORTFOLIO_WEIGHTS",
    {},
)

INDEX_PORTFOLIO = getattr(
    config,
    "INDEX_PORTFOLIO",
    [],
)

INDEX_PORTFOLIO_WEIGHTS = getattr(
    config,
    "INDEX_PORTFOLIO_WEIGHTS",
    {},
)

AVANTIS_PORTFOLIO = getattr(
    config,
    "AVANTIS_PORTFOLIO",
    [],
)

AVANTIS_PORTFOLIO_WEIGHTS = getattr(
    config,
    "AVANTIS_PORTFOLIO_WEIGHTS",
    {},
)


# Benchmark used for factor "vs market" returns (total-return ETF so it is
# comparable with the dividend-adjusted factor ETF returns).
FACTOR_BENCHMARK = getattr(
    config,
    "FACTOR_BENCHMARK",
    "SPY",
)


# Earnings screens. Market caps are in USD.
UPCOMING_EARNINGS_MIN_MARKET_CAP = getattr(
    config, "UPCOMING_EARNINGS_MIN_MARKET_CAP", 50e9
)
RECENT_EARNINGS_MIN_MARKET_CAP = getattr(
    config, "RECENT_EARNINGS_MIN_MARKET_CAP", 20e9
)
EARNINGS_LOOKAHEAD_DAYS = getattr(
    config, "EARNINGS_LOOKAHEAD_DAYS", 7
)
MAX_UPCOMING_EARNINGS = getattr(
    config, "MAX_UPCOMING_EARNINGS", 20
)
MAX_RECENT_EARNINGS = getattr(
    config, "MAX_RECENT_EARNINGS", 10
)


# AUDIT: Canadian-listed, unhedged ETFs compared with their USD equivalent.
# Their return should equal (1 + USD ETF return) * (1 + USD/CAD change) - 1.
# Any large residual points to a data problem rather than a real return gap.
CROSS_LISTING_CHECKS = getattr(
    config,
    "CROSS_LISTING_CHECKS",
    {
        "ZSP.TO": ("SPY", "CAD=X"),
        "ZEA.TO": ("EFA", "CAD=X"),
    },
)


# Date helpers

def market_today():
    """Return the current calendar date in New York time."""
    return datetime.now(MARKET_TZ).date()


def next_day(d):
    return d + timedelta(days=1)


# AUDIT FIX: every return in the report is anchored to one explicit US
# session date and the previous US session date, both read from ^GSPC.
# Previously each instrument used its own last two rows, so any symbol whose
# latest bar was missing or still NaN silently reported the PRIOR day's move.
SESSION = {
    "date": None,
    "prev_date": None,
}


def session_date():
    return SESSION["date"] or market_today()


def previous_close_dt():
    """Timestamp of the previous US session's 4:00 pm ET close."""

    prev = SESSION["prev_date"] or (session_date() - timedelta(days=1))
    return MARKET_TZ.localize(datetime.combine(prev, dtime(16, 0)))


# Data-quality audit log, rendered at the end of the email.

AUDIT_LOG = []


def audit(message, level="WARN"):
    """Record a data-quality finding for the console and the email."""

    print(f"[AUDIT {level}] {message}")
    AUDIT_LOG.append({"level": level, "message": message})


# Fresh-data and duplicate-run checks

def has_fresh_data(ticker):
    """
    Check whether Yahoo Finance has a daily observation for today and record
    today's and the previous session dates for every later calculation.
    """

    try:
        hist = yf.Ticker(ticker).history(
            period="10d"
        )

        hist = hist.dropna(subset=["Close"])

        if len(hist) < 2:
            return False

        dates = sorted({ts.date() for ts in hist.index})

        latest_date = dates[-1]

        if latest_date != market_today():
            return False

        SESSION["date"] = latest_date
        SESSION["prev_date"] = dates[-2]

        print(
            f"Session date {SESSION['date']} "
            f"(previous close {SESSION['prev_date']})"
        )

        return True

    except Exception as e:
        print(
            f"Fresh-data check failed for {ticker}: {e}"
        )
        return False


def get_run_state():
    headers = {
        "Authorization": (
            f"token {config.GIST_TOKEN}"
        ),
        "Accept": (
            "application/vnd.github.v3+json"
        ),
    }

    try:
        r = requests.get(
            f"https://api.github.com/gists/"
            f"{config.GIST_ID}",
            headers=headers,
            timeout=15,
        )
    except Exception as e:
        print(
            f"Failed to fetch Gist: {e}"
        )
        return {}

    if r.status_code != 200:
        print(
            f"Failed to fetch Gist: "
            f"{r.status_code}"
        )
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
    return (
        get_run_state().get("last_run_date")
        == market_today().isoformat()
    )


def mark_run_success():
    headers = {
        "Authorization": (
            f"token {config.GIST_TOKEN}"
        ),
        "Accept": (
            "application/vnd.github.v3+json"
        ),
    }

    payload = {
        "files": {
            "run_state.json": {
                "content": json.dumps(
                    {
                        "last_run_date":
                            market_today().isoformat()
                    }
                )
            }
        }
    }

    try:
        r = requests.patch(
            f"https://api.github.com/gists/"
            f"{config.GIST_ID}",
            headers=headers,
            data=json.dumps(payload),
            timeout=15,
        )
    except Exception as e:
        print(
            f"Failed to save run state: {e}"
        )
        return

    if r.status_code != 200:
        print(
            f"Failed to save run state: "
            f"{r.status_code}"
        )


# Quote and performance helpers

@lru_cache(maxsize=None)
def get_quote(ticker):
    """
    Return price, daily and YTD performance for one symbol as a dict:

        price    unadjusted closing price for display
        daily    % change vs the ADJUSTED previous close (None if stale)
        ytd      % change vs the ADJUSTED prior-year final close
        as_of    date of the bar used
        stale    True when the symbol has no bar for today's US session

    AUDIT FIXES:
    - Returns use dividend/split-adjusted closes ("Adj Close"), so an
      ex-dividend day no longer shows up as a loss.
    - The current bar must be dated on today's US session. If it is missing
      (Yahoo lag, a NaN row still being finalised, or a foreign exchange
      holiday) the row is flagged stale instead of silently reporting the
      previous day's move.
    - YTD is measured from the last close of the PRIOR year. The old code used
      the first close of this year, which dropped the first session's return.
    - Symbols that print a bar dated after the US session (e.g. FX on a
      weekend) are cut off at the session date.
    """

    session = session_date()

    try:
        hist = yf.Ticker(ticker).history(
            start=date(session.year - 1, 12, 1).isoformat(),
            end=(session + timedelta(days=2)).isoformat(),
            auto_adjust=False,
            actions=False,
        )
    except Exception as e:
        print(f"Error fetching {ticker}: {e}")
        return None

    if hist is None or hist.empty or "Close" not in hist:
        return None

    adj_col = "Adj Close" if "Adj Close" in hist else "Close"
    hist = hist.dropna(subset=["Close", adj_col])

    # One row per calendar date (yfinance can append a duplicate live row).
    hist.index = [ts.date() for ts in hist.index]
    hist = hist[~hist.index.duplicated(keep="last")]
    hist = hist[hist.index <= session]

    if len(hist) < 2:
        return None

    as_of = hist.index[-1]
    price = float(hist["Close"].iloc[-1])
    adj = hist[adj_col].astype(float)

    stale = as_of != session

    daily = None
    if not stale:
        prev_adj = float(adj.iloc[-2])
        if prev_adj:
            daily = (float(adj.iloc[-1]) / prev_adj - 1) * 100

    year_start = date(session.year, 1, 1)
    prior_year = adj[adj.index < year_start]

    if not prior_year.empty:
        ytd_base = float(prior_year.iloc[-1])
    else:
        # Listed this year: fall back to the first available close.
        ytd_base = float(adj.iloc[0])
        audit(
            f"{ticker}: no prior-year close available; YTD measured from "
            f"first close on {adj.index[0]}."
        )

    ytd = (float(adj.iloc[-1]) / ytd_base - 1) * 100 if ytd_base else None

    if stale:
        audit(
            f"{ticker}: no bar for the {session} session (latest is {as_of}); "
            "daily return shown as n/a rather than the prior day's move."
        )

    return {
        "price": price,
        "daily": daily,
        "ytd": ytd,
        "as_of": as_of,
        "stale": stale,
    }


def get_after_hours(ticker):
    """
    Return the post-market move vs today's regular close, or None.

    Only quotes stamped after today's 4:00 pm ET close are used, so a stale
    post-market print from the previous evening is never reported.
    """

    try:
        info = yf.Ticker(ticker).info or {}
    except Exception as e:
        print(f"After-hours quote failed for {ticker}: {e}")
        return None

    post_price = info.get("postMarketPrice")
    regular = info.get("regularMarketPrice")
    post_time = info.get("postMarketTime")

    if not post_price or not regular or not post_time:
        return None

    try:
        post_dt = datetime.fromtimestamp(int(post_time), MARKET_TZ)
    except Exception:
        return None

    close_dt = MARKET_TZ.localize(datetime.combine(session_date(), dtime(16, 0)))

    if post_dt < close_dt:
        return None

    return {
        "price": float(post_price),
        "pct": (float(post_price) / float(regular) - 1) * 100,
        "time": post_dt.strftime("%I:%M %p ET").lstrip("0"),
    }


# Built-in display names for the symbols used by this report. Values in
# config.TICKER_NAMES override these defaults.
DEFAULT_TICKER_NAMES = {
    "^GSPC": "S&P 500 Index",
    "^IXIC": "Nasdaq Composite Index",
    "^RUT": "Russell 2000 Index",
    "^DJI": "Dow Jones Industrial Average",
    # AUDIT: EFA is the correct, most liquid proxy for MSCI EAFE; Yahoo does
    # not carry the MSCI index itself. It is priced in USD (includes FX) and
    # trades during US hours, so it reflects EAFE through the US close.
    "EFA": "iShares MSCI EAFE ETF (USD)",
    "SPY": "SPDR S&P 500 ETF",
    "^GSPTSE": "S&P/TSX Composite Index",
    "EL.PA": "EssilorLuxottica",
    "SPGI": "S&P Global",
    "CAUS.TO": "Avantis CIBC U.S. Equity ETF",
    "CALV.TO": "Avantis CIBC U.S. Large Cap Value ETF",
    "VCN.TO": "Vanguard FTSE Canada All Cap Index ETF",
    "CADE.TO": "Avantis CIBC International Equity ETF",
    # AUDIT: ZSP.TO is the correct ticker (unhedged, CAD). Its gap vs ^GSPC is
    # the USD/CAD move plus dividends (^GSPC is price-only). See the
    # cross-listing check in the Data Quality section of the email.
    "ZSP.TO": "BMO S&P 500 Index ETF (CAD, unhedged)",
    "ZEA.TO": "BMO MSCI EAFE Index ETF (CAD)",
    "CACE.TO": "Avantis CIBC Canadian Equity ETF",
}

@lru_cache(maxsize=None)
def company_name(ticker):
    """Return a readable display name for a tracked stock, index, or ETF."""

    if ticker in TICKER_NAMES:
        return TICKER_NAMES[ticker]

    if ticker in DEFAULT_TICKER_NAMES:
        return DEFAULT_TICKER_NAMES[ticker]

    # AUDIT FIX: unknown tickers used to fall back to the bare symbol, which
    # made the company-news search ("AAPL" AAPL ...) far less precise.
    try:
        info = yf.Ticker(ticker).info or {}
        name = info.get("longName") or info.get("shortName")
        if name:
            return str(name).strip()
    except Exception as e:
        print(f"Name lookup failed for {ticker}: {e}")

    return ticker


def parse_published(value):
    """Parse a news timestamp (ISO 8601 or RFC 2822) into an aware datetime."""

    if not value:
        return None

    text = str(value).strip()

    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None

    if dt.tzinfo is None:
        dt = pytz.utc.localize(dt)

    return dt


def is_recent(published, cutoff):
    """True when a timestamp is on or after the cutoff (undated → False)."""

    return published is not None and published >= cutoff


def news_cutoff(hours_before_prev_close=0):
    """Earliest publish time accepted for 'today' news."""

    return previous_close_dt() - timedelta(hours=hours_before_prev_close)


def search_days():
    """Tavily 'days' window that covers back to the previous session close."""

    return max(1, (market_today() - previous_close_dt().date()).days + 1)


def news_publisher(result):
    """Return the publisher name, falling back to the article URL's domain."""

    source = str(result.get("source") or "").strip()
    if source:
        return source

    url = str(result.get("url") or "").strip()
    hostname = urlparse(url).hostname or ""
    hostname = hostname.lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]

    return hostname or "Unknown source"

# News and market-group helpers

def get_headlines(ticker, limit=5, cutoff=None):
    """
    Return deduplicated Yahoo Finance headlines for a ticker, newest first.

    AUDIT FIX: Yahoo's feed mixes in stories that are days old, which is why
    ticker summaries described the prior day. Only stories published since
    `cutoff` (default: the previous session's close) are kept.
    """

    if cutoff is None:
        cutoff = news_cutoff()

    try:
        news = (
            yf.Ticker(ticker).news
            or []
        )

    except Exception as e:
        print(
            f"Yahoo headlines failed for {ticker}: {e}"
        )
        return []

    out = []
    seen = set()

    for article in news:

        content = (
            article.get("content", {})
            or {}
        )

        title = content.get(
            "title"
        )

        if not title:
            continue

        title = title.strip()

        published = parse_published(
            content.get("pubDate")
            or content.get("displayTime")
            or article.get("providerPublishTime")
        )

        if published is None and isinstance(
            article.get("providerPublishTime"), (int, float)
        ):
            published = datetime.fromtimestamp(
                article["providerPublishTime"], pytz.utc
            )

        if not is_recent(published, cutoff):
            continue

        normalized_title = (
            title.lower()
        )

        if normalized_title in seen:
            continue

        seen.add(
            normalized_title
        )

        publisher = (
            content.get(
                "provider",
                {}
            ).get(
                "displayName"
            )
            or article.get(
                "publisher"
            )
            or "Unknown"
        )

        url = (
            content.get(
                "canonicalUrl",
                {}
            ).get("url")
            or content.get(
                "clickThroughUrl",
                {}
            ).get("url")
            or article.get(
                "link"
            )
            or ""
        )

        out.append(
            {
                "title": title,
                "publisher": publisher,
                "url": url,
                "published": published,
            }
        )

    out.sort(key=lambda h: h["published"], reverse=True)

    return out[:limit]



def gather_group(
    tickers,
    with_headlines=False,
    headline_limit=5,
    with_after_hours=False,
):
    rows = []

    for ticker in tickers:
        quote = get_quote(ticker)

        if quote is None:
            audit(f"{ticker}: no quote data available; row omitted.")
            continue

        row = {
            "ticker": ticker,
            "name": company_name(ticker),
            "price": quote["price"],
            "daily": quote["daily"],
            "ytd": quote["ytd"],
            "as_of": quote["as_of"],
            "stale": quote["stale"],
            "after_hours": None,
            "headlines": [],
        }

        if with_after_hours:
            row["after_hours"] = get_after_hours(ticker)

        if with_headlines:
            yahoo_headlines = get_headlines(ticker, limit=headline_limit)
            company_headlines = search_company_news(
                ticker, limit=headline_limit
            )

            combined = []
            seen_titles = set()

            for headline in yahoo_headlines + company_headlines:
                title = headline.get("title", "").strip()
                key = title.lower()

                if title and key not in seen_titles:
                    seen_titles.add(key)
                    combined.append(headline)

            # Newest first so the model leads with today's catalyst.
            combined.sort(
                key=lambda h: h.get("published")
                or datetime.min.replace(tzinfo=pytz.utc),
                reverse=True,
            )

            row["headlines"] = combined[:headline_limit]

        rows.append(row)

    return rows


def gather_market_news(limit=10):
    """
    Search the current web for major financial-market stories.

    This is deliberately broad rather than ticker-driven. The goal is
    to surface the day's important investor-relevant stories, similar
    to a concise financial front page, and then let Groq select the
    most important distinct stories.
    """

    print(
        "Gathering market news from the web..."
    )

    api_key = getattr(
        config,
        "TAVILY_API_KEY",
        "",
    )

    if not api_key:
        print(
            "TAVILY_API_KEY is not configured."
        )
        return []

    today = session_date()
    cutoff = news_cutoff()

    queries = [
        (
            "major financial market news "
            "stocks bonds economy today "
            f"{today.isoformat()}"
        ),
        (
            "Wall Street markets Federal Reserve "
            "interest rates inflation today "
            f"{today.isoformat()}"
        ),
        (
            "major business financial news "
            "investors today "
            f"{today.isoformat()}"
        ),
        (
            "oil gold dollar Treasury markets "
            f"today {today.isoformat()}"
        ),
    ]

    per_query = []
    undated = []
    seen_urls = set()
    seen_titles = set()
    dropped_old = 0

    for query in queries:

        results = []
        per_query.append(results)

        try:

            response = requests.post(
                "https://api.tavily.com/search",
                headers={
                    "Content-Type":
                        "application/json",
                },
                json={
                    "api_key": api_key,
                    "query": query,
                    "search_depth": "advanced",
                    "topic": "news",
                    "days": search_days(),
                    "max_results": limit,
                    "include_answer": False,
                },
                timeout=30,
            )

            if response.status_code != 200:

                print(
                    "Tavily search failed: "
                    f"{response.status_code} "
                    f"{response.text[:500]}"
                )

                continue

            data = response.json()

        except Exception as e:

            print(
                f"News search exception: {e}"
            )

            continue

        for result in (
            data.get("results", [])
        ):

            title = str(
                result.get(
                    "title",
                    ""
                )
                or ""
            ).strip()

            url = str(
                result.get(
                    "url",
                    ""
                )
                or ""
            ).strip()

            content = str(
                result.get(
                    "content",
                    ""
                )
                or ""
            ).strip()

            if not title or not url:
                continue

            # AUDIT FIX: Tavily's "days" window is coarse; drop anything
            # published before the previous session's close.
            published = parse_published(
                result.get("published_date")
            )

            if published is not None and published < cutoff:
                dropped_old += 1
                continue

            title_key = (
                title.lower()
            )

            if (
                url in seen_urls
                or title_key in seen_titles
            ):
                continue

            seen_urls.add(url)
            seen_titles.add(title_key)

            # Undated results are only used if dated coverage is thin.
            (results if published else undated).append(
                {
                    "title": title,
                    "publisher": news_publisher(result),
                    "url": url,
                    "snippet": content,
                    "published": published,
                }
            )

    # AUDIT FIX: the old code took the first `limit` results overall, so the
    # first query filled the whole list (the opposite of the comment's
    # intent). Interleave the queries round-robin instead.
    results = []
    depth = max((len(r) for r in per_query), default=0)

    for i in range(depth):
        for query_results in per_query:
            if i < len(query_results):
                results.append(query_results[i])

    if len(results) < 3 and undated:
        audit(
            f"Only {len(results)} dated market-news results since the "
            f"previous close; added {min(len(undated), limit)} undated results."
        )
        results.extend(undated)

    results = results[:limit]

    if dropped_old:
        print(
            f"Dropped {dropped_old} market-news results published "
            f"before the previous close"
        )

    print(
        "Total market news results collected: "
        f"{len(results)}"
    )

    for i, result in enumerate(
        results,
        start=1,
    ):

        print(
            f"  [{i}] "
            f"{result['title']} — "
            f"{result['publisher']}"
        )

    return results

def search_company_news(ticker, limit=5, name=None, topic="stock news", cutoff=None):
    """
    Search broadly for company-specific news using Tavily.

    AUDIT FIX: the search window was 7 days with no date filter, so
    summaries regularly described last week's or yesterday's news as
    today's catalyst. Results are now limited to stories published since the
    previous session's close and carry their publish time into the prompt.
    """

    if cutoff is None:
        cutoff = news_cutoff()

    api_key = getattr(
        config,
        "TAVILY_API_KEY",
        "",
    )

    if not api_key:
        return []

    name = name or company_name(ticker)

    try:
        response = requests.post(
            "https://api.tavily.com/search",
            headers={
                "Content-Type": "application/json",
            },
            json={
                "api_key": api_key,
                "query": (
                    f'"{name}" {ticker} {topic} '
                    f'{session_date().strftime("%B %d, %Y")}'
                ),
                "topic": "news",
                "search_depth": "advanced",
                "days": search_days(),
                "max_results": limit,
                "include_answer": False,
            },
            timeout=30,
        )

        if response.status_code != 200:
            print(
                f"Company news search failed for "
                f"{ticker}: {response.status_code}"
            )
            return []

        results = response.json().get(
            "results",
            [],
        )

    except Exception as e:
        print(
            f"Company news search exception for "
            f"{ticker}: {e}"
        )
        return []

    headlines = []
    seen = set()

    for result in results:
        title = str(
            result.get("title", "") or ""
        ).strip()

        url = str(
            result.get("url", "") or ""
        ).strip()

        if not title or not url:
            continue

        published = parse_published(
            result.get("published_date")
        )

        if not is_recent(published, cutoff):
            continue

        key = title.lower()

        if key in seen:
            continue

        seen.add(key)

        headlines.append({
            "title": title,
            "publisher": news_publisher(result),
            "url": url,
            "snippet": str(
                result.get("content", "") or ""
            ).strip(),
            "published": published,
        })

    return headlines


def gather_labeled_etfs(mapping, benchmark=None):
    """
    Gather performance for sector and factor ETF proxies.

    When a benchmark is given, each row also carries its daily and YTD
    return relative to that benchmark (simple difference in % points).
    """

    bench = get_quote(benchmark) if benchmark else None

    rows = []

    for label, ticker in mapping.items():

        quote = get_quote(ticker)

        if quote is None:
            audit(
                f"Could not fetch ETF data "
                f"for {label} ({ticker})"
            )
            continue

        row = {
            "label": label,
            "ticker": ticker,
            "price": quote["price"],
            "daily": quote["daily"],
            "ytd": quote["ytd"],
            "stale": quote["stale"],
            "rel_daily": None,
            "rel_ytd": None,
        }

        if bench:
            if row["daily"] is not None and bench["daily"] is not None:
                row["rel_daily"] = row["daily"] - bench["daily"]
            if row["ytd"] is not None and bench["ytd"] is not None:
                row["rel_ytd"] = row["ytd"] - bench["ytd"]

        rows.append(row)

    return rows


# Portfolio calculations

def calculate_weighted_portfolio_return(
    rows,
    field,
    weights=None,
):
    """Calculate a weighted return using the supplied portfolio weights."""

    if weights is None:
        weights = PORTFOLIO_WEIGHTS

    if not weights:
        return None

    weighted_sum = 0.0
    total_weight = 0.0

    for row in rows:

        ticker = row["ticker"]
        value = row.get(field)
        weight = weights.get(ticker)

        # Stale rows have daily=None and are excluded; see
        # portfolio_weight_coverage() for how much weight that leaves out.
        if value is None:
            continue

        if weight is None:
            continue

        weighted_sum += (
            value * weight
        )

        total_weight += weight

    if total_weight == 0:
        return None

    return (
        weighted_sum
        / total_weight
    )


def portfolio_weight_coverage(rows, field, weights):
    """
    Share of total portfolio weight that has a usable value for `field`.

    AUDIT: the weighted return renormalises over the holdings that have data.
    Without this check a missing or stale holding silently changes the
    portfolio's composition for the day.
    """

    total = sum(w for w in weights.values() if w)

    if not total:
        return None

    covered = sum(
        weights.get(r["ticker"]) or 0
        for r in rows
        if r.get(field) is not None
    )

    return covered / total


# Treasury / FRED macro data

def get_treasury_curve(columns):
    """
    Pull the two latest observations for each requested tenor from the U.S.
    Treasury's official daily par yield curve.

    AUDIT FIX: FRED's DGS series are republished the following business day,
    so at a post-close run FRED's "latest" value is usually YESTERDAY's yield
    and the bps change is the prior day's change. Treasury publishes the
    same-day curve in the late afternoon; FRED is kept only as a fallback.

    Returns {column: (previous, latest)} with each item {"date", "value"}.
    """

    session = session_date()
    observations = []

    for year in (session.year, session.year - 1):

        url = (
            "https://home.treasury.gov/resource-center/data-chart-center/"
            f"interest-rates/daily-treasury-rates.csv/{year}/all"
            f"?type=daily_treasury_yield_curve&field_tdr_date_value={year}"
            "&page&_format=csv"
        )

        try:
            response = requests.get(url, timeout=20)
            response.raise_for_status()
        except Exception as e:
            print(f"Treasury yield curve {year} failed: {e}")
            continue

        for row in csv.DictReader(io.StringIO(response.text)):
            try:
                obs_date = datetime.strptime(
                    row.get("Date", ""), "%m/%d/%Y"
                ).date()
            except ValueError:
                continue

            if obs_date > session:
                continue

            observations.append((obs_date, row))

        # Two observations from the current year are enough.
        if len(observations) >= 2:
            break

    observations.sort(key=lambda x: x[0])

    out = {}

    for column in columns:
        values = []

        for obs_date, row in observations:
            raw = (row.get(column) or "").strip()
            try:
                values.append(
                    {"date": obs_date.isoformat(), "value": float(raw)}
                )
            except ValueError:
                continue

        if len(values) >= 2:
            out[column] = (values[-2], values[-1])

    return out



def get_fred_series(series_id):
    """Pull the two latest valid observations from FRED's public CSV endpoint."""

    url = (
        "https://fred.stlouisfed.org/graph/"
        f"fredgraph.csv?id={series_id}"
    )

    try:
        response = requests.get(
            url,
            timeout=15,
        )

        response.raise_for_status()

        reader = csv.DictReader(
            io.StringIO(
                response.text
            )
        )

        observations = []

        for row in reader:

            raw_value = row.get(
                series_id
            )

            if raw_value in (
                None,
                "",
                ".",
            ):
                continue

            try:
                value = float(
                    raw_value
                )
            except ValueError:
                continue

            observations.append(
                {
                    "date": row.get(
                        "observation_date"
                    ),
                    "value": value,
                }
            )

        if len(observations) < 2:
            return None

        return (
            observations[-2],
            observations[-1],
        )

    except Exception as e:
        print(
            f"FRED {series_id} failed: {e}"
        )
        return None


def gather_macro_data():
    """Gather Treasury rates and major cross-asset markets."""

    data = {
        "rates": [],
        "cross_asset": [],
    }

    # Treasury rates

    treasury_series = {
        # label: (Treasury CSV column, FRED fallback series)
        "2Y Treasury": ("2 Yr", "DGS2"),
        "10Y Treasury": ("10 Yr", "DGS10"),
        "30Y Treasury": ("30 Yr", "DGS30"),
    }

    curve = get_treasury_curve(
        [column for column, _ in treasury_series.values()]
    )

    for label, (column, series_id) in (
        treasury_series.items()
    ):

        result = curve.get(column)
        source = "U.S. Treasury"

        if not result:
            result = get_fred_series(
                series_id
            )
            source = "FRED"

        if not result:
            audit(f"{label}: no yield data from Treasury or FRED.")
            continue

        previous, latest = result

        change_bps = (
            latest["value"]
            - previous["value"]
        ) * 100

        if latest["date"] != session_date().isoformat():
            audit(
                f"{label}: latest yield ({source}) is dated "
                f"{latest['date']}, not {session_date().isoformat()}; the "
                "change shown is for that date."
            )

        data["rates"].append(
            {
                "label": label,
                "value": latest["value"],
                "daily_bps": change_bps,
                "as_of": latest["date"],
                "source": source,
            }
        )

    # Cross-asset markets

    # AUDIT FIX: Brent (BZ=F) replaces WTI (CL=F) as the global benchmark.
    # USD/CAD is added because several holdings are CAD-denominated and the
    # currency explains most of their gap vs USD benchmarks.
    cross_asset_map = {
        "DXY": "DX-Y.NYB",
        "USD/CAD": "CAD=X",
        "Gold": "GC=F",
        "Brent Crude": "BZ=F",
        "VIX": "^VIX",
    }

    for label, ticker in (
        cross_asset_map.items()
    ):

        quote = get_quote(ticker)

        if quote is None:
            audit(f"{label} ({ticker}): no quote data available.")
            continue

        data["cross_asset"].append(
            {
                "label": label,
                "ticker": ticker,
                "value": quote["price"],
                "daily": quote["daily"],
                "ytd": quote["ytd"],
            }
        )

    return data


# Earnings: recent reporters and the market-wide calendar

NASDAQ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def parse_money(value):
    """'$1,234.5' / '($0.12)' → float, or None."""

    if value in (None, "", "N/A", "--"):
        return None

    text = str(value).strip()
    negative = text.startswith("(") or text.startswith("-")
    text = re.sub(r"[^0-9.]", "", text)

    try:
        number = float(text)
    except ValueError:
        return None

    return -number if negative else number


@lru_cache(maxsize=None)
def fetch_earnings_calendar(day):
    """
    Return the market-wide earnings calendar for one date from Nasdaq.

    AUDIT FIX: earnings used to come only from yfinance calendars for the
    tracked tickers. This covers every US-listed reporter, so the most
    market-moving reports appear whether or not they are tracked.
    """

    url = (
        "https://api.nasdaq.com/api/calendar/earnings"
        f"?date={day.isoformat()}"
    )

    try:
        response = requests.get(url, headers=NASDAQ_HEADERS, timeout=20)
        response.raise_for_status()
        rows = (response.json().get("data") or {}).get("rows") or []
    except Exception as e:
        print(f"Earnings calendar failed for {day}: {e}")
        return ()

    timing = {
        "time-pre-market": "Before open",
        "time-after-hours": "After close",
    }

    out = []
    seen_names = set()

    for row in rows:
        symbol = str(row.get("symbol") or "").strip()
        name = str(row.get("name") or "").strip()

        if not symbol:
            continue

        # Collapse share classes (GOOG/GOOGL) into one entry.
        name_key = name.lower()
        if name_key in seen_names:
            continue
        seen_names.add(name_key)

        out.append({
            "ticker": symbol,
            "name": name or symbol,
            "date": day.isoformat(),
            "time": timing.get(row.get("time"), "Time n/a"),
            "market_cap": parse_money(row.get("marketCap")),
            "eps_forecast": parse_money(row.get("epsForecast")),
            "eps_actual": parse_money(row.get("eps")),
            "surprise_pct": parse_money(row.get("surprise")),
        })

    return tuple(out)


def resolve_report_time(ticker, day):
    """
    Fill in report timing when Nasdaq does not supply it, using the
    timestamp on yfinance's earnings dates (16:00 ET = after the close).
    """

    try:
        dates = yf.Ticker(ticker).get_earnings_dates(limit=8)
    except Exception as e:
        print(f"Earnings timing lookup failed for {ticker}: {e}")
        return "Time n/a"

    if dates is None or dates.empty:
        return "Time n/a"

    for ts in dates.index:
        local = ts.tz_convert(MARKET_TZ) if ts.tzinfo else ts

        if local.date() != day:
            continue

        if local.hour >= 16:
            return "After close"

        if (local.hour, local.minute) < (9, 30):
            return "Before open"

    return "Time n/a"


def tracked_universe():
    return set(
        list(getattr(config, "TICKERS", []))
        + list(getattr(config, "PORTFOLIO", []))
    )


def gather_recent_earnings():
    """
    Impactful earnings released since the previous close, with the market's
    reaction:

    - previous session after the close, or today before the open
      → reaction is today's regular-session return;
    - today after the close → reaction is the after-hours move;
    - today, time not supplied → both are shown.
    """

    session = session_date()
    prev = SESSION["prev_date"] or (session - timedelta(days=1))
    tracked = tracked_universe()

    def impactful(entry):
        return (
            entry["ticker"] in tracked
            or (entry["market_cap"] or 0) >= RECENT_EARNINGS_MIN_MARKET_CAP
        )

    candidates = []

    for entry in fetch_earnings_calendar(prev):
        if not impactful(entry):
            continue

        timing = entry["time"]
        if timing == "Time n/a":
            timing = resolve_report_time(entry["ticker"], prev)

        # Only previous-day reports released after the close are new to
        # today's session; earlier ones were already priced yesterday.
        if timing == "After close":
            candidates.append(dict(entry, time=timing, window="prev_after"))

    for entry in fetch_earnings_calendar(session):
        if not impactful(entry):
            continue

        timing = entry["time"]
        if timing == "Time n/a":
            timing = resolve_report_time(entry["ticker"], session)

        candidates.append(dict(entry, time=timing, window="today"))

    candidates.sort(
        key=lambda c: (c["ticker"] not in tracked, -(c["market_cap"] or 0))
    )

    results = []

    for c in candidates[:MAX_RECENT_EARNINGS]:

        quote = get_quote(c["ticker"])
        after_hours = get_after_hours(c["ticker"])

        session_move = quote["daily"] if quote else None
        ah_move = after_hours["pct"] if after_hours else None

        if c["window"] == "prev_after" or c["time"] == "Before open":
            reaction = session_move
            reaction_basis = "today's session"
        elif c["time"] == "After close":
            reaction = ah_move
            reaction_basis = "after hours"
        else:
            reaction = ah_move if ah_move is not None else session_move
            reaction_basis = (
                "after hours" if ah_move is not None else "today's session"
            )

        headlines = search_company_news(
            c["ticker"],
            limit=3,
            name=c["name"],
            topic="earnings results guidance",
        )

        results.append(dict(
            c,
            reported=(
                f"{prev.isoformat()} after close"
                if c["window"] == "prev_after"
                else f"{session.isoformat()} {c['time'].lower()}"
            ),
            session_move=session_move,
            after_hours=ah_move,
            reaction=reaction,
            reaction_basis=reaction_basis,
            headlines=headlines,
        ))

    return results


# Looking ahead: earnings and macro headlines

def normalize_date_value(value):
    if value is None:
        return None

    if isinstance(
        value,
        datetime,
    ):
        return value.date()

    if isinstance(
        value,
        date,
    ):
        return value

    if hasattr(
        value,
        "to_pydatetime",
    ):
        try:
            return (
                value
                .to_pydatetime()
                .date()
            )
        except Exception:
            pass

    if hasattr(
        value,
        "date",
    ) and callable(
        value.date
    ):
        try:
            return value.date()
        except Exception:
            pass

    return None


def extract_earnings_dates(calendar):
    """Handle common yfinance earnings calendar formats."""

    results = []

    if calendar is None:
        return results

    # Dictionary format

    if isinstance(
        calendar,
        dict,
    ):

        raw = calendar.get(
            "Earnings Date"
        )

        if raw is None:
            return results

        if not isinstance(
            raw,
            (list, tuple),
        ):
            raw = [raw]

        for value in raw:

            parsed = (
                normalize_date_value(
                    value
                )
            )

            if parsed:
                results.append(
                    parsed
                )

        return results

    # DataFrame-like format

    try:

        if (
            hasattr(calendar, "index")
            and
            "Earnings Date" in calendar.index
        ):

            row = calendar.loc[
                "Earnings Date"
            ]

            if hasattr(
                row,
                "tolist",
            ):
                values = row.tolist()
            else:
                values = [row]

            for value in values:

                parsed = (
                    normalize_date_value(
                        value
                    )
                )

                if parsed:
                    results.append(
                        parsed
                    )

    except Exception:
        pass

    return results


def gather_lookahead():
    """Gather upcoming earnings and macro-related headlines."""

    lookahead = {
        "earnings": [],
        "macro": [],
    }

    today = session_date()

    horizon = (
        today
        + timedelta(days=EARNINGS_LOOKAHEAD_DAYS)
    )

    # Earnings

    # Market-wide calendar, screened by market cap so that the biggest
    # upcoming reports appear even when they are not tracked.
    tracked = tracked_universe()
    seen = set()

    day = today + timedelta(days=1)

    while day <= horizon:

        if day.weekday() < 5:
            for entry in fetch_earnings_calendar(day):
                if (
                    entry["ticker"] in tracked
                    or (entry["market_cap"] or 0)
                    >= UPCOMING_EARNINGS_MIN_MARKET_CAP
                ):
                    lookahead["earnings"].append(
                        dict(entry, tracked=entry["ticker"] in tracked)
                    )
                    seen.add(entry["ticker"])

        day += timedelta(days=1)

    # Tracked non-US listings (e.g. EL.PA) are not on the Nasdaq calendar.
    for ticker in sorted(tracked - seen):

        try:

            calendar = (
                yf.Ticker(
                    ticker
                ).calendar
            )

            earnings_dates = (
                extract_earnings_dates(
                    calendar
                )
            )

            for earnings_date in (
                earnings_dates
            ):

                if (
                    today
                    < earnings_date
                    <= horizon
                ):

                    lookahead[
                        "earnings"
                    ].append(
                        {
                            "ticker": ticker,
                            "name": company_name(ticker),
                            "date": earnings_date.isoformat(),
                            "time": "Time n/a",
                            "market_cap": None,
                            "eps_forecast": None,
                            "tracked": True,
                        }
                    )

                    break

        except Exception as e:
            print(
                f"Calendar check failed "
                f"for {ticker}: {e}"
            )

    # Tracked names first, then by size; keep the list readable.
    lookahead["earnings"].sort(
        key=lambda e: (not e["tracked"], -(e["market_cap"] or 0))
    )
    lookahead["earnings"] = sorted(
        lookahead["earnings"][:MAX_UPCOMING_EARNINGS],
        key=lambda e: (e["date"], -(e["market_cap"] or 0)),
    )

    # Macro headlines

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

    for source in macro_sources:

        for headline in get_headlines(
            source,
            limit=10,
        ):

            title_lower = (
                headline["title"]
                .lower()
            )

            if not any(
                keyword in title_lower
                for keyword in macro_keywords
            ):
                continue

            title_key = (
                headline["title"]
                .strip()
                .lower()
            )

            if title_key in seen:
                continue

            seen.add(
                title_key
            )

            lookahead[
                "macro"
            ].append(
                headline
            )

            if len(
                lookahead["macro"]
            ) >= 6:
                return lookahead

    return lookahead


# Groq API

def _groq_call(
    prompt,
    max_tokens=6500,
    temperature=0.3,
):
    """Call Groq, log useful diagnostics, and parse its JSON response."""

    groq_url = (
        "https://api.groq.com/openai/v1/"
        "chat/completions"
    )

    api_key = getattr(config, "GROQ_API_KEY", "")
    if not api_key:
        print("Groq API key is missing (GROQ_API_KEY).")
        return None

    headers = {
        "Authorization": f"Bearer {api_key}",
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

    print(
        "Calling Groq: "
        f"model={payload['model']}, "
        f"prompt_length={len(prompt)}, "
        f"max_tokens={max_tokens}"
    )

    try:
        response = requests.post(
            groq_url,
            headers=headers,
            json=payload,
            timeout=120,
        )
    except Exception as e:
        print(f"Groq request exception: {type(e).__name__}: {e}")
        return None

    print(f"Groq HTTP status: {response.status_code}")

    if response.status_code != 200:
        print(f"Groq error body: {response.text[:2000]}")
        return None

    try:
        body = response.json()
    except Exception as e:
        print(
            "Groq returned a non-JSON HTTP response: "
            f"{type(e).__name__}: {e}; body={response.text[:1000]}"
        )
        return None

    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        print(
            "Groq response missing a usable 'choices' list: "
            f"{json.dumps(body)[:2000]}"
        )
        return None

    choice = choices[0] or {}
    message = choice.get("message") or {}
    content = message.get("content")

    print(f"Groq finish_reason: {choice.get('finish_reason')}")
    print(f"Groq usage: {json.dumps(body.get('usage', {}))}")

    # Some API responses can return content as None or an unexpected type.
    if isinstance(content, str):
        raw = content.strip()
    else:
        raw = ""

    print(f"Groq response length: {len(raw)} characters")

    if not raw:
        print(
            "Groq returned empty message content. "
            f"Message metadata: {json.dumps(message)[:1000]}"
        )
        return None

    # Remove Markdown code fences if present.
    if raw.startswith("```"):
        lines = raw.splitlines()

        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]

        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]

        raw = "\n".join(lines).strip()

    # Extract the outer JSON object if the model added surrounding text.
    first_brace = raw.find("{")
    last_brace = raw.rfind("}")

    if first_brace != -1 and last_brace != -1 and last_brace >= first_brace:
        raw = raw[first_brace:last_brace + 1]

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Groq JSON parse failed: {e}")
        print(f"Groq raw content (first 4000 chars): {raw[:4000]}")
        if choice.get("finish_reason") == "length":
            print(
                "Groq output appears to have hit the token limit. "
                "Reduce the requested output length or split the request."
            )
        return None

    if not isinstance(parsed, dict):
        print(
            "Groq JSON parsed successfully, but the top-level value "
            f"was {type(parsed).__name__}, not an object."
        )
        return None

    print(f"Groq JSON parsed successfully; keys={list(parsed.keys())}")
    return parsed


# AI analysis

def fmt_signed(value, suffix="%", digits=2):
    """Prompt/HTML-safe signed number; None → 'n/a'."""

    if value is None:
        return "n/a"

    return f"{value:+.{digits}f}{suffix}"


def fmt_published(published):
    if not published:
        return "time n/a"

    return published.astimezone(MARKET_TZ).strftime("%b %d %I:%M %p ET")


def fmt_market_cap(value):
    if not value:
        return "n/a"

    if value >= 1e12:
        return f"${value / 1e12:.2f}T"

    return f"${value / 1e9:.0f}B"


def build_ai_summary(
    indices,
    sectors,
    factors,
    tickers,
    portfolio,
    macro,
    market_news,
    lookahead,
    recent_earnings,
):

    # Prompt formatting helpers

    def fmt_quote(r, label=None):
        line = (
            f"{label or r['ticker']} "
            f"({r.get('name') or r['ticker']}) | "
            f"daily {fmt_signed(r['daily'])} | "
            f"YTD {fmt_signed(r['ytd'])}"
        )

        if r.get("stale"):
            line += (
                f" | STALE: no bar for {session_date()}, last close "
                f"{r.get('as_of')}; do NOT describe a move for today"
            )

        return line

    def fmt_indices(rows):

        return "\n".join(
            fmt_quote(r) for r in rows
        )

    def fmt_labeled(rows):

        return "\n".join(
            f"{r['label']} "
            f"({r['ticker']}) | "
            f"daily {fmt_signed(r['daily'])} | "
            f"YTD {fmt_signed(r['ytd'])}"
            + (
                f" | vs {FACTOR_BENCHMARK} daily "
                f"{fmt_signed(r['rel_daily'], ' pp')}, YTD "
                f"{fmt_signed(r['rel_ytd'], ' pp')}"
                if r.get("rel_daily") is not None
                else ""
            )
            for r in rows
        )

    def fmt_headline_list(headlines, indent="  "):

        lines = []

        for i, headline in enumerate(
            headlines,
            start=1,
        ):

            lines.append(
                f"{indent}[{i}] "
                f"({fmt_published(headline.get('published'))}) "
                f"{headline['title']} "
                f"— {headline['publisher']}"
            )

            if headline.get("snippet"):
                lines.append(
                    f"{indent}    {headline['snippet'][:400]}"
                )

        return lines

    def fmt_tickers(rows):

        lines = []

        for r in rows:

            line = "\n" + fmt_quote(r)

            if r.get("after_hours"):
                line += (
                    f" | after hours "
                    f"{fmt_signed(r['after_hours']['pct'])} "
                    f"as of {r['after_hours']['time']}"
                )

            lines.append(line)

            if r["headlines"]:

                lines.extend(
                    fmt_headline_list(r["headlines"])
                )

            else:

                lines.append(
                    "  (no company-specific headlines "
                    "published since the previous close)"
                )

        return "\n".join(lines)

    def fmt_portfolio(rows):

        return "\n".join(
            fmt_quote(r) for r in rows
        )

    def fmt_macro(macro_data):

        lines = []

        if macro_data.get(
            "rates"
        ):

            lines.append(
                "TREASURY RATES"
            )

            for r in (
                macro_data["rates"]
            ):

                lines.append(
                    f"{r['label']} | "
                    f"{r['value']:.2f}% | "
                    f"{r['daily_bps']:+.1f} bps | "
                    f"as of {r['as_of']}"
                )

        if macro_data.get(
            "cross_asset"
        ):

            lines.append(
                "\nCROSS-ASSET"
            )

            for r in (
                macro_data[
                    "cross_asset"
                ]
            ):

                lines.append(
                    f"{r['label']} | "
                    f"value "
                    f"{r['value']:.2f} | "
                    f"daily {fmt_signed(r['daily'])} | "
                    f"YTD {fmt_signed(r['ytd'])}"
                )

        if not lines:
            return "(no macro data)"

        return "\n".join(lines)

    def fmt_news(rows):

        if not rows:
            return (
                "(no market news search results)"
            )

        lines = []

        for i, h in enumerate(
            rows,
            start=1,
        ):

            lines.append(
                f"[{i}] {h['title']}"
            )

            lines.append(
                f"    Publisher: "
                f"{h['publisher']} | "
                f"Published: {fmt_published(h.get('published'))}"
            )

            if h.get("snippet"):

                lines.append(
                    f"    Summary: "
                    f"{h['snippet']}"
                )

            if h.get("url"):

                lines.append(
                    f"    URL: "
                    f"{h['url']}"
                )

        return "\n".join(lines)

    def fmt_recent_earnings(rows):

        if not rows:
            return "(no impactful earnings since the previous close)"

        lines = []

        for r in rows:

            lines.append(
                f"\n{r['ticker']} ({r['name']}) | reported {r['reported']} | "
                f"mkt cap {fmt_market_cap(r['market_cap'])} | "
                f"EPS {r['eps_actual'] if r['eps_actual'] is not None else 'n/a'}"
                f" vs est {r['eps_forecast'] if r['eps_forecast'] is not None else 'n/a'}"
                f" (surprise {fmt_signed(r['surprise_pct'])}) | "
                f"session {fmt_signed(r['session_move'])} | "
                f"after hours {fmt_signed(r['after_hours'])}"
            )

            lines.extend(
                fmt_headline_list(r["headlines"])
                or ["  (no earnings coverage found)"]
            )

        return "\n".join(lines)

    def fmt_lookahead(lookahead_data):

        lines = []

        if lookahead_data[
            "earnings"
        ]:

            lines.append(
                "UPCOMING EARNINGS (market-wide, screened by size; "
                "TRACKED = in the user's watchlist/portfolio)"
            )

            for earnings in (
                lookahead_data["earnings"]
            ):

                lines.append(
                    f"{earnings['date']} {earnings['time']}: "
                    f"{earnings['ticker']} "
                    f"({earnings['name']}) | "
                    f"mkt cap {fmt_market_cap(earnings['market_cap'])} | "
                    f"EPS est {earnings['eps_forecast'] if earnings['eps_forecast'] is not None else 'n/a'}"
                    + (" | TRACKED" if earnings.get("tracked") else "")
                )

        if lookahead_data[
            "macro"
        ]:

            lines.append(
                "\nRECENT MACRO HEADLINES"
            )

            for headline in (
                lookahead_data["macro"]
            ):

                lines.append(
                    f"- "
                    f"{headline['title']} "
                    f"— "
                    f"{headline['publisher']}"
                )

        if not lines:
            return "(no lookahead data)"

        return "\n".join(lines)

    portfolio_daily = (
        calculate_weighted_portfolio_return(
            portfolio,
            "daily",
        )
    )

    portfolio_ytd = (
        calculate_weighted_portfolio_return(
            portfolio,
            "ytd",
        )
    )

    if (
        portfolio_daily is not None
        and
        portfolio_ytd is not None
    ):

        portfolio_summary = (
            "Weighted portfolio daily return: "
            f"{portfolio_daily:+.2f}%\n"
            "Weighted portfolio YTD return: "
            f"{portfolio_ytd:+.2f}%"
        )

    else:

        portfolio_summary = (
            "No portfolio weights supplied; "
            "use holdings-level portfolio "
            "movements only."
        )

    # Groq prompt

    prompt = f"""
You are writing a professional Daily Market Review for someone
building experience for a career in wealth management and asset management.

The report should resemble a concise internal investment note:
data first, analysis second, implications third.

SESSION DATE: {session_date().strftime('%A, %B %d, %Y')}
PREVIOUS CLOSE: {previous_close_dt().strftime('%A, %B %d, %Y %I:%M %p ET')}

All returns below are close-to-close for the session date, measured
against the dividend/split-adjusted previous close. YTD is measured from
the prior year's final close. Headlines carry their publish time; only
material published since the previous close has been supplied.

Respond with ONE JSON OBJECT ONLY.
Do not include markdown outside the JSON.

==================================================
MARKET DATA
==================================================

INDICES:
{fmt_indices(indices)}

SECTORS:
{fmt_labeled(sectors)}

FACTORS / STYLE (pp = percentage points vs {FACTOR_BENCHMARK}):
{fmt_labeled(factors)}

TRACKED TICKERS:
{fmt_tickers(tickers)}

PORTFOLIO MOVEMENTS:
{fmt_portfolio(portfolio)}

{portfolio_summary}

==================================================
MACRO / CROSS-ASSET
==================================================

{fmt_macro(macro)}

==================================================
RECENT EARNINGS (since the previous close)
==================================================

{fmt_recent_earnings(recent_earnings)}

==================================================
MARKET HEADLINES
==================================================

{fmt_news(market_news)}

==================================================
LOOKING AHEAD DATA
==================================================

{fmt_lookahead(lookahead)}

==================================================
REQUIRED JSON
==================================================

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

  "earnings_recap": [
    "<one bullet per material report: result vs expectations, guidance,
      and the price reaction supplied above>"
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

==================================================
RULES
==================================================

EXECUTIVE SUMMARY
- Write 4-5 bullets.
- Summarize the most important developments across equities,
  rates, macro, sectors, factors and cross-asset markets.
- Do not simply repeat every number.
- Each bullet should communicate a meaningful takeaway.

TICKER REVIEW
- Every tracked ticker MUST receive a summary.
- Do not omit small movers.
- Focus on company-specific developments, earnings, guidance,
  products, regulation, industry developments, or other
  identifiable catalysts.
- If headlines exist, ground the summary in those headlines.
- Cite relevant headline numbers from the ticker's headline list.
- If Yahoo Finance provides no relevant headline, use any supplied
  broader web-search results for that company.
- When a tracked stock has a material daily move (roughly 1% or more)
  and the available evidence does not explain it, explicitly state
  that the move remains unexplained by the sources reviewed.
- Do not infer a cause from the price movement alone.
- Distinguish confirmed catalysts from plausible but unverified
  explanations.
- Do not fabricate company events, facts or sources.
- Do not spend the entire note repeating the daily percentage move.
- Use ONLY the daily, YTD and after-hours figures supplied above. Never
  compute, round differently or recall a return from memory.
- Describe events from the supplied headlines as TODAY's news only if
  their publish time is on or after the previous close.
- If a row is marked STALE, say its market did not trade / data is not
  yet available, and do not describe a daily move.
- If an after-hours move is supplied and material (roughly 2% or more),
  mention it and its catalyst if one is visible in the headlines.

EARNINGS RECAP
- Cover the most market-relevant reports from RECENT EARNINGS, largest
  or most market-moving first; 3-6 bullets.
- State the EPS result vs estimate, any guidance visible in the
  headlines, and the price reaction (session or after hours as supplied).
- If no coverage explains the reaction, say so. Return an empty list if
  there are no recent earnings.

MARKET STORIES
- Select 3-5 of the most important DISTINCT stories from the supplied
  market-news search results.
- Think of this section as a concise morning/evening market briefing
  similar in spirit to the front page of Bloomberg, Financial Times,
  CNBC, or Reuters.
- Prioritize stories based on their importance to investors and their
  breadth of market impact, not simply on how prominently they appear
  in the search results.
- Favor major developments involving:
  * monetary policy and central banks
  * inflation, employment and economic data
  * government policy and regulation
  * geopolitics and international trade
  * major moves in rates, currencies, commodities or credit
  * major industry developments
  * significant corporate developments with broad market implications
- Do not force every story to relate to the tracked portfolio.
- Do not simply describe today's market performance. The Market
  Dashboard already provides those numbers.
- Each summary should be roughly 180-250 words.
- Explain what happened, the relevant context, why investors care,
  and what could matter next.
- Do not duplicate stories that are essentially about the same event.
- Use only information contained in the supplied search results.
- Do not invent facts, quotes, events, sources or explanations that
  are not reasonably supported by the supplied material.
- source_index MUST correspond to the numbered MARKET HEADLINES list.
- If the supplied search results do not contain enough reliable material
  for three distinct stories, return only the number of stories that
  can be supported. Never manufacture additional stories.
INVESTMENT IMPLICATIONS
- Bull case: strongest arguments supporting current conditions.
- Bear case: strongest risks or challenges.
- Key question: one specific question that should guide monitoring.
- These should be analytical rather than repetitive.

LOOKING AHEAD
- Highlight the most market-moving upcoming earnings from the
  market-wide calendar (mega-caps and sector bellwethers), and call out
  any marked TRACKED.
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
        max_tokens=8000,
        temperature=0.3,
    )

    if parsed is None:

        return {
            "executive_summary": [],
            "tickers": {},
            "stories": [],
            "earnings_recap": [],
            "investment_implications": {
                "bull_case": [],
                "bear_case": [],
                "key_question": "",
            },
            "looking_ahead": "",
            "bottom_line": "",
        }

    # Normalize executive summary

    executive_summary = (
        parsed.get(
            "executive_summary"
        )
        or []
    )

    if not isinstance(
        executive_summary,
        list,
    ):
        executive_summary = []

    executive_summary = [
        str(item).strip()
        for item in executive_summary
        if str(item).strip()
    ][:5]

    # Normalize ticker summaries

    ticker_notes_raw = (
        parsed.get("tickers")
        or {}
    )

    ticker_notes = {}

    if isinstance(
        ticker_notes_raw,
        dict,
    ):

        for ticker, value in (
            ticker_notes_raw.items()
        ):

            if isinstance(
                value,
                dict,
            ):

                note = str(
                    value.get(
                        "note"
                    )
                    or ""
                ).strip()

                cites = (
                    value.get(
                        "cites"
                    )
                    or []
                )

                if not isinstance(
                    cites,
                    list,
                ):
                    cites = []

                cites = [
                    c
                    for c in cites
                    if isinstance(
                        c,
                        int
                    )
                ]

            else:

                note = (
                    str(value).strip()
                    if isinstance(
                        value,
                        str,
                    )
                    else ""
                )

                cites = []

            ticker_notes[
                ticker
            ] = {
                "note": note,
                "cites": cites,
            }

    # Normalize market stories

    stories_raw = (
        parsed.get("stories")
        or []
    )

    stories = []

    if isinstance(
        stories_raw,
        list,
    ):

        for story in stories_raw[:5]:

            if not isinstance(
                story,
                dict,
            ):
                continue

            stories.append(
                {
                    "title": str(
                        story.get(
                            "title"
                        )
                        or ""
                    ).strip(),

                    "summary": str(
                        story.get(
                            "summary"
                        )
                        or ""
                    ).strip(),

                    "source_index":
                        story.get(
                            "source_index"
                        ),
                }
            )

    # Normalize investment implications

    implications_raw = (
        parsed.get(
            "investment_implications"
        )
        or {}
    )

    if not isinstance(
        implications_raw,
        dict,
    ):
        implications_raw = {}

    bull_case = (
        implications_raw.get(
            "bull_case"
        )
        or []
    )

    bear_case = (
        implications_raw.get(
            "bear_case"
        )
        or []
    )

    if not isinstance(
        bull_case,
        list,
    ):
        bull_case = []

    if not isinstance(
        bear_case,
        list,
    ):
        bear_case = []

    investment_implications = {
        "bull_case": [
            str(item).strip()
            for item in bull_case
            if str(item).strip()
        ][:3],

        "bear_case": [
            str(item).strip()
            for item in bear_case
            if str(item).strip()
        ][:3],

        "key_question": str(
            implications_raw.get(
                "key_question"
            )
            or ""
        ).strip(),
    }

    # Normalize earnings recap

    earnings_recap = (
        parsed.get("earnings_recap")
        or []
    )

    if not isinstance(
        earnings_recap,
        list,
    ):
        earnings_recap = []

    earnings_recap = [
        str(item).strip()
        for item in earnings_recap
        if str(item).strip()
    ][:6]

    # Normalize forward view

    looking_ahead = str(
        parsed.get(
            "looking_ahead"
        )
        or ""
    ).strip()

    # Normalize bottom line

    bottom_line = str(
        parsed.get(
            "bottom_line"
        )
        or ""
    ).strip()

    return {
        "executive_summary":
            executive_summary,

        "tickers":
            ticker_notes,

        "stories":
            stories,

        "earnings_recap":
            earnings_recap,

        "investment_implications":
            investment_implications,

        "looking_ahead":
            looking_ahead,

        "bottom_line":
            bottom_line,
    }


# HTML helpers

def html_escape(value):
    return escape(
        str(value),
        quote=True,
    )


def pct_class(value):
    if value is None:
        return "neutral"

    if value > 0:
        return "positive"

    if value < 0:
        return "negative"

    return "neutral"


def format_pct(value, suffix="%"):
    if value is None:
        return "n/a"

    return f"{value:+.2f}{suffix}"


def pct_cell(value, suffix="%"):
    return (
        f"<td class=\"number {pct_class(value)}\">"
        f"{format_pct(value, suffix)}"
        f"</td>"
    )


def build_quote_table(
    rows,
    include_price=False,
    include_after_hours=False,
):
    """Build HTML tables for indices, tickers, and portfolio holdings."""

    if not rows:
        return (
            "<p>Unavailable.</p>"
        )

    html = [
        '<table class="data-table">'
    ]

    html.append(
        "<thead>"
        "<tr>"
        "<th>Ticker</th>"
        "<th>Name</th>"
    )

    if include_price:
        html.append(
            "<th>Price</th>"
        )

    html.append(
        "<th>Daily</th>"
    )

    if include_after_hours:
        html.append(
            "<th>After Hours</th>"
        )

    html.append(
        "<th>YTD</th>"
        "</tr>"
        "</thead>"
    )

    html.append(
        "<tbody>"
    )

    for row in rows:

        name = html_escape(row["name"])

        if row.get("stale"):
            name += (
                f' <span class="flag">(no {session_date():%b %d} '
                f'session data; last close {row.get("as_of")})</span>'
            )

        html.append(
            "<tr>"
        )

        html.append(
            f"<td class=\"ticker\">"
            f"{html_escape(row['ticker'])}"
            f"</td>"
        )

        html.append(
            f"<td class=\"name\">"
            f"{name}"
            f"</td>"
        )

        if include_price:

            html.append(
                f"<td class=\"number\">"
                f"{row['price']:.2f}"
                f"</td>"
            )

        html.append(
            pct_cell(row["daily"])
        )

        if include_after_hours:
            after_hours = row.get("after_hours")
            html.append(
                pct_cell(after_hours["pct"] if after_hours else None)
            )

        html.append(
            pct_cell(row["ytd"])
        )

        html.append(
            "</tr>"
        )

    html.append(
        "</tbody>"
        "</table>"
    )

    return "".join(html)


def build_labeled_table(rows, relative_to=None):
    """Build HTML tables for sectors and factors."""

    if not rows:
        return (
            "<p>Unavailable.</p>"
        )

    html = [
        '<table class="data-table">'
    ]

    html.append(
        "<thead>"
        "<tr>"
        "<th>Category</th>"
        "<th>Ticker</th>"
        "<th>Daily</th>"
        "<th>YTD</th>"
    )

    if relative_to:
        html.append(
            f"<th>Daily vs {html_escape(relative_to)}</th>"
            f"<th>YTD vs {html_escape(relative_to)}</th>"
        )

    html.append(
        "</tr>"
        "</thead>"
        "<tbody>"
    )

    for row in rows:

        html.append(
            "<tr>"
            f"<td class=\"name\">"
            f"{html_escape(row['label'])}"
            f"</td>"
            f"<td class=\"ticker\">"
            f"{html_escape(row['ticker'])}"
            f"</td>"
            + pct_cell(row["daily"])
            + pct_cell(row["ytd"])
        )

        if relative_to:
            html.append(
                pct_cell(row.get("rel_daily"), " pp")
                + pct_cell(row.get("rel_ytd"), " pp")
            )

        html.append(
            "</tr>"
        )

    html.append(
        "</tbody>"
        "</table>"
    )

    return "".join(html)


def sort_by_daily(rows):
    """Sort high-to-low by daily return, with n/a rows last."""

    return sorted(
        rows,
        key=lambda x: (x["daily"] is None, -(x["daily"] or 0)),
    )


def build_macro_table(macro):
    html = ""

    # Treasury rates

    if macro.get(
        "rates"
    ):

        html += (
            "<h2>Treasury Rates</h2>"
        )

        html += """
        <table class="data-table">
            <thead>
                <tr>
                    <th>Rate</th>
                    <th>Yield</th>
                    <th>Daily Change</th>
                    <th>As Of</th>
                </tr>
            </thead>
            <tbody>
        """

        for row in macro[
            "rates"
        ]:

            change = row[
                "daily_bps"
            ]

            html += (
                "<tr>"
                f"<td class=\"name\">"
                f"{html_escape(row['label'])}"
                f"</td>"
                f"<td class=\"number\">"
                f"{row['value']:.2f}%"
                f"</td>"
                f"<td class=\"number "
                f"{pct_class(change)}\">"
                f"{change:+.1f} bps"
                f"</td>"
                f"<td class=\"number\">"
                f"{html_escape(row['as_of'])}"
                f"</td>"
                "</tr>"
            )

        html += (
            "</tbody>"
            "</table>"
        )

    # Cross-asset markets

    if macro.get(
        "cross_asset"
    ):

        html += (
            "<h2>Cross-Asset</h2>"
        )

        html += """
        <table class="data-table">
            <thead>
                <tr>
                    <th>Asset</th>
                    <th>Value</th>
                    <th>Daily</th>
                    <th>YTD</th>
                </tr>
            </thead>
            <tbody>
        """

        for row in macro[
            "cross_asset"
        ]:

            daily = row[
                "daily"
            ]

            ytd = row[
                "ytd"
            ]

            html += (
                "<tr>"
                f"<td class=\"name\">"
                f"{html_escape(row['label'])}"
                f"</td>"
                f"<td class=\"number\">"
                f"{row['value']:.2f}"
                f"</td>"
                + pct_cell(daily)
                + pct_cell(ytd)
                + "</tr>"
            )

        html += (
            "</tbody>"
            "</table>"
        )

    return (
        html
        or "<p>Unavailable.</p>"
    )


def build_bullet_list(items):
    if not items:
        return (
            "<p>Unavailable.</p>"
        )

    html = "<ul>"

    for item in items:

        html += (
            f"<li>"
            f"{html_escape(item)}"
            f"</li>"
        )

    html += "</ul>"

    return html


def build_source_link(
    headline
):
    publisher = html_escape(
        headline.get(
            "publisher",
            "Unknown"
        )
    )

    url = headline.get(
        "url",
        ""
    )

    if url:

        return (
            '<p class="source">'
            'Source: '
            f'<a href="{html_escape(url)}">'
            f'{publisher}'
            '</a>'
            '</p>'
        )

    return (
        '<p class="source">'
        f"Source: {publisher}"
        "</p>"
    )


def build_ticker_review(
    tickers,
    ai,
):
    """Build the detailed company-by-company review."""

    ticker_ai = ai.get(
        "tickers",
        {}
    )

    html = ""

    for row in tickers:

        ticker = html_escape(
            row["ticker"]
        )

        name = html_escape(
            row["name"]
        )

        entry = ticker_ai.get(
            row["ticker"],
            {}
        )

        if not isinstance(
            entry,
            dict,
        ):
            entry = {}

        note = str(
            entry.get(
                "note",
                ""
            )
            or ""
        ).strip()

        cites = (
            entry.get(
                "cites",
                []
            )
            or []
        )

        html += (
            '<div class="ticker-review">'
        )

        html += (
            f"<h2>"
            f"{ticker} — {name}"
            f"</h2>"
        )

        if note:

            html += (
                f"<p>"
                f"{html_escape(note)}"
                f"</p>"
            )

        cited_headlines = []

        if isinstance(
            cites,
            list,
        ):

            for index in cites:

                if (
                    isinstance(
                        index,
                        int,
                    )
                    and
                    1 <= index <= len(
                        row["headlines"]
                    )
                ):

                    cited_headlines.append(
                        row[
                            "headlines"
                        ][index - 1]
                    )

        if cited_headlines:

            html += (
                "<h3>Sources</h3>"
            )

            for headline in (
                cited_headlines
            ):

                html += (
                    build_source_link(
                        headline
                    )
                )

        html += (
            "</div>"
        )

    return (
        html
        or "<p>Unavailable.</p>"
    )

def build_market_stories(
    market_news,
    ai,
):
    stories = ai.get(
        "stories",
        []
    )

    if not stories:
        return (
            "<p>Unavailable.</p>"
        )

    html = ""

    for story in stories:
        title = html_escape(
            str(
                story.get(
                    "title",
                    ""
                )
                or ""
            ).strip()
        )

        summary = str(
            story.get(
                "summary",
                ""
            )
            or ""
        ).strip()

        source_index = story.get(
            "source_index"
        )

        if title:
            html += f"<h2>{title}</h2>"

        if (
            isinstance(source_index, int)
            and 1 <= source_index <= len(market_news)
        ):
            source = market_news[source_index - 1]
            html += build_source_link(source)

        if summary:
            # Preserve paragraph breaks from the model.
            paragraphs = [
                p.strip()
                for p in summary.split("\n\n")
                if p.strip()
            ]

            for paragraph in paragraphs:
                html += (
                    f"<p>"
                    f"{html_escape(paragraph)}"
                    f"</p>"
                )

    return html

def build_paragraph(
    text
):
    if not text:
        return (
            "<p>Unavailable.</p>"
        )

    paragraphs = [
        p.strip()
        for p in text.split(
            "\n\n"
        )
        if p.strip()
    ]

    return "".join(
        f"<p>{html_escape(p)}</p>"
        for p in paragraphs
    )


def build_recent_earnings_table(rows):
    if not rows:
        return "<p>No impactful earnings reported since the previous close.</p>"

    html = [
        '<table class="data-table">'
        "<thead><tr>"
        "<th>Ticker</th><th>Name</th><th>Reported</th>"
        "<th>EPS vs Est.</th><th>Surprise</th>"
        "<th>Session</th><th>After Hours</th>"
        "</tr></thead><tbody>"
    ]

    for r in rows:

        if r["eps_actual"] is not None:
            eps = (
                f"{r['eps_actual']:.2f} vs "
                + (
                    f"{r['eps_forecast']:.2f}"
                    if r["eps_forecast"] is not None
                    else "n/a"
                )
            )
        else:
            eps = "n/a"

        html.append(
            "<tr>"
            f"<td class=\"ticker\">{html_escape(r['ticker'])}</td>"
            f"<td class=\"name\">{html_escape(r['name'])}</td>"
            f"<td>{html_escape(r['reported'])}</td>"
            f"<td class=\"number\">{html_escape(eps)}</td>"
            + pct_cell(r["surprise_pct"])
            + pct_cell(r["session_move"])
            + pct_cell(r["after_hours"])
            + "</tr>"
        )

    html.append("</tbody></table>")

    return "".join(html)


def build_upcoming_earnings_table(rows):
    if not rows:
        return "<p>No major upcoming earnings found.</p>"

    html = [
        '<table class="data-table">'
        "<thead><tr>"
        "<th>Date</th><th>Timing</th><th>Ticker</th><th>Name</th>"
        "<th>Mkt Cap</th><th>EPS Est.</th>"
        "</tr></thead><tbody>"
    ]

    for e in rows:

        name = html_escape(e["name"])

        if e.get("tracked"):
            name = f"<strong>{name}</strong> (tracked)"

        eps = (
            f"{e['eps_forecast']:.2f}"
            if e.get("eps_forecast") is not None
            else "n/a"
        )

        html.append(
            "<tr>"
            f"<td>{html_escape(e['date'])}</td>"
            f"<td>{html_escape(e['time'])}</td>"
            f"<td class=\"ticker\">{html_escape(e['ticker'])}</td>"
            f"<td class=\"name\">{name}</td>"
            f"<td class=\"number\">{fmt_market_cap(e.get('market_cap'))}</td>"
            f"<td class=\"number\">{eps}</td>"
            "</tr>"
        )

    html.append("</tbody></table>")

    return "".join(html)


# Data-quality audits

def run_cross_listing_checks():
    """
    AUDIT: reconcile CAD-listed unhedged ETFs against their USD equivalent.

    expected CAD return = (1 + USD ETF return) * (1 + USD/CAD change) - 1

    Small residuals (fees, withholding tax, FX fixing time) are normal;
    large ones mean one of the price series is wrong or stale.
    """

    results = []

    for cad_ticker, (usd_ticker, fx_ticker) in CROSS_LISTING_CHECKS.items():

        cad = get_quote(cad_ticker)
        usd = get_quote(usd_ticker)
        fx = get_quote(fx_ticker)

        if not (cad and usd and fx):
            continue

        for field, tolerance in (("daily", 0.75), ("ytd", 1.5)):

            if None in (cad[field], usd[field], fx[field]):
                continue

            expected = (
                (1 + usd[field] / 100) * (1 + fx[field] / 100) - 1
            ) * 100
            residual = cad[field] - expected

            result = {
                "ticker": cad_ticker,
                "vs": usd_ticker,
                "field": "Daily" if field == "daily" else "YTD",
                "actual": cad[field],
                "usd": usd[field],
                "fx": fx[field],
                "expected": expected,
                "residual": residual,
                "ok": abs(residual) <= tolerance,
            }

            results.append(result)

            if not result["ok"]:
                audit(
                    f"{cad_ticker} {result['field']} {cad[field]:+.2f}% does "
                    f"not reconcile with {usd_ticker} {usd[field]:+.2f}% and "
                    f"USD/CAD {fx[field]:+.2f}% (expected {expected:+.2f}%, "
                    f"residual {residual:+.2f} pp)."
                )

    return results


def run_portfolio_coverage_checks(portfolio_definitions):
    for portfolio_name, rows, weights in portfolio_definitions:

        if not weights:
            continue

        missing = sorted(
            set(weights) - {r["ticker"] for r in rows}
        )

        if missing:
            audit(
                f"{portfolio_name}: weighted holdings with no data: "
                f"{', '.join(missing)}."
            )

        coverage = portfolio_weight_coverage(rows, "daily", weights)

        if coverage is not None and coverage < 0.995:
            audit(
                f"{portfolio_name}: daily weighted return covers only "
                f"{coverage:.0%} of portfolio weight (stale or missing "
                "holdings excluded and the rest renormalised)."
            )


def run_ai_number_audit(ai, data_values):
    """
    AUDIT: every signed percentage the model writes in the summary, ticker
    notes, earnings recap and bottom line should match a figure supplied in
    the data. Unmatched figures are flagged for manual verification.
    """

    texts = list(ai.get("executive_summary", []))
    texts += list(ai.get("earnings_recap", []))
    texts.append(ai.get("bottom_line", ""))
    texts += [
        v.get("note", "") for v in ai.get("tickers", {}).values()
    ]

    known = [abs(v) for v in data_values if v is not None]

    unmatched = []

    for text in texts:
        for match in re.finditer(r"([+\-−]?\d+(?:\.\d+)?)\s?%", text or ""):
            value = abs(float(match.group(1).replace("−", "-")))

            if value == 0:
                continue

            if not any(abs(value - k) <= 0.06 for k in known):
                unmatched.append(match.group(0))

    for figure in sorted(set(unmatched))[:10]:
        audit(
            f"AI text cites {figure}, which does not match any figure in "
            "the dashboard data; verify before relying on it.",
            level="CHECK",
        )


def build_audit_section(cross_checks):
    html = ""

    if cross_checks:
        html += (
            "<h2>Cross-Listing Reconciliation (CAD vs USD)</h2>"
            '<table class="data-table"><thead><tr>'
            "<th>ETF</th><th>Period</th><th>Actual</th>"
            "<th>USD ETF</th><th>USD/CAD</th><th>Expected</th>"
            "<th>Residual</th></tr></thead><tbody>"
        )

        for c in cross_checks:
            html += (
                "<tr>"
                f"<td class=\"ticker\">{html_escape(c['ticker'])}</td>"
                f"<td>{c['field']}</td>"
                + pct_cell(c["actual"])
                + f"<td class=\"number\">{html_escape(c['vs'])} "
                f"{format_pct(c['usd'])}</td>"
                + pct_cell(c["fx"])
                + pct_cell(c["expected"])
                + f"<td class=\"number\">{format_pct(c['residual'], ' pp')}"
                f"{'' if c['ok'] else ' ⚠'}</td>"
                "</tr>"
            )

        html += "</tbody></table>"

    if AUDIT_LOG:
        html += "<h2>Data Quality Flags</h2><ul>"

        for entry in AUDIT_LOG:
            html += (
                f"<li><strong>{html_escape(entry['level'])}:</strong> "
                f"{html_escape(entry['message'])}</li>"
            )

        html += "</ul>"
    else:
        html += "<p>All data-quality checks passed.</p>"

    return html


# Email composition

def compose_email(
    indices,
    sectors,
    factors,
    tickers,
    portfolio,
    index_portfolio,
    avantis_portfolio,
    macro,
    market_news,
    lookahead,
    ai,
    recent_earnings=(),
    cross_checks=(),
):
    # AUDIT FIX: show the session the data describes, not the run date.
    now = session_date().strftime(
        "%A, %B %d, %Y"
    )

    html = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">

<style>

body {{
    font-family:
        "Times New Roman",
        Times,
        serif;

    font-size: 14px;
    line-height: 1.5;

    color: #000000;

    margin: 0;
    padding: 20px;

    background: #ffffff;
}}

.container {{
    max-width: 1000px;
    margin: 0 auto;
}}

h1 {{
    font-family:
        "Times New Roman",
        Times,
        serif;

    font-size: 25px;

    margin-top: 34px;
    margin-bottom: 12px;

    padding-bottom: 7px;

    border-bottom:
        1px solid #000000;
}}

h2 {{
    font-family:
        "Times New Roman",
        Times,
        serif;

    font-size: 19px;

    margin-top: 23px;
    margin-bottom: 8px;
}}

h3 {{
    font-family:
        "Times New Roman",
        Times,
        serif;

    font-size: 16px;

    margin-top: 16px;
    margin-bottom: 6px;
}}

p {{
    font-family:
        "Times New Roman",
        Times,
        serif;

    margin-top: 0;
    margin-bottom: 12px;
}}

ul {{
    margin-top: 5px;
    margin-bottom: 16px;

    padding-left: 28px;
}}

li {{
    font-family:
        "Times New Roman",
        Times,
        serif;

    margin-bottom: 5px;
}}

.date {{
    font-size: 15px;
    margin-bottom: 27px;
}}

.data-table {{
    width: 100%;

    border-collapse:
        collapse;

    margin-top: 8px;
    margin-bottom: 23px;

    table-layout: auto;
}}

.data-table th {{
    text-align: left;

    font-weight: bold;

    border-bottom:
        2px solid #000000;

    padding:
        7px 10px;

    white-space: nowrap;

    font-family:
        "Times New Roman",
        Times,
        serif;
}}

.data-table td {{
    padding:
        6px 10px;

    border-bottom:
        1px solid #dddddd;

    vertical-align: top;

    font-family:
        "Times New Roman",
        Times,
        serif;
}}

.data-table .ticker {{
    white-space: nowrap;
    font-weight: bold;
}}

.data-table .name {{
    min-width: 260px;

    white-space: normal;

    overflow-wrap: break-word;
}}

.data-table .number {{
    text-align: right;

    white-space: nowrap;

    font-variant-numeric:
        tabular-nums;
}}

.positive {{
    color: #008000;
}}

.negative {{
    color: #b00000;
}}

.neutral {{
    color: #000000;
}}

.ticker-review {{
    margin-bottom: 26px;
}}

.flag {{
    font-size: 12px;
    color: #8a5a00;
}}

.source {{
    font-size: 12px;

    margin-top: -4px;
    margin-bottom: 8px;
}}

.source a {{
    color: #000000;

    text-decoration:
        underline;
}}

.footer {{
    margin-top: 38px;

    padding-top: 10px;

    border-top:
        1px solid #999999;

    font-size: 12px;

    color: #555555;
}}

</style>
</head>

<body>
<div class="container">

<h1>Daily Market Review</h1>

<p class="date">
{html_escape(now)}
</p>


<!-- 1. Executive Summary -->

<h1>1. Executive Summary</h1>

{build_bullet_list(
    ai.get(
        "executive_summary",
        []
    )
)}


<!-- 2. Market Dashboard -->

<h1>2. Market Dashboard</h1>


<h2>Major Indices</h2>

{build_quote_table(
    indices,
    include_price=False
)}


<h2>Portfolio Movements</h2>

"""

    # Show the three model portfolios in the requested order.
    portfolio_definitions = [
        ("Index Portfolio", index_portfolio, INDEX_PORTFOLIO_WEIGHTS),
        ("Avantis Portfolio", avantis_portfolio, AVANTIS_PORTFOLIO_WEIGHTS),
        ("Current Portfolio", portfolio, PORTFOLIO_WEIGHTS),
    ]

    for portfolio_name, portfolio_rows, portfolio_weights in portfolio_definitions:
        daily_return = calculate_weighted_portfolio_return(
            portfolio_rows, "daily", portfolio_weights
        )
        ytd_return = calculate_weighted_portfolio_return(
            portfolio_rows, "ytd", portfolio_weights
        )

        html += f"<h3>{html_escape(portfolio_name)}</h3>"

        if daily_return is not None and ytd_return is not None:
            html += f"""
<p>
<strong>Weighted Return:</strong>
<span class="{pct_class(daily_return)}">{format_pct(daily_return)}</span>
today
&nbsp;&nbsp;|&nbsp;&nbsp;
YTD
<span class="{pct_class(ytd_return)}">{format_pct(ytd_return)}</span>
</p>
"""
        else:
            html += "<p>Weighted return unavailable; check portfolio weights and holdings data.</p>"

        coverage = portfolio_weight_coverage(
            portfolio_rows, "daily", portfolio_weights
        )

        if coverage is not None and coverage < 0.995:
            html += (
                f'<p class="flag">Daily weighted return covers '
                f"{coverage:.0%} of portfolio weight; see Data Quality.</p>"
            )

        html += build_quote_table(portfolio_rows, include_price=True)

    html += f"""

<h2>Sector Performance</h2>

{build_labeled_table(
    sort_by_daily(sectors)
)}


<h2>Factor / Style Performance</h2>

{build_labeled_table(
    sort_by_daily(factors),
    relative_to=FACTOR_BENCHMARK,
)}


<h2>Tracked Tickers</h2>

{build_quote_table(
    tickers,
    include_price=True,
    include_after_hours=True,
)}

<h2>Company-by-Company Review</h2>

{build_ticker_review(
    tickers,
    ai
)}


<!-- 3. Rates, Macro & Cross-Asset -->

<h1>3. Rates, Macro &amp; Cross-Asset</h1>

{build_macro_table(
    macro
)}



<!-- 4. Earnings & After-Hours -->

<h1>4. Earnings &amp; After-Hours</h1>

<h2>Reported Since the Previous Close</h2>

{build_recent_earnings_table(
    recent_earnings
)}

{build_bullet_list(
    ai.get(
        "earnings_recap",
        []
    )
) if ai.get("earnings_recap") else ""}


<!-- 5. Top Market Stories -->

<h1>5. Top Market Stories</h1>

{build_market_stories(
    market_news,
    ai
)}


<!-- 6. Investment Implications -->

<h1>6. Investment Implications</h1>


<h2>Bull Case</h2>

{build_bullet_list(
    ai.get(
        "investment_implications",
        {}
    ).get(
        "bull_case",
        []
    )
)}


<h2>Bear Case</h2>

{build_bullet_list(
    ai.get(
        "investment_implications",
        {}
    ).get(
        "bear_case",
        []
    )
)}


<h2>Key Question</h2>

{build_paragraph(
    ai.get(
        "investment_implications",
        {}
    ).get(
        "key_question",
        ""
    )
)}


<!-- 7. Looking Ahead -->

<h1>7. Looking Ahead</h1>


<h2>Upcoming Earnings</h2>
"""

    html += build_upcoming_earnings_table(
        lookahead["earnings"]
    )

    if lookahead[
        "macro"
    ]:

        html += (
            "<h2>"
            "Macro Developments to Monitor"
            "</h2>"
        )

        html += "<ul>"

        for headline in (
            lookahead["macro"]
        ):

            html += (
                "<li>"
                f"{html_escape(headline['title'])}"
                " — "
                f"{html_escape(headline['publisher'])}"
                "</li>"
            )

        html += "</ul>"

    html += f"""

<h2>Forward View</h2>

{build_paragraph(
    ai.get(
        "looking_ahead",
        ""
    )
)}


<!-- 8. Bottom Line -->

<h1>8. Bottom Line</h1>

{build_paragraph(
    ai.get(
        "bottom_line",
        ""
    )
)}


<!-- 9. Data Quality -->

<h1>9. Data Quality</h1>

{build_audit_section(
    cross_checks
)}


<div class="footer">
Generated automatically via
Yahoo Finance, U.S. Treasury, Nasdaq, Tavily, FRED &amp; Groq.
Returns are total returns in each security's local currency, measured
from the adjusted previous close; YTD from the prior year's final close.
</div>

</div>
</body>
</html>
"""

    return html


# Email sending

def send_email(
    subject,
    html_body,
    plain_text_body=None,
):
    """Send the report as a multipart HTML/plain-text email."""

    msg = MIMEMultipart(
        "alternative"
    )

    msg["Subject"] = subject

    msg["From"] = (
        config.EMAIL_SENDER
    )

    msg["To"] = ", ".join(
        config.EMAIL_RECIPIENT
    )

    if not plain_text_body:

        plain_text_body = (
            "Please view this email "
            "in an HTML-capable "
            "email client."
        )

    msg.attach(
        MIMEText(
            plain_text_body,
            "plain",
            "utf-8",
        )
    )

    msg.attach(
        MIMEText(
            html_body,
            "html",
            "utf-8",
        )
    )

    with smtplib.SMTP_SSL(
        "smtp.gmail.com",
        465,
    ) as server:

        server.login(
            config.EMAIL_SENDER,
            config.EMAIL_PASSWORD,
        )

        server.sendmail(
            config.EMAIL_SENDER,
            config.EMAIL_RECIPIENT,
            msg.as_string(),
        )


# Main workflow

def main():

    # Confirm fresh market data (also fixes today's and the previous
    # session dates used by every return calculation).

    if not has_fresh_data(
        "^GSPC"
    ):

        print(
            "No fresh market data "
            "for today — exiting."
        )

        return

    # Prevent duplicate daily runs.

    if already_ran_today():

        print(
            "Already ran today — exiting."
        )

        return

    # Gather market data.

    print(
        "Gathering indices..."
    )

    indices = gather_group(
        config.INDICES,
        with_headlines=False,
    )

    print(
        "Gathering tracked tickers..."
    )

    tickers = gather_group(
        config.TICKERS,
        with_headlines=True,
        with_after_hours=True,
    )

    print(
        "Gathering portfolio..."
    )

    portfolio = gather_group(
        config.PORTFOLIO,
        with_headlines=False,
    )

    print("Gathering index portfolio...")
    index_portfolio = gather_group(
        INDEX_PORTFOLIO,
        with_headlines=False,
    )

    print("Gathering Avantis portfolio...")
    avantis_portfolio = gather_group(
        AVANTIS_PORTFOLIO,
        with_headlines=False,
    )

    print(
        "Gathering sectors..."
    )

    sectors = gather_labeled_etfs(
        SECTOR_ETFS
    )

    print(
        "Gathering factors..."
    )

    factors = gather_labeled_etfs(
        FACTOR_ETFS,
        benchmark=FACTOR_BENCHMARK,
    )

    print(
        "Gathering macro data..."
    )

    macro = gather_macro_data()

    print(
        "Gathering market headlines..."
    )

    market_news = (
        gather_market_news(
            limit=10
        )
    )

    print(
        "Gathering recent earnings..."
    )

    recent_earnings = gather_recent_earnings()

    print(
        "Gathering lookahead..."
    )

    lookahead = (
        gather_lookahead()
    )

    # Stop if no usable market data was gathered.

    if (
        not indices
        and not tickers
        and not portfolio
        and not index_portfolio
        and not avantis_portfolio
    ):

        print(
            "No data gathered — "
            "exiting without email."
        )

        return

    # Data-quality audits on the gathered data.

    print(
        "Running data-quality audits..."
    )

    cross_checks = run_cross_listing_checks()

    run_portfolio_coverage_checks([
        ("Index Portfolio", index_portfolio, INDEX_PORTFOLIO_WEIGHTS),
        ("Avantis Portfolio", avantis_portfolio, AVANTIS_PORTFOLIO_WEIGHTS),
        ("Current Portfolio", portfolio, PORTFOLIO_WEIGHTS),
    ])

    # Generate AI analysis.

    print(
        "Building AI analysis..."
    )

    ai = build_ai_summary(
        indices=indices,
        sectors=sectors,
        factors=factors,
        tickers=tickers,
        portfolio=portfolio,
        macro=macro,
        market_news=market_news,
        lookahead=lookahead,
        recent_earnings=recent_earnings,
    )

    # Check that the model's figures exist in the supplied data.

    data_values = []

    for row in (
        indices + tickers + portfolio + index_portfolio
        + avantis_portfolio + sectors + factors + macro["cross_asset"]
    ):
        data_values += [row.get("daily"), row.get("ytd")]
        data_values += [row.get("rel_daily"), row.get("rel_ytd")]
        if row.get("after_hours"):
            data_values.append(row["after_hours"]["pct"])

    for row in recent_earnings:
        data_values += [
            row["session_move"], row["after_hours"], row["surprise_pct"]
        ]

    for row in macro["rates"]:
        data_values.append(row["value"])

    for _, rows, weights in (
        ("", index_portfolio, INDEX_PORTFOLIO_WEIGHTS),
        ("", avantis_portfolio, AVANTIS_PORTFOLIO_WEIGHTS),
        ("", portfolio, PORTFOLIO_WEIGHTS),
    ):
        for field in ("daily", "ytd"):
            data_values.append(
                calculate_weighted_portfolio_return(rows, field, weights)
            )

    run_ai_number_audit(ai, data_values)

    # Build and send the email.

    today_str = (
        session_date()
        .strftime(
            "%a, %b %d %Y"
        )
    )

    subject = (
        "Daily Market Review — "
        f"{today_str}"
    )

    html_body = compose_email(
        indices=indices,
        sectors=sectors,
        factors=factors,
        tickers=tickers,
        portfolio=portfolio,
        index_portfolio=index_portfolio,
        avantis_portfolio=avantis_portfolio,
        macro=macro,
        market_news=market_news,
        lookahead=lookahead,
        ai=ai,
        recent_earnings=recent_earnings,
        cross_checks=cross_checks,
    )

    send_email(
        subject=subject,
        html_body=html_body,
    )

    print(
        "Daily Market Review "
        "email sent."
    )

    # Record successful completion.

    mark_run_success()


if __name__ == "__main__":
    main()
