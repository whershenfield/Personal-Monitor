import smtplib
import json
import os
import csv
import io
import re
import time
from functools import lru_cache
from datetime import datetime, date, timedelta, timezone
from email.utils import parsedate_to_datetime

import yfinance as yf
import pytz
import requests

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from urllib.parse import urlparse

import config


REPORT_TITLE = "Daily Market Review"


# ---------------------------------------------------------------------------
# Configuration and environment overrides
# ---------------------------------------------------------------------------

config.EMAIL_SENDER = os.environ.get("EMAIL_SENDER", config.EMAIL_SENDER)
config.EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", config.EMAIL_PASSWORD)
config.EMAIL_RECIPIENT = os.environ.get(
    "EMAIL_RECIPIENT", ",".join(config.EMAIL_RECIPIENT)
).split(",")
config.GROQ_API_KEY = os.environ.get("GROQ_API_KEY", config.GROQ_API_KEY)
config.GIST_ID = os.environ.get("GIST_ID", config.GIST_ID)
config.GIST_TOKEN = os.environ.get("GIST_TOKEN_GITHUB", config.GIST_TOKEN)
config.TAVILY_API_KEY = os.environ.get(
    "TAVILY_API_KEY", getattr(config, "TAVILY_API_KEY", "")
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
DEFAULT_FACTOR_ETFS = {
    "Growth": "VUG",
    "Momentum": "MTUM",
    "Quality": "QUAL",
    "Value": "VTV",
    "Minimum Volatility": "USMV",
    "Small Cap": "IWM",
    "Equal Weight": "RSP",
}

SECTOR_ETFS = getattr(config, "SECTOR_ETFS", DEFAULT_SECTOR_ETFS)
FACTOR_ETFS = getattr(config, "FACTOR_ETFS", DEFAULT_FACTOR_ETFS)

# Optional custom security names and news keywords.
TICKER_NAMES = getattr(config, "TICKER_NAMES", {})
TICKER_KEYWORDS = getattr(config, "TICKER_KEYWORDS", {})

# Portfolio definitions and weights.
PORTFOLIO_WEIGHTS = getattr(config, "PORTFOLIO_WEIGHTS", {})
INDEX_PORTFOLIO = getattr(config, "INDEX_PORTFOLIO", [])
INDEX_PORTFOLIO_WEIGHTS = getattr(config, "INDEX_PORTFOLIO_WEIGHTS", {})
AVANTIS_PORTFOLIO = getattr(config, "AVANTIS_PORTFOLIO", [])
AVANTIS_PORTFOLIO_WEIGHTS = getattr(config, "AVANTIS_PORTFOLIO_WEIGHTS", {})

# Earnings settings (overridable in config.py).
RECENT_EARNINGS_LIMIT = getattr(config, "RECENT_EARNINGS_LIMIT", 6)
RECENT_EARNINGS_MIN_CAP = getattr(config, "RECENT_EARNINGS_MIN_CAP", 10e9)
UPCOMING_EARNINGS_DAYS = getattr(config, "UPCOMING_EARNINGS_DAYS", 2)
UPCOMING_EARNINGS_LIMIT = getattr(config, "UPCOMING_EARNINGS_LIMIT", 8)
UPCOMING_EARNINGS_MIN_CAP = getattr(config, "UPCOMING_EARNINGS_MIN_CAP", 20e9)


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def market_today():
    """Return the current calendar date in New York time."""
    return datetime.now(MARKET_TZ).date()


def next_day(d):
    return d + timedelta(days=1)


def previous_business_day(d):
    d = d - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def parse_published(value):
    """Parse RFC-2822 or ISO timestamps into an aware UTC datetime."""
    if not value:
        return None
    s = str(value).strip()
    dt = None
    try:
        dt = parsedate_to_datetime(s)
    except Exception:
        dt = None
    if dt is None:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def hours_old(dt):
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600


def fmt_published(dt):
    if not dt:
        return "time unknown"
    return dt.astimezone(MARKET_TZ).strftime("%b %d, %I:%M %p ET")


# ---------------------------------------------------------------------------
# Fresh-data and duplicate-run checks
# ---------------------------------------------------------------------------

def has_fresh_data(ticker):
    """Check whether Yahoo Finance has a daily observation for today."""
    try:
        hist = yf.Ticker(ticker).history(period="5d")
        if hist.empty:
            return False
        return hist.index[-1].date() == market_today()
    except Exception as e:
        print(f"Fresh-data check failed for {ticker}: {e}")
        return False


def _gist_headers():
    return {
        "Authorization": f"token {config.GIST_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }


def get_run_state():
    try:
        r = requests.get(
            f"https://api.github.com/gists/{config.GIST_ID}",
            headers=_gist_headers(),
            timeout=15,
        )
    except Exception as e:
        print(f"Failed to fetch Gist: {e}")
        return {}

    if r.status_code != 200:
        print(f"Failed to fetch Gist: {r.status_code}")
        return {}

    content = (
        r.json().get("files", {}).get("run_state.json", {}).get("content", "{}")
    )
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return {}


def already_ran_today():
    return get_run_state().get("last_run_date") == market_today().isoformat()


def mark_run_success():
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
            headers=_gist_headers(),
            data=json.dumps(payload),
            timeout=15,
        )
    except Exception as e:
        print(f"Failed to save run state: {e}")
        return
    if r.status_code != 200:
        print(f"Failed to save run state: {r.status_code}")


# ---------------------------------------------------------------------------
# Quotes and performance
# ---------------------------------------------------------------------------

_QUOTE_CACHE = {}


def _today_intraday_close(stock, today):
    """Last intraday price for today, used when the daily bar is late."""
    try:
        intra = stock.history(
            period="5d", interval="15m", auto_adjust=False, prepost=False
        )
    except Exception:
        return None
    if intra.empty:
        return None
    intra = intra.dropna(subset=["Close"])
    todays = intra[[ts.date() == today for ts in intra.index]]
    if todays.empty:
        return None
    return float(todays["Close"].iloc[-1])


def _fetch_quote(ticker):
    today = market_today()
    try:
        stock = yf.Ticker(ticker)
        # Start in mid-December so we capture last year's final close,
        # which is the correct base for a YTD return.
        hist = stock.history(
            start=date(today.year - 1, 12, 15).isoformat(),
            end=next_day(today).isoformat(),
            auto_adjust=False,
        )
    except Exception as e:
        print(f"Error fetching {ticker}: {e}")
        return None

    if hist.empty or "Close" not in hist:
        return None

    closes = hist["Close"].dropna()
    series = [
        (ts.date(), float(c)) for ts, c in zip(closes.index, closes.tolist())
    ]
    if not series:
        return None

    prior_year = [s for s in series if s[0].year < today.year]
    this_year = [s for s in series if s[0].year == today.year]
    if not this_year:
        return None
    ytd_base = prior_year[-1][1] if prior_year else this_year[0][1]

    # Yahoo's daily bar for today is sometimes late for individual
    # securities even after the close. If it's missing, fall back to the
    # last intraday price from today's session.
    if series[-1][0] < today:
        intraday = _today_intraday_close(stock, today)
        if intraday is not None:
            series.append((today, intraday))
            print(f"{ticker}: daily bar missing, used intraday close")

    if len(series) < 2:
        return None

    as_of, current = series[-1]
    prev_close = series[-2][1]
    if prev_close == 0 or ytd_base == 0:
        return None

    return {
        "price": current,
        "daily": (current - prev_close) / prev_close * 100,
        "ytd": (current - ytd_base) / ytd_base * 100,
        "as_of": as_of,
        "stale": as_of < today,
    }


def get_quote(ticker):
    if ticker not in _QUOTE_CACHE:
        _QUOTE_CACHE[ticker] = _fetch_quote(ticker)
        q = _QUOTE_CACHE[ticker]
        if q and q["stale"]:
            print(f"WARNING: {ticker} latest data is from {q['as_of']}")
    return _QUOTE_CACHE[ticker]


# ---------------------------------------------------------------------------
# Names and news relevance
# ---------------------------------------------------------------------------

DEFAULT_TICKER_NAMES = {
    "^GSPC": "S&P 500 Index",
    "^IXIC": "Nasdaq Composite Index",
    "^RUT": "Russell 2000 Index",
    "^DJI": "Dow Jones Industrial Average",
    "EFA": "iShares MSCI EAFE ETF",
    "^GSPTSE": "S&P/TSX Composite Index",
    "EL.PA": "EssilorLuxottica",
    "SPGI": "S&P Global",
    "CAUS.TO": "Avantis CIBC U.S. Equity ETF",
    "CALV.TO": "Avantis CIBC U.S. Large Cap Value ETF",
    "VCN.TO": "Vanguard FTSE Canada All Cap Index ETF",
    "CADE.TO": "Avantis CIBC International Equity ETF",
    "ZSP.TO": "BMO S&P 500 Index ETF",
    "ZEA.TO": "BMO MSCI EAFE Index ETF",
    "CACE.TO": "Avantis CIBC Canadian Equity ETF",
}

# Words that must appear in a headline or snippet for it to count as news
# about the company. Add more in config.TICKER_KEYWORDS.
DEFAULT_TICKER_KEYWORDS = {
    "EL.PA": ["essilorluxottica", "essilor", "luxottica"],
    "SPGI": ["s&p global"],
}

# Names discovered at runtime (e.g., from the earnings calendar).
_EXTRA_NAMES = {}

_CORPORATE_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd",
    "limited", "plc", "sa", "se", "nv", "ag", "holdings", "holding",
    "group", "class", "a", "b", "c", "common", "stock", "shares", "&",
}

_GENERIC_WORDS = {
    "american", "united", "general", "national", "first", "global",
    "international", "advanced", "the", "new", "bank", "energy",
}


@lru_cache(maxsize=None)
def company_name(ticker):
    """Return a readable display name for a stock, index, or ETF."""
    if ticker in TICKER_NAMES:
        return TICKER_NAMES[ticker]
    if ticker in DEFAULT_TICKER_NAMES:
        return DEFAULT_TICKER_NAMES[ticker]
    if ticker in _EXTRA_NAMES:
        return _EXTRA_NAMES[ticker]
    try:
        info = yf.Ticker(ticker).info or {}
        name = info.get("longName") or info.get("shortName")
        if name:
            return str(name).strip()
    except Exception:
        pass
    return ticker


@lru_cache(maxsize=None)
def company_keywords(ticker):
    custom = TICKER_KEYWORDS.get(ticker) or DEFAULT_TICKER_KEYWORDS.get(ticker)
    if custom:
        return tuple(k.lower() for k in custom)

    words = [
        w.strip(".,()") for w in company_name(ticker).lower().split()
    ]
    words = [w for w in words if w]
    while words and words[-1] in _CORPORATE_SUFFIXES:
        words.pop()
    if words and words[0] == "the":
        words = words[1:]

    keywords = []
    core = " ".join(words).strip()
    if core and core != ticker.lower():
        keywords.append(core)
    if words and len(words[0]) >= 5 and words[0] not in _GENERIC_WORDS:
        keywords.append(words[0])
    return tuple(dict.fromkeys(keywords))


def mentions_company(ticker, text):
    """True if the text is plausibly about this specific company."""
    if not text:
        return False
    low = text.lower()
    if any(k in low for k in company_keywords(ticker)):
        return True
    # Plain US tickers can also match as a standalone symbol (e.g. "AAPL").
    # Exchange-suffixed tickers like EL.PA are never matched this way,
    # which prevents "EL" from pulling in unrelated companies.
    if "." not in ticker and "^" not in ticker and len(ticker) >= 2:
        pattern = rf"(?<![A-Za-z]){re.escape(ticker)}(?![A-Za-z])"
        if re.search(pattern, text):
            return True
    return False


def news_publisher(result):
    """Return the publisher name, falling back to the article URL's domain."""
    source = str(result.get("source") or "").strip()
    if source:
        return source
    hostname = (urlparse(str(result.get("url") or "")).hostname or "").lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return hostname or "Unknown source"


def dedupe_headlines(items):
    out, seen_titles, seen_urls = [], set(), set()
    for h in items:
        key = h.get("title", "").strip().lower()
        url = h.get("url") or ""
        if not key or key in seen_titles or (url and url in seen_urls):
            continue
        seen_titles.add(key)
        if url:
            seen_urls.add(url)
        out.append(h)
    return out


def sort_by_recency(items):
    floor = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(items, key=lambda h: h.get("published") or floor, reverse=True)


def is_recent(headline, max_hours):
    published = headline.get("published")
    if published is None:
        return True
    return hours_old(published) <= max_hours


# ---------------------------------------------------------------------------
# News sources
# ---------------------------------------------------------------------------

def get_headlines(ticker, limit=5):
    """Return Yahoo Finance headlines for a ticker (unfiltered)."""
    try:
        news = yf.Ticker(ticker).news or []
    except Exception as e:
        print(f"Yahoo headlines failed for {ticker}: {e}")
        return []

    out = []
    for article in news:
        content = article.get("content", {}) or {}
        title = (content.get("title") or "").strip()
        if not title:
            continue
        publisher = (
            (content.get("provider") or {}).get("displayName")
            or article.get("publisher")
            or "Unknown"
        )
        url = (
            (content.get("canonicalUrl") or {}).get("url")
            or (content.get("clickThroughUrl") or {}).get("url")
            or article.get("link")
            or ""
        )
        out.append({
            "title": title,
            "publisher": publisher,
            "url": url,
            "snippet": str(content.get("summary") or "").strip(),
            "published": parse_published(
                content.get("pubDate") or content.get("displayTime")
            ),
        })

    return dedupe_headlines(out)[:limit]


def tavily_search(query, days=1, max_results=10, depth="advanced",
                  include_domains=None, exclude_domains=None):
    """Run a Tavily news search and return normalized results."""
    api_key = getattr(config, "TAVILY_API_KEY", "")
    if not api_key:
        return []

    body = {
        "api_key": api_key,
        "query": query,
        "topic": "news",
        "search_depth": depth,
        "days": days,
        "max_results": max_results,
        "include_answer": False,
    }
    if include_domains:
        body["include_domains"] = list(include_domains)
    if exclude_domains:
        body["exclude_domains"] = list(exclude_domains)

    try:
        response = requests.post(
            "https://api.tavily.com/search",
            headers={"Content-Type": "application/json"},
            json=body,
            timeout=30,
        )
    except Exception as e:
        print(f"Tavily exception for '{query}': {e}")
        return []

    if response.status_code != 200:
        print(
            f"Tavily search failed: {response.status_code} "
            f"{response.text[:300]}"
        )
        return []

    out = []
    for result in response.json().get("results", []) or []:
        title = str(result.get("title") or "").strip()
        url = str(result.get("url") or "").strip()
        if not title or not url:
            continue
        out.append({
            "title": title,
            "publisher": news_publisher(result),
            "url": url,
            "snippet": str(result.get("content") or "").strip(),
            "published": parse_published(result.get("published_date")),
        })
    return out


def search_company_news(ticker, limit=5):
    """Search the web for company-specific news from the last week."""
    name = company_name(ticker)
    if "." in ticker or "^" in ticker:
        # Don't put exchange-suffixed tickers in the query; search engines
        # read "EL.PA" as "El Paso" and similar.
        query = f'"{name}" shares company news'
    else:
        query = f'"{name}" ({ticker}) stock news'
    return tavily_search(query, days=7, max_results=limit + 3)


def gather_company_headlines(ticker, limit=5):
    """
    Headlines from the last week that are verifiably about this company,
    newest first. Older items give the AI background when there's no
    fresh news; each one carries its publication time.
    """
    candidates = get_headlines(ticker, limit=20) + search_company_news(
        ticker, limit=limit
    )
    relevant = [
        h for h in candidates
        if is_recent(h, 7 * 24)
        and mentions_company(ticker, f"{h['title']} {h.get('snippet', '')}")
    ]
    dropped = len(candidates) - len(relevant)
    if dropped:
        print(f"{ticker}: dropped {dropped} stale or off-topic headlines")
    return sort_by_recency(dedupe_headlines(relevant))[:limit]


def gather_group(tickers, with_headlines=False, headline_limit=5):
    rows = []
    for ticker in tickers:
        quote = get_quote(ticker)
        if quote is None:
            print(f"Skipping {ticker}: no quote data available")
            continue
        row = {
            "ticker": ticker,
            "name": company_name(ticker),
            **quote,
            "headlines": [],
        }
        if with_headlines:
            row["headlines"] = gather_company_headlines(
                ticker, limit=headline_limit
            )
            row["benchmarks"] = gather_benchmarks(ticker)
        rows.append(row)
    return rows


# Yahoo's sector names mapped to the sector ETF labels above.
_YF_SECTOR_TO_LABEL = {
    "Technology": "Information Technology",
    "Communication Services": "Communication Services",
    "Consumer Cyclical": "Consumer Discretionary",
    "Industrials": "Industrials",
    "Financial Services": "Financials",
    "Healthcare": "Health Care",
    "Consumer Defensive": "Consumer Staples",
    "Energy": "Energy",
    "Utilities": "Utilities",
    "Real Estate": "Real Estate",
    "Basic Materials": "Materials",
}

# Home-market index for exchange-suffixed tickers.
_SUFFIX_INDEX = {
    "PA": ("CAC 40", "^FCHI"),
    "TO": ("S&P/TSX Composite", "^GSPTSE"),
    "L": ("FTSE 100", "^FTSE"),
    "DE": ("DAX", "^GDAXI"),
    "AS": ("AEX", "^AEX"),
    "SW": ("SMI", "^SSMI"),
}

# Optional overrides in config.py, e.g.
# TICKER_BENCHMARKS = {"EL.PA": {"CAC 40": "^FCHI", "Euro Stoxx 50": "^STOXX50E"}}
TICKER_BENCHMARKS = getattr(config, "TICKER_BENCHMARKS", {})


def ticker_benchmarks(ticker):
    """The market (and, for U.S. stocks, sector) to compare a stock against."""
    if ticker in TICKER_BENCHMARKS:
        return list(TICKER_BENCHMARKS[ticker].items())
    if "." in ticker:
        suffix = ticker.rsplit(".", 1)[1].upper()
        return [_SUFFIX_INDEX[suffix]] if suffix in _SUFFIX_INDEX else []
    out = [("S&P 500", "^GSPC")]
    try:
        sector = (yf.Ticker(ticker).info or {}).get("sector")
    except Exception:
        sector = None
    label = _YF_SECTOR_TO_LABEL.get(sector)
    if label and label in SECTOR_ETFS:
        out.append((f"{label} sector", SECTOR_ETFS[label]))
    return out


def gather_benchmarks(ticker):
    out = []
    for label, symbol in ticker_benchmarks(ticker):
        quote = get_quote(symbol)
        if quote is None:
            continue
        out.append({"label": label, "ticker": symbol,
                    "daily": quote["daily"], "stale": quote["stale"],
                    "as_of": quote["as_of"]})
    return out


# Headlines about a company's results are covered in the Earnings Recap,
# so they're kept out of Top Market Stories.
_EARNINGS_TITLE = re.compile(
    r"earnings|\beps\b"
    r"|(?:raises?|raised|cuts?|lowers?|lowered|reaffirms?|boosts?|hikes?|"
    r"trims?|slashes|withdraws?|maintains?) (?:(?:its|their|full-year|"
    r"annual|sales|profit|revenue|\d{4}) ){0,2}(?:guidance|outlook|forecast)"
    r"|(?:strong|weak|solid|record|blowout|disappointing|upbeat|downbeat|"
    r"mixed|soft|stellar|dismal) (?:quarter|results)"
    r"|(?:profit|revenue|net income) (?:jumps?|rises?|falls?|drops?|slumps?|"
    r"surges?|soars?|beats?|misses?|tops?|declines?|grows?|climbs?|slides?)"
    r"|results? (?:beats?|miss(?:es)?|tops?|disappoints?)"
    r"|ahead of (?:its |their )?(?:results|report)"
    r"|(?:to |will )report (?:results|earnings)",
    re.IGNORECASE,
)
# "Beats estimates" is earnings language unless the headline is about an
# economic data release (jobs, inflation, GDP and so on).
_BEAT_MISS = re.compile(
    r"(?:beats?|miss(?:es|ed)?|tops?|topped|exceeds?|exceeded|lags?)"
    r" (?:[\w'.]+ ){0,2}(?:estimates|expectations|forecasts|consensus)",
    re.IGNORECASE,
)
_MACRO_DATA = re.compile(
    r"\b(?:jobs?|payrolls?|employment|unemployment|jobless|inflation|cpi|"
    r"ppi|pce|gdp|economy|economic|retail sales|pmi|housing|home sales|"
    r"consumer confidence|sentiment|wages|claims|data|deficit|trade "
    r"balance|exports|imports|factory|manufacturing|services)\b",
    re.IGNORECASE,
)
# "Quarter"/"Q3" only counts as earnings alongside a results word, so
# macro items like "third-quarter GDP" stay in.
_QUARTER = re.compile(r"\bquarter(?:ly)?\b|\bq[1-4]\b", re.IGNORECASE)
_RESULTS_WORDS = re.compile(
    r"\b(?:results|profits?|revenues?|sales|loss(?:es)?|margins?|"
    r"guidance)\b",
    re.IGNORECASE,
)
# Tell-tale phrases near the top of an article's summary, for stories
# whose headline doesn't mention results (e.g. "Shares slide despite
# strong quarter").
_EARNINGS_SNIPPET = re.compile(
    r"reported (?:its )?(?:first|second|third|fourth|fiscal)[- ]quarter"
    r"|quarterly (?:results|earnings|profit|revenue)"
    r"|earnings per share|adjusted (?:earnings|eps)"
    r"|(?:beat|missed|topped) (?:analysts'? |wall street )?"
    r"(?:estimates|expectations|forecasts)"
    r"|after (?:the company )?report(?:ed|ing) (?:results|earnings)",
    re.IGNORECASE,
)

_OPINION_URL = re.compile(
    r"/opinion|/commentary|/column|breakingviews|/views/|/op-?ed",
    re.IGNORECASE,
)
_OPINION_TITLE = re.compile(r"^\s*(opinion|column|commentary|view)\s*[:|]",
                            re.IGNORECASE)


def is_earnings_story(headline):
    """True if a story is mainly about a company's results."""
    title = headline.get("title", "") or ""
    if _EARNINGS_TITLE.search(title):
        return True
    if _BEAT_MISS.search(title) and not _MACRO_DATA.search(title):
        return True
    if _QUARTER.search(title) and _RESULTS_WORDS.search(title):
        return True
    lead = (headline.get("snippet") or "")[:300]
    return bool(_EARNINGS_SNIPPET.search(lead))


def looks_like_opinion(headline):
    return bool(_OPINION_URL.search(headline.get("url", ""))
                or _OPINION_TITLE.search(headline.get("title", "")))


# ---------------------------------------------------------------------------
# News focus: who the review is for and which sources to trust.
# Everything here can be overridden in config.py.
# ---------------------------------------------------------------------------

DEFAULT_READER_PROFILE = (
    "a Canadian business student building a career in wealth and asset "
    "management. They hold Canadian, U.S. and international (developed "
    "markets) equity ETFs and follow EssilorLuxottica and S&P Global. "
    "They care most about the U.S., Canada and Europe: the Fed, the Bank "
    "of Canada and the ECB, Treasury and Canadian yields, the Canadian "
    "dollar, oil, trade and tariffs, big-tech and AI investment, banks "
    "and private credit, and the asset-management industry itself (fund "
    "flows, fees, ETFs, private markets, deals among asset managers). "
    "They like stories with a clear 'so what' for investors and "
    "thoughtful, contrarian analysis."
)

# Outlets searched first for Top Market Stories and the opinion piece.
DEFAULT_NEWS_DOMAINS = [
    "reuters.com", "bloomberg.com", "ft.com", "wsj.com", "cnbc.com",
    "marketwatch.com", "barrons.com", "economist.com", "apnews.com",
    "nytimes.com", "axios.com", "fortune.com", "businessinsider.com",
    "morningstar.com", "theglobeandmail.com", "financialpost.com",
    "bnnbloomberg.ca", "cbc.ca", "investmentexecutive.com",
    "advisor.ca", "institutionalinvestor.com", "pionline.com",
]

# Outlets never used (mostly regional business sites that flood the
# results with local-market stories).
DEFAULT_NEWS_EXCLUDE_DOMAINS = [
    "economictimes.indiatimes.com", "timesofindia.indiatimes.com",
    "livemint.com", "business-standard.com", "moneycontrol.com",
    "thehindubusinessline.com", "financialexpress.com", "ndtvprofit.com",
    "businesstoday.in", "zeebiz.com", "thehindu.com", "hindustantimes.com",
    "indianexpress.com", "jakartaglobe.id", "thejakartapost.com",
    "antaranews.com", "tempo.co", "kontan.co.id", "cnbcindonesia.com",
    "bisnis.com", "theedgemalaysia.com", "thestar.com.my",
    "bworldonline.com", "inquirer.net", "bangkokpost.com",
    "vietnamnews.vn", "dawn.com", "tribune.com.pk",
]

READER_PROFILE = getattr(config, "READER_PROFILE", DEFAULT_READER_PROFILE)
NEWS_DOMAINS = getattr(config, "NEWS_DOMAINS", DEFAULT_NEWS_DOMAINS)
NEWS_EXCLUDE_DOMAINS = getattr(config, "NEWS_EXCLUDE_DOMAINS",
                               DEFAULT_NEWS_EXCLUDE_DOMAINS)

def _domain(url):
    host = (urlparse(str(url or "")).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _from_domain(url, domains):
    host = _domain(url)
    return any(host == d or host.endswith("." + d) for d in domains)


def is_off_interest(headline):
    """True for stories from excluded outlets."""
    return _from_domain(headline.get("url"), NEWS_EXCLUDE_DOMAINS)


def _focused_search(queries, days, per_query=10, min_results=12):
    """
    Search the preferred outlets first. If that comes back thin, widen to
    the whole web (minus excluded outlets) so the section isn't empty.
    """
    results = []
    for query in queries:
        results.extend(tavily_search(
            query, days=days, max_results=per_query,
            include_domains=NEWS_DOMAINS or None,
            exclude_domains=NEWS_EXCLUDE_DOMAINS or None,
        ))
    if len(dedupe_headlines(results)) < min_results:
        print("Preferred outlets returned few results; widening search.")
        for query in queries:
            results.extend(tavily_search(
                query, days=days, max_results=per_query,
                exclude_domains=NEWS_EXCLUDE_DOMAINS or None,
            ))
    return results


def _preferred_first(items):
    """Newest first, but stories from preferred outlets ahead of others."""
    items = sort_by_recency(items)
    return ([h for h in items if _from_domain(h.get("url"), NEWS_DOMAINS)]
            + [h for h in items
               if not _from_domain(h.get("url"), NEWS_DOMAINS)])


def gather_market_news(limit=20, opinion_limit=8):
    """
    Today's major financial-market stories, minus earnings coverage and
    excluded outlets, plus a separate pool of opinion pieces.
    """
    print("Gathering market news from the web...")
    today = market_today().strftime("%B %d, %Y")

    queries = getattr(config, "NEWS_QUERIES", None) or [
        f"Wall Street stocks bonds Federal Reserve markets {today}",
        f"Treasury yields inflation economy investors {today}",
        f"Bank of Canada TSX Canadian dollar economy {today}",
        f"Canada U.S. trade tariffs markets {today}",
        f"ECB Europe markets economy {today}",
        f"oil gold dollar commodities markets {today}",
        f"asset management ETF fund flows private credit wealth {today}",
        f"AI tech investment big tech markets {today}",
        f"banks mergers deals Wall Street {today}",
    ]
    results = _focused_search(queries, days=1)

    # Keep only stories from roughly the last day; drop earnings and
    # stories from excluded outlets.
    results = [h for h in results if is_recent(h, 30)]
    dropped = [h for h in results if is_earnings_story(h)]
    results = [h for h in results if not is_earnings_story(h)]
    off_topic = [h for h in results if is_off_interest(h)]
    results = [h for h in results if not is_off_interest(h)]
    # Opinion pieces that turn up in the news search join the opinion pool.
    found_opinion = [h for h in results if looks_like_opinion(h)]
    results = [h for h in results if not looks_like_opinion(h)]
    results = _preferred_first(dedupe_headlines(results))[:limit]
    for h in results:
        h["kind"] = "news"

    opinion_queries = getattr(config, "OPINION_QUERIES", None) or [
        f"opinion column markets economy investors {today}",
        f"columnist Fed interest rates stocks bubble {today}",
        f"Breakingviews FT Alphaville Bloomberg Opinion markets {today}",
        f"Globe and Mail Financial Post opinion economy markets {today}",
    ]
    opinions = list(found_opinion) + _focused_search(
        opinion_queries, days=2, min_results=6
    )
    opinions = [h for h in opinions
                if is_recent(h, 54) and not is_earnings_story(h)
                and not is_off_interest(h)]
    # Prefer pieces that are clearly labelled opinion; otherwise let the
    # AI judge from the commentary search results.
    labelled = [h for h in opinions if looks_like_opinion(h)]
    opinions = labelled or opinions
    titles = {h["title"].strip().lower() for h in results}
    opinions = [h for h in _preferred_first(dedupe_headlines(opinions))
                if h["title"].strip().lower() not in titles][:opinion_limit]
    for h in opinions:
        h["kind"] = "opinion"

    print(
        f"Market news: {len(results)} stories, {len(opinions)} opinion "
        f"candidates ({len(labelled)} clearly labelled), "
        f"{len(dropped)} earnings and {len(off_topic)} excluded-outlet "
        f"headlines removed"
    )
    combined = results + opinions
    for i, r in enumerate(combined, start=1):
        print(f"  [{i}] ({r['kind']}) {r['title']} — {r['publisher']}")
    return combined


def gather_sectors(mapping):
    rows = []
    for label, ticker in mapping.items():
        quote = get_quote(ticker)
        if quote is None:
            print(f"Could not fetch ETF data for {label} ({ticker})")
            continue
        rows.append({"label": label, "ticker": ticker, **quote})
    return rows


# ---------------------------------------------------------------------------
# Portfolio calculations
# ---------------------------------------------------------------------------

def calculate_weighted_portfolio_return(rows, field, weights=None):
    if weights is None:
        weights = PORTFOLIO_WEIGHTS
    if not weights:
        return None
    weighted_sum = total_weight = 0.0
    for row in rows:
        value = row.get(field)
        weight = weights.get(row["ticker"])
        if value is None or weight is None:
            continue
        weighted_sum += value * weight
        total_weight += weight
    if total_weight == 0:
        return None
    return weighted_sum / total_weight


# ---------------------------------------------------------------------------
# Rates and cross-asset
# ---------------------------------------------------------------------------

def get_treasury_curve():
    """
    Daily par yield curve straight from the U.S. Treasury, which publishes
    the same afternoon. (FRED's copy lags a day, which is why the old
    report showed yesterday's yields.)
    """
    year = market_today().year
    rows = []
    for y in (year, year - 1):
        url = (
            "https://home.treasury.gov/resource-center/data-chart-center/"
            f"interest-rates/daily-treasury-rates.csv/{y}/all"
            f"?type=daily_treasury_yield_curve&field_tdr_date_value={y}"
            "&page&_format=csv"
        )
        try:
            r = requests.get(
                url, timeout=20, headers={"User-Agent": "Mozilla/5.0"}
            )
            r.raise_for_status()
        except Exception as e:
            print(f"Treasury curve fetch failed for {y}: {e}")
            continue
        for row in csv.DictReader(io.StringIO(r.text)):
            try:
                d = datetime.strptime(row.get("Date", ""), "%m/%d/%Y").date()
            except ValueError:
                continue
            rows.append((d, row))
        if len(rows) >= 2:
            break
    rows.sort(key=lambda x: x[0])
    return rows


def get_fred_series(series_id):
    """Fallback: two latest observations from FRED (lags by one day)."""
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
    except Exception as e:
        print(f"FRED {series_id} failed: {e}")
        return None
    obs = []
    for row in csv.DictReader(io.StringIO(response.text)):
        raw = row.get(series_id)
        if raw in (None, "", "."):
            continue
        try:
            obs.append({"date": row.get("observation_date"), "value": float(raw)})
        except ValueError:
            continue
    if len(obs) < 2:
        return None
    return obs[-2], obs[-1]


def _to_float(value):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def gather_rates():
    tenors = [("2Y Treasury", "2 Yr", "DGS2"),
              ("10Y Treasury", "10 Yr", "DGS10"),
              ("30Y Treasury", "30 Yr", "DGS30")]
    rates = []

    curve = get_treasury_curve()
    if len(curve) >= 2:
        (prev_date, prev_row), (last_date, last_row) = curve[-2], curve[-1]
        for label, column, _ in tenors:
            latest = _to_float(last_row.get(column))
            previous = _to_float(prev_row.get(column))
            if latest is None or previous is None:
                continue
            rates.append({
                "label": label,
                "value": latest,
                "daily_bps": (latest - previous) * 100,
                "as_of": last_date.isoformat(),
            })
    if not rates:
        print("Treasury curve unavailable; falling back to FRED.")
        for label, _, series_id in tenors:
            result = get_fred_series(series_id)
            if not result:
                continue
            previous, latest = result
            rates.append({
                "label": label,
                "value": latest["value"],
                "daily_bps": (latest["value"] - previous["value"]) * 100,
                "as_of": latest["date"],
            })

    # 2s10s curve slope
    by_label = {r["label"]: r for r in rates}
    two, ten = by_label.get("2Y Treasury"), by_label.get("10Y Treasury")
    if two and ten:
        rates.append({
            "label": "2s10s Spread",
            "value": ten["value"] - two["value"],
            "daily_bps": ten["daily_bps"] - two["daily_bps"],
            "as_of": ten["as_of"],
            "is_spread": True,
        })
    return rates


def gather_macro_data():
    data = {"rates": gather_rates(), "cross_asset": []}
    cross_asset_map = {
        "DXY": "DX-Y.NYB",
        "Gold": "GC=F",
        "Brent Crude": "BZ=F",
        "VIX": "^VIX",
    }
    for label, ticker in cross_asset_map.items():
        quote = get_quote(ticker)
        if quote is None:
            continue
        data["cross_asset"].append({
            "label": label,
            "ticker": ticker,
            "value": quote["price"],
            **quote,
        })
    return data


# ---------------------------------------------------------------------------
# Earnings: recent results and upcoming reports
# ---------------------------------------------------------------------------

_NASDAQ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}

_TIME_LABELS = {
    "time-pre-market": "Before open",
    "time-after-hours": "After close",
    "time-not-supplied": "Time n/a",
}


def parse_money(value):
    if not value:
        return 0.0
    cleaned = re.sub(r"[^0-9.\-]", "", str(value))
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def format_cap(value):
    if not value:
        return "—"
    if value >= 1e12:
        return f"${value / 1e12:.2f}T"
    if value >= 1e9:
        return f"${value / 1e9:.1f}B"
    return f"${value / 1e6:.0f}M"


def fmt_clock(dt):
    """'6:42 PM ET' style label for an aware datetime."""
    if not dt:
        return ""
    return dt.astimezone(MARKET_TZ).strftime("%I:%M %p ET").lstrip("0")


def _after_hours_quote(ticker, regular_close):
    """
    Latest after-hours (post-4:00 PM ET) price for today, and its change
    versus today's regular-session close. Returns None if nothing has
    traded after the close yet or the data isn't available.
    """
    if not regular_close or "^" in ticker:
        return None
    today = market_today()
    stock = yf.Ticker(ticker)
    price = stamp = None

    # Primary source: one-minute bars including extended hours.
    try:
        intra = stock.history(
            period="5d", interval="1m", prepost=True, auto_adjust=False
        )
    except Exception as e:
        print(f"After-hours fetch failed for {ticker}: {e}")
        intra = None
    if intra is not None and not intra.empty and "Close" in intra:
        intra = intra.dropna(subset=["Close"])
        for ts, close in zip(intra.index, intra["Close"].tolist()):
            local = (ts.tz_convert(MARKET_TZ) if ts.tzinfo is not None
                     else ts.tz_localize(MARKET_TZ))
            if local.date() == today and local.hour >= 16:
                price, stamp = float(close), local.to_pydatetime()

    # Fallback: Yahoo's quote summary, which carries a post-market price.
    if price is None:
        try:
            info = stock.info or {}
            p, t = info.get("postMarketPrice"), info.get("postMarketTime")
            if p and t:
                local = datetime.fromtimestamp(int(t), tz=timezone.utc)
                local = local.astimezone(MARKET_TZ)
                if local.date() == today and local.hour >= 16:
                    price, stamp = float(p), local
        except Exception:
            pass

    if price is None:
        return None
    return {
        "price": price,
        "change": (price - regular_close) / regular_close * 100,
        "as_of": stamp,
    }


def fetch_earnings_calendar(day):
    """All companies reporting on a given date (Nasdaq earnings calendar)."""
    try:
        r = requests.get(
            "https://api.nasdaq.com/api/calendar/earnings",
            params={"date": day.isoformat()},
            headers=_NASDAQ_HEADERS,
            timeout=20,
        )
        r.raise_for_status()
        payload = r.json() or {}
    except Exception as e:
        print(f"Earnings calendar failed for {day}: {e}")
        return None

    rows = ((payload.get("data") or {}).get("rows")) or []
    out = []
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        out.append({
            "ticker": symbol,
            "name": str(row.get("name") or symbol).strip(),
            "date": day,
            "timing": _TIME_LABELS.get(row.get("time"), "Time n/a"),
            "market_cap": parse_money(row.get("marketCap")),
            "eps_forecast": str(row.get("epsForecast") or "").strip(),
            "eps_actual": str(row.get("eps") or "").strip(),
            "surprise": str(row.get("surprise") or "").strip(),
        })
    return out


def gather_recent_earnings():
    """
    Biggest companies reporting today (before the open, after the close,
    or time not supplied). Companies that reported after the previous
    session's close are left out: they were already covered, with their
    after-hours move, in the previous day's email.
    """
    today = market_today()

    candidates = []
    calendar_ok = False

    today_rows = fetch_earnings_calendar(today)
    if today_rows is not None:
        calendar_ok = True
        candidates += today_rows

    candidates = [
        c for c in candidates if c["market_cap"] >= RECENT_EARNINGS_MIN_CAP
    ]
    candidates.sort(key=lambda c: c["market_cap"], reverse=True)
    candidates = list({c["ticker"]: c for c in candidates}.values())
    candidates = candidates[:RECENT_EARNINGS_LIMIT]

    for c in candidates:
        _EXTRA_NAMES.setdefault(c["ticker"], c["name"])
        quote = get_quote(c["ticker"])
        fresh = quote is not None and not quote["stale"]

        # Regular-session move and close today, and any trading since the
        # 4 PM close.
        c["session"] = quote["daily"] if fresh else None
        c["close"] = quote["price"] if fresh else None
        ah = _after_hours_quote(c["ticker"], c["close"])
        c["after_hours"] = ah["change"] if ah else None
        c["after_hours_price"] = ah["price"] if ah else None
        c["after_hours_time"] = ah["as_of"] if ah else None

        reported_after_today_close = (
            c["date"] == today and c["timing"] == "After close"
        )
        c["reported_after_today_close"] = reported_after_today_close
        if reported_after_today_close:
            # The session move happened before the numbers came out, so
            # the after-hours move is the real earnings reaction.
            c["reaction"] = c["after_hours"]
            c["reaction_source"] = "after_hours"
            if ah:
                c["reaction_note"] = (
                    f"Reported after today's close; stock "
                    f"{format_pct(ah['change'])} in after-hours trading "
                    f"as of {fmt_clock(ah['as_of'])} (preliminary, thin volume)"
                )
            else:
                c["reaction_note"] = (
                    "Reported after today's close; no after-hours trades "
                    "recorded yet"
                )
        elif c["session"] is not None:
            c["reaction"] = c["session"]
            c["reaction_source"] = "session"
            c["reaction_note"] = f"Stock {format_pct(c['session'])} today"
            if ah:
                c["reaction_note"] += (
                    f", {format_pct(ah['change'])} after hours"
                )
        else:
            c["reaction"] = None
            c["reaction_source"] = None
            c["reaction_note"] = "Price reaction unavailable"

        news = tavily_search(
            f'"{c["name"]}" quarterly earnings results revenue guidance',
            days=3,
            max_results=6,
        )
        relevant = [
            h for h in news
            if mentions_company(c["ticker"], f"{h['title']} {h['snippet']}")
        ]
        c["headlines"] = sort_by_recency(dedupe_headlines(relevant))[:4]

    general = []
    if len(candidates) < 3:
        print("Few earnings candidates; adding general earnings news search.")
        general = tavily_search(
            f"major company quarterly earnings results "
            f"{today.strftime('%B %d, %Y')}",
            days=2,
            max_results=8,
        )
        general = dedupe_headlines(general)

    print(
        f"Recent earnings: {len(candidates)} companies "
        f"(calendar {'ok' if calendar_ok else 'unavailable'})"
    )
    return {"companies": candidates, "general": general}


def extract_earnings_dates(calendar):
    """Handle common yfinance earnings calendar formats."""
    results = []
    if calendar is None:
        return results

    def norm(value):
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if hasattr(value, "to_pydatetime"):
            try:
                return value.to_pydatetime().date()
            except Exception:
                return None
        return None

    if isinstance(calendar, dict):
        raw = calendar.get("Earnings Date")
        if raw is None:
            return results
        if not isinstance(raw, (list, tuple)):
            raw = [raw]
        return [d for d in (norm(v) for v in raw) if d]

    try:
        if hasattr(calendar, "index") and "Earnings Date" in calendar.index:
            row = calendar.loc["Earnings Date"]
            values = row.tolist() if hasattr(row, "tolist") else [row]
            results = [d for d in (norm(v) for v in values) if d]
    except Exception:
        pass
    return results


def next_business_days(start, count):
    days, d = [], start
    while len(days) < count:
        d = next_day(d)
        if d.weekday() < 5:
            days.append(d)
    return days


def gather_upcoming_earnings():
    """
    Largest reports over the next two business days, plus any tracked
    holdings in that window, each with preview coverage for the AI blurb.
    """
    today = market_today()
    window = next_business_days(today, UPCOMING_EARNINGS_DAYS)
    horizon = window[-1]
    tracked = list(dict.fromkeys(config.TICKERS + config.PORTFOLIO))

    upcoming = []
    for day in window:
        rows = fetch_earnings_calendar(day) or []
        upcoming += [
            r for r in rows if r["market_cap"] >= UPCOMING_EARNINGS_MIN_CAP
        ]

    upcoming.sort(key=lambda r: r["market_cap"], reverse=True)
    upcoming = upcoming[:UPCOMING_EARNINGS_LIMIT]
    listed = {r["ticker"] for r in upcoming}

    # Always include tracked holdings reporting in the window.
    for ticker in tracked:
        try:
            dates = extract_earnings_dates(yf.Ticker(ticker).calendar)
        except Exception as e:
            print(f"Calendar check failed for {ticker}: {e}")
            continue
        for d in dates:
            if today < d <= horizon:
                base = ticker.split(".")[0]
                if ticker in listed or base in listed:
                    for r in upcoming:
                        if r["ticker"] in (ticker, base):
                            r["tracked"] = True
                else:
                    upcoming.append({
                        "ticker": ticker,
                        "name": company_name(ticker),
                        "date": d,
                        "timing": "Time n/a",
                        "market_cap": 0.0,
                        "eps_forecast": "",
                        "tracked": True,
                    })
                break

    for r in upcoming:
        r.setdefault("tracked", r["ticker"] in tracked)
        _EXTRA_NAMES.setdefault(r["ticker"], r["name"])

        news = tavily_search(
            f'"{r["name"]}" earnings preview what to expect analysts',
            days=7,
            max_results=6,
        )
        relevant = [
            h for h in news
            if mentions_company(r["ticker"], f"{h['title']} {h['snippet']}")
        ]
        r["headlines"] = sort_by_recency(dedupe_headlines(relevant))[:3]

    upcoming.sort(key=lambda r: (r["date"], -r["market_cap"]))
    print(
        f"Upcoming earnings ({window[0]} to {horizon}): "
        f"{', '.join(r['ticker'] for r in upcoming) or 'none'}"
    )
    return upcoming


def gather_macro_headlines():
    """Macro-related headlines from Yahoo's market feeds."""
    macro_keywords = [
        "fed", "inflation", "cpi", "ppi", "jobs", "payroll", "rate",
        "treasury", "yield", "gdp", "employment", "consumer", "economic",
    ]
    out, seen = [], set()
    for source in ["SPY", "^TNX", "^VIX", "TLT"]:
        for h in get_headlines(source, limit=10):
            if not is_recent(h, 48):
                continue
            title = h["title"].lower()
            if not any(k in title for k in macro_keywords):
                continue
            if title in seen:
                continue
            seen.add(title)
            out.append(h)
            if len(out) >= 6:
                return out
    return out


def gather_lookahead():
    return {
        "earnings": gather_upcoming_earnings(),
        "macro": gather_macro_headlines(),
    }


# ---------------------------------------------------------------------------
# Groq API
# ---------------------------------------------------------------------------

def _retry_after_seconds(response, default=20.0, cap=65.0):
    header = response.headers.get("retry-after")
    if header:
        try:
            return min(float(header) + 1, cap)
        except ValueError:
            pass
    match = re.search(r"try again in ([\d.]+)(ms|s|m)", response.text)
    if match:
        value, unit = float(match.group(1)), match.group(2)
        secs = value / 1000 if unit == "ms" else value * 60 if unit == "m" else value
        return min(secs + 1, cap)
    return default


def _groq_call(prompt, max_tokens=4000, temperature=0.3, label="ai"):
    """Call Groq, log useful diagnostics, and parse its JSON response."""
    api_key = getattr(config, "GROQ_API_KEY", "")
    if not api_key:
        print("Groq API key is missing (GROQ_API_KEY).")
        return None

    payload = {
        "model": "openai/gpt-oss-120b",
        "max_tokens": max_tokens,
        "temperature": temperature,
        # gpt-oss "thinks" before answering, and that hidden reasoning
        # counts against max_tokens. Medium keeps quality without
        # crowding out the actual answer.
        "reasoning_effort": "medium",
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": "You reply with a single valid JSON object only.",
            },
            {"role": "user", "content": prompt},
        ],
    }
    print(
        f"Calling Groq [{label}]: prompt_length={len(prompt)}, "
        f"max_tokens={max_tokens}"
    )

    response = None
    for attempt in range(1, 5):
        try:
            response = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=180,
            )
        except Exception as e:
            print(f"Groq request exception: {type(e).__name__}: {e}")
            time.sleep(10)
            continue

        status = response.status_code
        print(f"Groq [{label}] HTTP status: {status} (attempt {attempt})")
        if status == 200:
            break

        body_text = response.text[:2000]
        print(f"Groq error body: {body_text}")

        if status == 400 and ("response_format" in body_text
                              or "json_validate_failed" in body_text):
            # JSON mode rejected or the model's output failed validation;
            # retry once in plain mode and parse the JSON ourselves.
            payload.pop("response_format", None)
            continue
        if status in (413, 429) or status >= 500:
            wait = _retry_after_seconds(response)
            print(f"Groq rate-limited or busy; waiting {wait:.0f}s.")
            time.sleep(wait)
            continue
        return None

    if response is None or response.status_code != 200:
        print(f"Groq [{label}] failed after retries.")
        return None

    try:
        body = response.json()
    except Exception as e:
        print(f"Groq returned non-JSON: {e}; body={response.text[:1000]}")
        return None

    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        print(f"Groq response missing choices: {json.dumps(body)[:2000]}")
        return None

    choice = choices[0] or {}
    message = choice.get("message") or {}
    content = message.get("content")
    print(f"Groq finish_reason: {choice.get('finish_reason')}")
    print(f"Groq usage: {json.dumps(body.get('usage', {}))}")

    raw = content.strip() if isinstance(content, str) else ""
    if not raw:
        print(f"Groq returned empty content: {json.dumps(message)[:1000]}")
        return None

    if raw.startswith("```"):
        lines = raw.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()

    first, last = raw.find("{"), raw.rfind("}")
    if first != -1 and last >= first:
        raw = raw[first:last + 1]

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Groq JSON parse failed: {e}")
        print(f"Groq raw content (first 4000 chars): {raw[:4000]}")
        if choice.get("finish_reason") == "length":
            print("Groq output hit the token limit.")
        return None

    if not isinstance(parsed, dict):
        print(f"Groq JSON top-level was {type(parsed).__name__}, not object.")
        return None

    shape = {k: type(v).__name__ for k, v in parsed.items()}
    print(f"Groq [{label}] JSON parsed; keys/types={shape}")
    return parsed


# ---------------------------------------------------------------------------
# Tolerant readers for model output
#
# The model doesn't always follow the requested shape exactly (a bullet
# list comes back as one string, "title" becomes "headline", everything
# gets wrapped in an extra {"report": {...}} layer, and so on). Strict
# parsing silently threw those sections away; these helpers accept the
# common variations instead.
# ---------------------------------------------------------------------------

def _nk(key):
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _pick(d, *keys):
    if not isinstance(d, dict):
        return None
    norm = {_nk(k): v for k, v in d.items()}
    for k in keys:
        v = norm.get(_nk(k))
        if v not in (None, "", [], {}):
            return v
    return None


def _unwrap(parsed, expected):
    """If the expected keys are nested one level down, return that level."""
    if not isinstance(parsed, dict):
        return {}
    if _pick(parsed, *expected) is not None:
        return parsed
    for v in parsed.values():
        if isinstance(v, dict) and _pick(v, *expected) is not None:
            return v
    return parsed


def _text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        v = _pick(value, "text", "point", "bullet", "summary", "content",
                  "note", "blurb", "value", "description")
        if v is not None:
            return _text(v)
        return " ".join(_text(x) for x in value.values() if _text(x))
    if isinstance(value, (list, tuple)):
        return " ".join(_text(x) for x in value if _text(x))
    return str(value).strip()


_BULLET_PREFIX = re.compile(r"^\s*(?:[-*•·]+|\d+[.)])\s*")


def _as_text_list(value, limit=None):
    items = []
    if isinstance(value, str):
        items = [_BULLET_PREFIX.sub("", line).strip()
                 for line in value.splitlines()]
    elif isinstance(value, (list, tuple)):
        items = [_text(v) for v in value]
    elif isinstance(value, dict):
        items = [_text(v) for v in value.values()]
    items = [i for i in items if i]
    return items[:limit] if limit else items


def _as_int_list(value):
    if isinstance(value, bool):
        return []
    if isinstance(value, int):
        return [value]
    if isinstance(value, str):
        return [int(x) for x in re.findall(r"\d+", value)]
    if isinstance(value, (list, tuple)):
        out = []
        for v in value:
            out += _as_int_list(v)
        return out
    return []


def _clean_cites(value):
    return _as_int_list(value)


def _fmt_headline_lines(headlines, snippet_chars=280):
    lines = []
    for i, h in enumerate(headlines, start=1):
        lines.append(
            f"  [{i}] ({fmt_published(h.get('published'))}) "
            f"{h['title']} — {h['publisher']}"
        )
        snippet = (h.get("snippet") or "").replace("\n", " ")
        if snippet:
            lines.append(f"      {snippet[:snippet_chars]}")
    return lines


def _keyed_entries(value, key_field="ticker"):
    """Accept {"AAPL": {...}} or [{"ticker": "AAPL", ...}] alike."""
    out = {}
    if isinstance(value, dict):
        for k, v in value.items():
            out[str(k).strip().upper()] = v
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                k = _text(_pick(item, key_field, "symbol"))
                if k:
                    out[k.upper()] = item
    return out


# ---------------------------------------------------------------------------
# Prompt formatting shared by the AI calls
# ---------------------------------------------------------------------------

def _stale_tag(r):
    return f" [last data {r['as_of']}]" if r.get("stale") else ""


def _fmt_rows(rows, key="ticker", label_key="name"):
    return "\n".join(
        f"{r[key]} ({r[label_key]}) | daily {r['daily']:+.2f}% | "
        f"YTD {r['ytd']:+.2f}%{_stale_tag(r)}"
        for r in rows
    ) or "(unavailable)"


def _fmt_macro(m):
    lines = []
    for r in m.get("rates", []):
        lines.append(
            f"{r['label']} | {r['value']:.2f}% | "
            f"{r['daily_bps']:+.1f} bps | as of {r['as_of']}"
        )
    for r in m.get("cross_asset", []):
        lines.append(
            f"{r['label']} | {r['value']:.2f} | daily "
            f"{r['daily']:+.2f}% | YTD {r['ytd']:+.2f}%{_stale_tag(r)}"
        )
    return "\n".join(lines) or "(no macro data)"


def _report_date():
    return market_today().strftime("%A, %B %d, %Y")


# ---------------------------------------------------------------------------
# AI: earnings recap (companies that just reported)
# ---------------------------------------------------------------------------

def build_earnings_summary(recent):
    companies = recent.get("companies", [])
    general = recent.get("general", [])
    if not companies and not general:
        return []

    lines = []
    for c in companies:
        lines.append(
            f"\n{c['ticker']} ({c['name']}) | market cap "
            f"{format_cap(c['market_cap'])} | reported {c['date']} "
            f"{c['timing']} | EPS estimate {c['eps_forecast'] or 'n/a'} | "
            f"EPS actual {c.get('eps_actual') or 'n/a'} | "
            f"surprise {c.get('surprise') or 'n/a'} | {c['reaction_note']}"
        )
        if c["headlines"]:
            lines += _fmt_headline_lines(c["headlines"], snippet_chars=450)
        else:
            lines.append("  (no results coverage found)")

    general_block = ""
    if general:
        general_block = (
            "\nGENERAL EARNINGS NEWS (use for companies not listed above):\n"
            + "\n".join(_fmt_headline_lines(general, snippet_chars=450))
        )

    prompt = f"""
You are writing the earnings recap section of a professional daily market
review dated {_report_date()}, delivered after the U.S. close.

COMPANIES THAT REPORTED (largest first):
{chr(10).join(lines) if lines else "(none from calendar)"}
{general_block}

Return JSON exactly in this shape:
{{"recent_earnings": [{{"ticker": "MSFT", "company": "Microsoft",
  "blurb": "3-5 sentences", "cites": [1, 2]}}]}}

RULES
- One entry per listed company that has results coverage. If GENERAL
  EARNINGS NEWS is supplied, you may add up to 3 other major companies
  that clearly reported today ({_report_date()}), either before today's
  open or after today's close; their cites refer to that list. Do not add
  companies that reported on an earlier day, including after the previous
  day's close, since those were covered in the previous report.
- Each blurb: EPS and revenue versus expectations, guidance, the key
  business driver(s), and the stock reaction given above.
- For companies that reported after today's close, the after-hours move
  is the earnings reaction; today's regular-session move happened before
  the results and should not be described as a reaction to them. Call
  after-hours moves early or preliminary, since volume is thin and they
  can change by the next open.
- Use only numbers and facts in the supplied material. If coverage is
  thin, say what is known and that full details weren't available.
- Ignore headlines not about that company's own results.
- Skip companies with no coverage rather than guessing.
"""
    parsed = _groq_call(prompt, max_tokens=3500, temperature=0.2,
                        label="earnings recap")
    if not parsed:
        return []

    items = _pick(_unwrap(parsed, ["recent_earnings"]),
                  "recent_earnings", "earnings", "companies")
    out = []
    for ticker, item in _keyed_entries(items).items():
        if not isinstance(item, dict):
            item = {"blurb": item}
        blurb = _text(_pick(item, "blurb", "summary", "note", "text"))
        if not blurb:
            continue
        out.append({
            "ticker": ticker,
            "company": _text(_pick(item, "company", "name")),
            "blurb": blurb,
            "cites": _clean_cites(_pick(item, "cites", "sources")),
        })
    return out


# ---------------------------------------------------------------------------
# AI: upcoming earnings previews (next two business days)
# ---------------------------------------------------------------------------

def build_earnings_previews(upcoming):
    if not upcoming:
        return {}

    lines = []
    for e in upcoming:
        tracked = " [TRACKED HOLDING]" if e.get("tracked") else ""
        lines.append(
            f"\n{e['ticker']} ({e['name']}){tracked} | reports "
            f"{e['date'].strftime('%a %b %d')} {e['timing']} | mkt cap "
            f"{format_cap(e['market_cap'])}"
        )
        if e.get("headlines"):
            lines += _fmt_headline_lines(e["headlines"], snippet_chars=400)
        else:
            lines.append("  (no preview coverage found)")

    prompt = f"""
You are writing the "Upcoming Earnings" section of a professional daily
market review dated {_report_date()}. For each company reporting in the
next two business days, name the single most important thing investors
should be looking for.

COMPANIES:
{chr(10).join(lines)}

Return JSON exactly in this shape:
{{"previews": [{{"ticker": "MSFT", "blurb": "one sentence",
  "cites": [1]}}]}}

RULES
- Write a preview for EVERY company listed.
- Each blurb is ONE sentence naming the ONE thing most likely to move
  the stock (a specific segment, metric, guidance item or issue raised in
  the preview coverage) and briefly why it matters. Don't list several
  items, and don't mention EPS estimates.
- Use preview coverage where supplied and cite it by number. Without
  coverage, you may rely on the company's well-known business drivers,
  but never invent figures, estimates or events.
- Be specific to the company; avoid generic lines like "investors will
  watch results closely".
"""
    parsed = _groq_call(prompt, max_tokens=3000, temperature=0.3,
                        label="earnings previews")
    if not parsed:
        return {}

    items = _pick(_unwrap(parsed, ["previews"]),
                  "previews", "upcoming_earnings", "earnings")
    out = {}
    for ticker, item in _keyed_entries(items).items():
        if not isinstance(item, dict):
            item = {"blurb": item}
        blurb = _text(_pick(item, "blurb", "preview", "summary", "note"))
        if blurb:
            out[ticker] = {
                "blurb": blurb,
                "cites": _clean_cites(_pick(item, "cites", "sources")),
            }
    return out


# ---------------------------------------------------------------------------
# AI: company-by-company review
# ---------------------------------------------------------------------------

def build_ticker_notes(tickers):
    if not tickers:
        return {}

    lines = []
    for r in tickers:
        lines.append(
            f"\n{r['ticker']} — {r['name']} | daily {r['daily']:+.2f}% | "
            f"YTD {r['ytd']:+.2f}%{_stale_tag(r)}"
        )
        bench = r.get("benchmarks") or []
        if bench:
            lines.append("  Benchmarks today: " + "; ".join(
                f"{b['label']} ({b['ticker']}) {b['daily']:+.2f}%{_stale_tag(b)}"
                for b in bench
            ))
        if r["headlines"]:
            lines += _fmt_headline_lines(r["headlines"], snippet_chars=300)
        else:
            lines.append("  (no recent company-specific headlines found)")

    prompt = f"""
You are writing the company-by-company review in a professional daily
market review dated {_report_date()}, sent after the U.S. close. Price
moves are today's close unless tagged "[last data ...]". Each ticker
shows how its market and sector benchmarks moved today, and each
headline shows its publication time (up to a week back).

TRACKED TICKERS:
{chr(10).join(lines)}

Return JSON exactly in this shape:
{{"tickers": {{"EL.PA": {{"note": "2-4 sentences", "cites": [1, 2]}}}}}}

RULES
- Every ticker MUST get a note, keyed by its exact ticker.
- The company is the one named after the ticker (e.g. EL.PA is
  EssilorLuxottica). Never describe a different company.
- Focus on today's company-specific developments: earnings, guidance,
  products, regulation, analyst actions, industry news. Cite headline
  numbers from that ticker's own list.
- If nothing is from today, say there was no fresh company-specific news
  today. Recent headlines from earlier in the week (for example, Friday's
  news in a Monday report) are useful background: use them, and say when
  they were published.
- Always explain the move relative to its benchmarks. If the stock moved
  roughly in line with its market or sector (within about 1 percentage
  point), say it moved with the market or sector; that is an explanation,
  not an unexplained move.
- Only if the stock diverged from its benchmarks by roughly 1 point or
  more and no headline accounts for it, say the divergence is unexplained
  by the sources reviewed. Don't invent a company-specific cause from the
  price move alone. Never fabricate events or sources.
"""
    parsed = _groq_call(prompt, max_tokens=3500, temperature=0.3,
                        label="tickers")
    if not parsed:
        return {}

    raw = _pick(_unwrap(parsed, ["tickers"]), "tickers", "companies")
    if raw is None:
        raw = parsed
    entries = _keyed_entries(raw)
    notes = {}
    for r in tickers:
        entry = entries.get(r["ticker"].upper())
        if entry is None:
            continue
        if isinstance(entry, dict):
            notes[r["ticker"]] = {
                "note": _text(_pick(entry, "note", "summary", "text")),
                "cites": _clean_cites(_pick(entry, "cites", "sources")),
            }
        else:
            notes[r["ticker"]] = {"note": _text(entry), "cites": []}
    return notes


# ---------------------------------------------------------------------------
# AI: top market stories
# ---------------------------------------------------------------------------

def _parse_stories(parsed, market_news):
    raw = _pick(_unwrap(parsed, ["stories"]),
                "stories", "top_stories", "market_stories")
    if isinstance(raw, dict):
        raw = list(raw.values())
    if not isinstance(raw, list):
        return []
    stories = []
    for s in raw[:8]:
        if not isinstance(s, dict):
            continue
        summary = _text(_pick(s, "summary", "body", "text", "content",
                              "analysis", "story"))
        if not summary:
            continue
        idx = _as_int_list(_pick(s, "source_index", "source", "index",
                                 "cite", "cites"))
        idx = idx[0] if idx else None
        valid = isinstance(idx, int) and 1 <= idx <= len(market_news)
        title = _text(_pick(s, "title", "headline", "name"))
        if not title and valid:
            title = market_news[idx - 1]["title"]
        # The source list decides what's opinion, not the model's label.
        kind = market_news[idx - 1].get("kind", "news") if valid else "news"
        source = market_news[idx - 1] if valid else {}
        if (is_earnings_story({"title": title})
                or (source and is_earnings_story(source))):
            print(f"Dropping earnings story from Top Stories: {title}")
            continue
        stories.append({"title": title, "summary": summary,
                        "source_index": idx, "kind": kind})

    # Exactly one opinion piece, listed last.
    news = [s for s in stories if s["kind"] != "opinion"]
    opinion = [s for s in stories if s["kind"] == "opinion"][:1]
    return news[:4] + opinion


def _stories_complete(stories, market_news):
    """Four news stories plus one opinion piece, when the sources allow it."""
    have_opinion = any(h.get("kind") == "opinion" for h in market_news)
    news_available = sum(1 for h in market_news if h.get("kind") != "opinion")
    want_news = min(4, news_available)
    got_news = sum(1 for s in stories if s["kind"] != "opinion")
    got_opinion = any(s["kind"] == "opinion" for s in stories)
    return got_news >= want_news and (got_opinion or not have_opinion)


def build_market_story_summaries(market_news):
    if not market_news:
        return []

    def block(kind):
        out = []
        for i, h in enumerate(market_news, start=1):
            if h.get("kind", "news") != kind:
                continue
            out.append(f"[{i}] {h['title']}")
            out.append(f"    Publisher: {h['publisher']} | "
                       f"Published: {fmt_published(h.get('published'))} | "
                       f"URL: {h.get('url', '')}")
            if h.get("snippet"):
                out.append(f"    Summary: {h['snippet'][:700]}")
        return "\n".join(out) or "(none)"

    prompt = f"""
You are writing the "Top Market Stories" section of a professional daily
market review dated {_report_date()}, sent after the U.S. close.

THE READER: {READER_PROFILE}

NEWS HEADLINES:
{block("news")}

OPINION AND COMMENTARY CANDIDATES:
{block("opinion")}

Return JSON exactly in this shape:
{{"stories": [{{"title": "headline", "summary": "150-220 words",
  "source_index": 1}}]}}

RULES
- Return exactly 5 entries: 4 news stories followed by 1 opinion piece.
- News stories: the 4 DISTINCT stories from NEWS HEADLINES that matter
  most to THE READER, like tonight's front page of Bloomberg, the FT or
  the Globe and Mail's Report on Business: central banks, economic data,
  policy, trade, big moves in rates/currencies/commodities/credit, and
  major industry, deal or asset-management news.
- Favour stories about the U.S., Canada and Europe, and at least one
  story with a Canadian angle when a substantive one is available. Skip
  stories about local markets or economies elsewhere (e.g. India,
  Indonesia, Southeast Asia) unless they are clearly moving global
  markets. China and Japan count only when the story is about their
  effect on global markets, trade or rates.
- Prefer stories that are genuinely interesting: a surprising shift, a
  real debate, or a development with clear consequences for investors,
  over routine recaps.
- NO earnings stories. Skip anything about a company's quarterly results,
  EPS, revenue versus estimates, guidance, or an upcoming report, even if
  it seems important. Also skip market wraps or roundups driven mainly by
  earnings. Earnings are covered elsewhere in the review, so don't
  discuss individual companies' results inside the summaries either.
  This applies to the opinion piece too.
- Don't just restate today's index performance.
- Each news summary: what happened, context, why investors care, what
  could matter next.
- Opinion piece: choose the single most thoughtful markets or economics
  opinion, column or commentary piece from OPINION AND COMMENTARY
  CANDIDATES, favouring the one THE READER would find most
  interesting. It must be an argument from a columnist or commentator,
  not a straight news report, and not about earnings. Its summary states
  the author's argument and evidence, attributes it to them ("the
  columnist argues..."), and closes with the strongest counterpoint or
  what would prove it wrong. Never present the author's view as fact.
- source_index is the number of the item the entry is based on.
- Use only the supplied material; never invent facts, authors or quotes.
- If no candidate is genuinely an opinion piece, return only the 4 news
  stories. If there are fewer than 4 distinct non-earnings news stories,
  return those you have.
"""
    stories = []
    attempts = ((1, 0.3), (2, 0.2), (3, 0.4))
    for attempt, temp in attempts:
        parsed = _groq_call(prompt, max_tokens=4500, temperature=temp,
                            label="stories" if attempt == 1
                            else f"stories retry {attempt - 1}")
        result = _parse_stories(parsed, market_news) if parsed else []
        if (_stories_complete(result, market_news)
                or len(result) > len(stories)):
            stories = result
        if _stories_complete(stories, market_news):
            break
        print(f"Stories incomplete ({len(stories)} returned); "
              + ("retrying." if attempt < len(attempts) else "keeping best."))
    return stories


# ---------------------------------------------------------------------------
# AI: executive summary, implications, forward view, bottom line
# ---------------------------------------------------------------------------

def _parse_overview(parsed):
    p = _unwrap(parsed, ["executive_summary", "bottom_line"])
    impl = _pick(p, "investment_implications", "implications") or {}
    if not isinstance(impl, dict):
        impl = {}
    # Some responses put bull/bear at the top level instead.
    bull = _pick(impl, "bull_case", "bull", "bullish") or _pick(
        p, "bull_case", "bull")
    bear = _pick(impl, "bear_case", "bear", "bearish") or _pick(
        p, "bear_case", "bear")
    key_q = _pick(impl, "key_question", "question") or _pick(
        p, "key_question")
    return {
        "executive_summary": _as_text_list(
            _pick(p, "executive_summary", "summary", "key_takeaways"), 5),
        "investment_implications": {
            "bull_case": _as_text_list(bull, 3),
            "bear_case": _as_text_list(bear, 3),
            "key_question": _text(key_q),
        },
        "looking_ahead": _text(_pick(p, "looking_ahead", "forward_view",
                                     "outlook")),
        "bottom_line": _text(_pick(p, "bottom_line", "conclusion")),
    }


def build_overview(indices, sectors, factors, tickers, portfolio, macro,
                   stories, earnings_notes, upcoming, previews, macro_heads):
    portfolio_daily = calculate_weighted_portfolio_return(portfolio, "daily")
    portfolio_ytd = calculate_weighted_portfolio_return(portfolio, "ytd")
    if portfolio_daily is not None and portfolio_ytd is not None:
        port = (f"Weighted portfolio: daily {portfolio_daily:+.2f}%, "
                f"YTD {portfolio_ytd:+.2f}%")
    else:
        port = "No portfolio weights supplied."

    story_lines = "\n".join(
        f"- {'[OPINION — a columnist view, not fact] ' if s.get('kind') == 'opinion' else ''}"
        f"{s['title']}: {s['summary'][:350]}" for s in stories
    ) or "(none)"
    recap_lines = "\n".join(
        f"- {e['company'] or e['ticker']}: {e['blurb'][:300]}"
        for e in earnings_notes
    ) or "(none)"
    upcoming_lines = "\n".join(
        f"- {e['date'].strftime('%a %b %d')} {e['timing']}: {e['ticker']} "
        f"({e['name']}){' [TRACKED]' if e.get('tracked') else ''}"
        + (f" — {previews[e['ticker'].upper()]['blurb'][:250]}"
           if e["ticker"].upper() in previews else "")
        for e in upcoming
    ) or "(none)"
    macro_lines = "\n".join(
        f"- {h['title']} — {h['publisher']}" for h in macro_heads
    ) or "(none)"

    prompt = f"""
You are writing the summary sections of a professional daily market
review dated {_report_date()}, sent after the U.S. close. All data is
today's close unless tagged "[last data ...]".

THE READER: {READER_PROFILE}
Frame takeaways around what matters to them, including what today means
for a Canadian investor holding U.S., Canadian and international ETFs.

INDICES:
{_fmt_rows(indices)}

SECTORS:
{_fmt_rows(sectors, label_key="label")}

FACTOR / STYLE ETFs:
{_fmt_rows(factors, label_key="label")}

TRACKED TICKERS:
{_fmt_rows(tickers)}

PORTFOLIO:
{_fmt_rows(portfolio)}
{port}

RATES AND CROSS-ASSET:
{_fmt_macro(macro)}

TODAY'S TOP STORIES:
{story_lines}

EARNINGS JUST REPORTED:
{recap_lines}

EARNINGS IN THE NEXT TWO BUSINESS DAYS:
{upcoming_lines}

MACRO HEADLINES:
{macro_lines}

Return JSON exactly in this shape:
{{
  "executive_summary": ["bullet", "bullet", "bullet", "bullet"],
  "investment_implications": {{
    "bull_case": ["point", "point", "point"],
    "bear_case": ["point", "point", "point"],
    "key_question": "one question"
  }},
  "looking_ahead": "3-5 sentences",
  "bottom_line": "2-4 sentences"
}}

RULES
- executive_summary: 4-5 bullets, each a meaningful takeaway across
  equities, rates, sectors, factors, earnings and cross-asset markets,
  not a list of numbers. Each bullet is a plain string.
- bull_case / bear_case: the 3 strongest arguments each way, analytical
  rather than repetitive. Each point is a plain string.
- key_question: one specific question to guide monitoring.
- looking_ahead: the most important upcoming earnings and macro items
  above and what to watch in each. Be concrete.
- bottom_line: was today constructive, mixed or defensive and why, and
  the single most important thing to monitor next.
- Use only the data above; don't invent facts.
"""
    parsed = _groq_call(prompt, max_tokens=3000, temperature=0.3,
                        label="overview")
    result = _parse_overview(parsed) if parsed else None

    def incomplete(r):
        return (r is None or not r["executive_summary"]
                or not r["investment_implications"]["bull_case"])

    if incomplete(result):
        print("Overview incomplete; retrying once.")
        parsed = _groq_call(prompt, max_tokens=3000, temperature=0.2,
                            label="overview retry")
        retry = _parse_overview(parsed) if parsed else None
        if retry and (result is None or not incomplete(retry)):
            result = retry

    return result or {
        "executive_summary": [],
        "investment_implications": {
            "bull_case": [], "bear_case": [], "key_question": "",
        },
        "looking_ahead": "",
        "bottom_line": "",
    }


def build_ai_summary(indices, sectors, factors, tickers, portfolio, macro,
                     market_news, lookahead, earnings_notes, previews):
    """
    Runs the analysis as several smaller requests instead of one large one,
    so a single oversized or malformed reply can't blank out the report.
    """
    ticker_notes = build_ticker_notes(tickers)
    stories = build_market_story_summaries(market_news)
    overview = build_overview(
        indices, sectors, factors, tickers, portfolio, macro, stories,
        earnings_notes, lookahead["earnings"], previews, lookahead["macro"],
    )
    return {"tickers": ticker_notes, "stories": stories, **overview}


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def html_escape(value):
    return escape(str(value), quote=True)


def pct_class(value):
    if value > 0:
        return "positive"
    if value < 0:
        return "negative"
    return "neutral"


def format_pct(value):
    return f"{value:+.2f}%"


STALE_MARK = "†"


def stale_footnote(rows):
    stale = [r for r in rows if r.get("stale")]
    if not stale:
        return ""
    dates = sorted({str(r["as_of"]) for r in stale})
    return (
        f'<p class="note">{STALE_MARK} No price for today yet; showing the '
        f"last available close ({', '.join(dates)}). Usually a local market "
        "holiday or a late data feed.</p>"
    )


def build_quote_table(rows, include_price=False):
    if not rows:
        return "<p>Unavailable.</p>"

    html = ['<table class="data-table"><thead><tr>'
            "<th>Ticker</th><th>Name</th>"]
    if include_price:
        html.append("<th>Price</th>")
    html.append("<th>Daily</th><th>YTD</th></tr></thead><tbody>")

    for row in rows:
        mark = STALE_MARK if row.get("stale") else ""
        html.append("<tr>")
        html.append(
            f'<td class="ticker">{html_escape(row["ticker"])}{mark}</td>'
        )
        html.append(f'<td class="name">{html_escape(row["name"])}</td>')
        if include_price:
            html.append(f'<td class="number">{row["price"]:.2f}</td>')
        html.append(
            f'<td class="number {pct_class(row["daily"])}">'
            f'{format_pct(row["daily"])}</td>'
            f'<td class="number {pct_class(row["ytd"])}">'
            f'{format_pct(row["ytd"])}</td>'
        )
        html.append("</tr>")

    html.append("</tbody></table>")
    return "".join(html) + stale_footnote(rows)


def build_labeled_table(rows, first_header="Category", second_header="Ticker"):
    if not rows:
        return "<p>Unavailable.</p>"

    html = [
        '<table class="data-table"><thead><tr>'
        f"<th>{html_escape(first_header)}</th>"
        f"<th>{html_escape(second_header)}</th>"
        "<th>Daily</th><th>YTD</th></tr></thead><tbody>"
    ]
    for row in rows:
        mark = STALE_MARK if row.get("stale") else ""
        html.append(
            "<tr>"
            f'<td class="name">{html_escape(row["label"])}</td>'
            f'<td class="ticker">{html_escape(row["ticker"])}{mark}</td>'
            f'<td class="number {pct_class(row["daily"])}">'
            f'{format_pct(row["daily"])}</td>'
            f'<td class="number {pct_class(row["ytd"])}">'
            f'{format_pct(row["ytd"])}</td>'
            "</tr>"
        )
    html.append("</tbody></table>")
    return "".join(html) + stale_footnote(rows)


def build_factor_tables(factors):
    return build_labeled_table(
        sorted(factors, key=lambda x: x["daily"], reverse=True)
    )


def build_macro_table(macro):
    html = ""
    today = market_today().isoformat()

    if macro.get("rates"):
        html += (
            "<h2>Treasury Rates</h2>"
            '<table class="data-table"><thead><tr>'
            "<th>Rate</th><th>Yield</th><th>Daily Change</th><th>As of</th>"
            "</tr></thead><tbody>"
        )
        for row in macro["rates"]:
            change = row["daily_bps"]
            as_of = str(row["as_of"])
            mark = STALE_MARK if as_of < today else ""
            value = (
                f"{row['value'] * 100:+.0f} bps" if row.get("is_spread")
                else f"{row['value']:.2f}%"
            )
            html += (
                "<tr>"
                f'<td class="name">{html_escape(row["label"])}</td>'
                f'<td class="number">{value}</td>'
                f'<td class="number {pct_class(change)}">{change:+.1f} bps</td>'
                f'<td class="number">{html_escape(as_of)}{mark}</td>'
                "</tr>"
            )
        html += "</tbody></table>"

    if macro.get("cross_asset"):
        html += (
            "<h2>Cross-Asset</h2>"
            '<table class="data-table"><thead><tr>'
            "<th>Asset</th><th>Value</th><th>Daily</th><th>YTD</th>"
            "</tr></thead><tbody>"
        )
        for row in macro["cross_asset"]:
            mark = STALE_MARK if row.get("stale") else ""
            html += (
                "<tr>"
                f'<td class="name">{html_escape(row["label"])}{mark}</td>'
                f'<td class="number">{row["value"]:.2f}</td>'
                f'<td class="number {pct_class(row["daily"])}">'
                f'{format_pct(row["daily"])}</td>'
                f'<td class="number {pct_class(row["ytd"])}">'
                f'{format_pct(row["ytd"])}</td>'
                "</tr>"
            )
        html += "</tbody></table>" + stale_footnote(macro["cross_asset"])

    return html or "<p>Unavailable.</p>"


def build_bullet_list(items):
    if not items:
        return "<p>Unavailable.</p>"
    return "<ul>" + "".join(
        f"<li>{html_escape(item)}</li>" for item in items
    ) + "</ul>"


def build_source_link(headline):
    publisher = html_escape(headline.get("publisher", "Unknown"))
    url = headline.get("url", "")
    if url:
        return (
            f'<p class="source">Source: '
            f'<a href="{html_escape(url)}">{publisher}</a></p>'
        )
    return f'<p class="source">Source: {publisher}</p>'


def build_paragraph(text):
    if not text:
        return "<p>Unavailable.</p>"
    return "".join(
        f"<p>{html_escape(p.strip())}</p>"
        for p in text.split("\n\n") if p.strip()
    )


def _cited(headlines, cites):
    return [
        headlines[i - 1] for i in cites
        if isinstance(i, int) and 1 <= i <= len(headlines)
    ]


def build_ticker_review(tickers, ai):
    ticker_ai = ai.get("tickers", {})
    html = ""
    for row in tickers:
        entry = ticker_ai.get(row["ticker"], {})
        if not isinstance(entry, dict):
            entry = {}
        note = str(entry.get("note", "") or "").strip()

        html += '<div class="ticker-review">'
        html += (
            f"<h2>{html_escape(row['ticker'])} — "
            f"{html_escape(row['name'])}</h2>"
        )
        if note:
            html += f"<p>{html_escape(note)}</p>"
        cited = _cited(row["headlines"], entry.get("cites") or [])
        if cited:
            html += "<h3>Sources</h3>"
            html += "".join(build_source_link(h) for h in cited)
        html += "</div>"
    return html or "<p>Unavailable.</p>"


def build_market_stories(market_news, ai):
    stories = ai.get("stories", [])
    if not stories:
        return "<p>Unavailable.</p>"
    html = ""
    for story in stories:
        title = str(story.get("title") or "").strip()
        if story.get("kind") == "opinion":
            title = _OPINION_TITLE.sub("", title).strip()
        title = html_escape(title)
        if story.get("kind") == "opinion":
            html += f"<h2>Opinion: {title}</h2>" if title else "<h2>Opinion</h2>"
        elif title:
            html += f"<h2>{title}</h2>"
        idx = story.get("source_index")
        if isinstance(idx, int) and 1 <= idx <= len(market_news):
            html += build_source_link(market_news[idx - 1])
        html += build_paragraph(str(story.get("summary") or "").strip())
    return html


def _pct_span(value, bold=False):
    if value is None:
        return "—"
    tag = "strong" if bold else "span"
    return f'<{tag} class="{pct_class(value)}">{format_pct(value)}</{tag}>'


def build_after_hours_box(c, summary=""):
    """
    Shaded box for one company: the move that reflects the earnings
    reaction as the headline (after-hours for companies that reported
    after today's close, regular session otherwise), a small price table,
    and the summary of the results underneath. If no summary is available,
    a plain-English note on how to read the prices is shown instead.
    """
    ah = c.get("after_hours")
    session = c.get("session")
    after_close = c.get("reported_after_today_close", False)

    if after_close:
        label, headline_value = "After-Hours Movement", ah
    else:
        label, headline_value = "Regular Session Movement", session
    headline = (_pct_span(headline_value) if headline_value is not None
                else '<span class="neutral">n/a</span>')
    html = '<div class="ah-box">'
    html += f'<p class="ah-title">{label}: {headline}</p>'

    if ah is not None:
        html += (
            '<table class="ah-table"><thead><tr>'
            "<th>Today's Close</th><th>After-Hours Price</th><th>Change</th>"
            "<th>Last Trade</th><th>Regular Session Today</th>"
            "</tr></thead><tbody><tr>"
            f'<td>{c["close"]:.2f}</td>'
            f'<td>{c["after_hours_price"]:.2f}</td>'
            f"<td>{_pct_span(ah, bold=after_close)}</td>"
            f'<td>{html_escape(fmt_clock(c.get("after_hours_time")))}</td>'
            f"<td>{_pct_span(session, bold=not after_close)}</td>"
            "</tr></tbody></table>"
        )

    thin = ("After-hours trading is thin, so the price can shift by "
            "tomorrow's open.")
    if after_close and ah is not None:
        note = ("Results came out after today's close, so this is the "
                "market's first reaction to them. The regular-session move "
                f"happened before the report. {thin}")
    elif after_close:
        note = ("Results came out after today's close, but no after-hours "
                "trades were available, so the market's first reaction will "
                "show at tomorrow's open.")
        if session is not None:
            note += (f" Regular session today: {format_pct(session)} "
                     "(before the report).")
    elif ah is not None:
        note = ("Results were already out during today's session, so the "
                "regular-session move is the main reaction. The after-hours "
                f"change reflects trading since the close. {thin}")
    elif session is not None:
        note = ("Results were already out during today's session, so the "
                f"regular-session move ({format_pct(session)}) is the main "
                "reaction. No after-hours trades were recorded since the "
                "close.")
    else:
        note = "Price data for today was unavailable."

    # The summary of the results sits inside the shaded box with the price
    # table; the note on how to read the prices always goes underneath it.
    summary = (summary or "").strip()
    if summary:
        html += f'<p class="blurb ah-summary">{html_escape(summary)}</p>'
    html += "</div>"
    html += f'<p class="ah-note ah-note-below">{html_escape(note)}</p>'
    return html


def build_earnings_recap(recent, earnings_notes):
    if not earnings_notes:
        if recent.get("companies"):
            return (
                "<p>Major companies reported, but no detailed results "
                "coverage was available in the sources reviewed.</p>"
            )
        return "<p>No major earnings reports since the last session.</p>"

    by_ticker = {c["ticker"]: c for c in recent.get("companies", [])}
    html = ""
    for e in earnings_notes:
        c = by_ticker.get(e["ticker"])
        name = (c["name"] if c else e["company"]) or e["ticker"]
        html += '<div class="ticker-review">'
        html += (
            f"<h2>{html_escape(name)} ({html_escape(e['ticker'])})</h2>"
        )
        if c:
            details = [f"Reported {c['date'].strftime('%b %d')}, "
                       f"{c['timing'].lower()}"]
            if c.get("eps_actual") and c.get("eps_forecast"):
                details.append(
                    f"EPS {c['eps_actual']} vs. {c['eps_forecast']} est."
                )
            elif c.get("eps_forecast"):
                details.append(f"EPS est. {c['eps_forecast']}")
            html += (
                f'<p class="note">{html_escape(" · ".join(details))}</p>'
            )
            html += build_after_hours_box(c, e.get("blurb"))
            headlines = c["headlines"]
        else:
            headlines = recent.get("general", [])
            html += f'<p class="blurb">{html_escape(e["blurb"])}</p>'
        cited = _cited(headlines, e["cites"])
        if cited:
            html += "".join(build_source_link(h) for h in cited)
        html += "</div>"
    return html


def build_upcoming_earnings(upcoming, previews):
    if not upcoming:
        return (
            "<p>No major earnings reports scheduled for the next two "
            "business days.</p>"
        )
    html = ""
    for e in upcoming:
        tracked = " — tracked holding" if e.get("tracked") else ""
        html += '<div class="ticker-review">'
        html += (
            f"<h3>{html_escape(e['name'])} ({html_escape(e['ticker'])})"
            f"{tracked}</h3>"
        )
        details = [f"{e['date'].strftime('%a %b %d')}, {e['timing'].lower()}"]
        if e.get("market_cap"):
            details.append(f"Mkt cap {format_cap(e['market_cap'])}")
        html += f'<p class="note">{html_escape(" · ".join(details))}</p>'

        preview = previews.get(e["ticker"].upper())
        if preview:
            html += f"<p>{html_escape(preview['blurb'])}</p>"
            cited = _cited(e.get("headlines", []), preview["cites"])
            html += "".join(build_source_link(h) for h in cited)
        else:
            html += "<p>Preview unavailable.</p>"
        html += "</div>"
    return html


# ---------------------------------------------------------------------------
# Email composition
# ---------------------------------------------------------------------------

EMAIL_CSS = """
body { font-family: "Times New Roman", Times, serif; font-size: 14px;
       line-height: 1.5; color: #000; margin: 0; padding: 20px;
       background: #fff; }
.container { max-width: 1000px; margin: 0 auto; }
h1 { font-family: "Times New Roman", Times, serif; font-size: 25px;
     margin-top: 34px; margin-bottom: 12px; padding-bottom: 7px;
     border-bottom: 1px solid #000; }
h2 { font-family: "Times New Roman", Times, serif; font-size: 19px;
     margin-top: 23px; margin-bottom: 8px; }
h3 { font-family: "Times New Roman", Times, serif; font-size: 16px;
     margin-top: 16px; margin-bottom: 6px; }
p { font-family: "Times New Roman", Times, serif; margin-top: 0;
    margin-bottom: 12px; }
ul { margin-top: 5px; margin-bottom: 16px; padding-left: 28px; }
li { font-family: "Times New Roman", Times, serif; margin-bottom: 5px; }
.date { font-size: 15px; margin-bottom: 27px; }
.data-table { width: 100%; border-collapse: collapse; margin-top: 8px;
              margin-bottom: 23px; table-layout: auto; }
.data-table th { text-align: left; font-weight: bold;
                 border-bottom: 2px solid #000; padding: 7px 10px;
                 white-space: nowrap;
                 font-family: "Times New Roman", Times, serif; }
.data-table td { padding: 6px 10px; border-bottom: 1px solid #ddd;
                 vertical-align: top;
                 font-family: "Times New Roman", Times, serif; }
.data-table .ticker { white-space: nowrap; font-weight: bold; }
.data-table .name { min-width: 260px; white-space: normal;
                    overflow-wrap: break-word; }
.data-table .number { text-align: right; white-space: nowrap;
                      font-variant-numeric: tabular-nums; }
.positive { color: #008000; }
.negative { color: #b00000; }
.neutral { color: #000; }
.ticker-review { margin-bottom: 26px; }
.source { font-size: 12px; margin-top: -4px; margin-bottom: 8px; }
.source a { color: #000; text-decoration: underline; }
.note { font-size: 12px; color: #444; margin-top: -4px; }
.ah-box { background: #f4f4f4; border-left: 4px solid #000;
          padding: 12px 16px; margin: 6px 0 16px 0; }
.ah-title { font-size: 17px; font-weight: bold; margin-bottom: 8px; }
.ah-table { border-collapse: collapse; margin-bottom: 10px; }
.ah-table th { text-align: left; font-size: 13px; font-weight: bold;
               border-bottom: 1px solid #000; padding: 3px 16px 4px 0;
               white-space: nowrap;
               font-family: "Times New Roman", Times, serif; }
.ah-table td { font-size: 16px; padding: 6px 16px 2px 0;
               white-space: nowrap; font-variant-numeric: tabular-nums;
               font-family: "Times New Roman", Times, serif; }
.ah-note { font-size: 12px; color: #444; margin: 0; }
.ah-summary { margin: 10px 0 0 0; }
.ah-note-below { margin: -10px 0 16px 0; }
.blurb { font-size: 16px; line-height: 1.6; color: #000;
         margin-bottom: 10px; }
.footer { margin-top: 38px; padding-top: 10px; border-top: 1px solid #999;
          font-size: 12px; color: #555; }
"""


def compose_email(indices, sectors, factors, tickers, portfolio,
                  index_portfolio, avantis_portfolio, macro, market_news,
                  lookahead, recent_earnings, earnings_notes, previews, ai):

    now = datetime.now(MARKET_TZ).strftime("%A, %B %d, %Y")
    impl = ai.get("investment_implications", {})

    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><style>{EMAIL_CSS}</style></head>
<body><div class="container">

<h1>{REPORT_TITLE}</h1>
<p class="date">{html_escape(now)}</p>

<h1>1. Executive Summary</h1>
{build_bullet_list(ai.get("executive_summary", []))}

<h1>2. Market Dashboard</h1>

<h2>Major Indices</h2>
{build_quote_table(indices)}

<h2>Portfolio Movements</h2>
"""

    portfolio_definitions = [
        ("Index Portfolio", index_portfolio, INDEX_PORTFOLIO_WEIGHTS),
        ("Avantis Portfolio", avantis_portfolio, AVANTIS_PORTFOLIO_WEIGHTS),
        ("Current Portfolio", portfolio, PORTFOLIO_WEIGHTS),
    ]
    for name, rows, weights in portfolio_definitions:
        daily = calculate_weighted_portfolio_return(rows, "daily", weights)
        ytd = calculate_weighted_portfolio_return(rows, "ytd", weights)
        html += f"<h3>{html_escape(name)}</h3>"
        if daily is not None and ytd is not None:
            html += (
                "<p><strong>Weighted Return:</strong> "
                f'<span class="{pct_class(daily)}">{format_pct(daily)}</span>'
                " today &nbsp;&nbsp;|&nbsp;&nbsp; YTD "
                f'<span class="{pct_class(ytd)}">{format_pct(ytd)}</span></p>'
            )
        else:
            html += (
                "<p>Weighted return unavailable; check portfolio weights "
                "and holdings data.</p>"
            )
        html += build_quote_table(rows, include_price=True)

    html += f"""
<h2>Sector Performance</h2>
{build_labeled_table(sorted(sectors, key=lambda x: x["daily"], reverse=True))}

<h2>Factor / Style Performance</h2>
{build_factor_tables(factors)}

<h2>Tracked Tickers</h2>
{build_quote_table(tickers, include_price=True)}

<h2>Company-by-Company Review</h2>
{build_ticker_review(tickers, ai)}

<h1>3. Rates, Macro &amp; Cross-Asset</h1>
{build_macro_table(macro)}

<h1>4. Top Market Stories</h1>
{build_market_stories(market_news, ai)}

<h1>5. Earnings Recap</h1>
{build_earnings_recap(recent_earnings, earnings_notes)}

<h1>6. Investment Implications</h1>
<h2>Bull Case</h2>
{build_bullet_list(impl.get("bull_case", []))}
<h2>Bear Case</h2>
{build_bullet_list(impl.get("bear_case", []))}
<h2>Key Question</h2>
{build_paragraph(impl.get("key_question", ""))}

<h1>7. Looking Ahead</h1>
<h2>Upcoming Earnings (Next Two Business Days)</h2>
{build_upcoming_earnings(lookahead["earnings"], previews)}
"""

    if lookahead["macro"]:
        html += "<h2>Macro Developments to Monitor</h2><ul>"
        for h in lookahead["macro"]:
            html += (
                f"<li>{html_escape(h['title'])} — "
                f"{html_escape(h['publisher'])}</li>"
            )
        html += "</ul>"

    html += f"""
<h2>Forward View</h2>
{build_paragraph(ai.get("looking_ahead", ""))}

<h1>8. Bottom Line</h1>
{build_paragraph(ai.get("bottom_line", ""))}

<div class="footer">
Generated automatically via Yahoo Finance, Tavily, U.S. Treasury, Nasdaq,
FRED &amp; Groq
</div>

</div></body></html>
"""
    return html


# ---------------------------------------------------------------------------
# Email sending
# ---------------------------------------------------------------------------

def send_email(subject, html_body, plain_text_body=None):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config.EMAIL_SENDER
    msg["To"] = ", ".join(config.EMAIL_RECIPIENT)

    if not plain_text_body:
        plain_text_body = "Please view this email in an HTML-capable email client."

    msg.attach(MIMEText(plain_text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(config.EMAIL_SENDER, config.EMAIL_PASSWORD)
        server.sendmail(
            config.EMAIL_SENDER, config.EMAIL_RECIPIENT, msg.as_string()
        )


# ---------------------------------------------------------------------------
# Main workflow
# ---------------------------------------------------------------------------

def main():
    if not has_fresh_data("^GSPC"):
        print("No fresh market data for today — exiting.")
        return

    if already_ran_today():
        print("Already ran today — exiting.")
        return

    print("Gathering indices...")
    indices = gather_group(config.INDICES)

    print("Gathering tracked tickers...")
    tickers = gather_group(config.TICKERS, with_headlines=True)

    print("Gathering portfolios...")
    portfolio = gather_group(config.PORTFOLIO)
    index_portfolio = gather_group(INDEX_PORTFOLIO)
    avantis_portfolio = gather_group(AVANTIS_PORTFOLIO)

    print("Gathering sectors and factors...")
    sectors = gather_sectors(SECTOR_ETFS)
    factors = gather_sectors(FACTOR_ETFS)

    print("Gathering macro data...")
    macro = gather_macro_data()

    print("Gathering market headlines...")
    market_news = gather_market_news()

    print("Gathering recent earnings...")
    recent_earnings = gather_recent_earnings()

    print("Gathering lookahead...")
    lookahead = gather_lookahead()

    if not (indices or tickers or portfolio or index_portfolio
            or avantis_portfolio):
        print("No data gathered — exiting without email.")
        return

    print("Building earnings recap...")
    earnings_notes = build_earnings_summary(recent_earnings)

    print("Building upcoming earnings previews...")
    previews = build_earnings_previews(lookahead["earnings"])

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
        earnings_notes=earnings_notes,
        previews=previews,
    )

    subject = f"{REPORT_TITLE} — {market_today().strftime('%a, %b %d %Y')}"

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
        recent_earnings=recent_earnings,
        earnings_notes=earnings_notes,
        previews=previews,
        ai=ai,
    )

    send_email(subject=subject, html_body=html_body)
    print(f"{REPORT_TITLE} email sent.")
    mark_run_success()


if __name__ == "__main__":
    main()
