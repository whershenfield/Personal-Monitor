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

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

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
DEFAULT_FACTOR_ETFS = {
    "Growth": "VUG",
    "Momentum": "MTUM",
    "Quality": "QUAL",
    "Value": "VTV",
    "Minimum Volatility": "USMV",
    "Small Cap": "IWM",
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


# Date helpers

def market_today():
    """Return the current calendar date in New York time."""
    return datetime.now(MARKET_TZ).date()


def next_day(d):
    return d + timedelta(days=1)


# Fresh-data and duplicate-run checks

def has_fresh_data(ticker):
    """Check whether Yahoo Finance has a daily observation for today."""

    try:
        hist = yf.Ticker(ticker).history(
            period="5d"
        )

        if hist.empty:
            return False

        latest_date = hist.index[-1].date()

        return latest_date == market_today()

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

def get_quote_and_ytd(ticker):
    """
    Return current close, daily percentage change, and YTD percentage change.
    Uses historical closes so after-hours movement does not distort returns.
    """

    try:
        stock = yf.Ticker(ticker)

        start_date = date(
            market_today().year,
            1,
            1,
        )

        end_date = next_day(
            market_today()
        )

        hist = stock.history(
            start=start_date.isoformat(),
            end=end_date.isoformat(),
            auto_adjust=False,
        )

        if hist.empty:
            return None, None, None

        hist = hist.dropna(
            subset=["Close"]
        )

        if len(hist) < 2:
            return None, None, None

        current = float(
            hist["Close"].iloc[-1]
        )

        prev_close = float(
            hist["Close"].iloc[-2]
        )

        ytd_start = float(
            hist["Close"].iloc[0]
        )

        if prev_close == 0:
            return None, None, None

        if ytd_start == 0:
            return None, None, None

        daily_pct = (
            (current - prev_close)
            / prev_close
        ) * 100

        ytd_pct = (
            (current - ytd_start)
            / ytd_start
        ) * 100

        return (
            current,
            daily_pct,
            ytd_pct,
        )

    except Exception as e:
        print(
            f"Error fetching {ticker}: {e}"
        )
        return None, None, None


@lru_cache(maxsize=None)
def company_name(ticker):
    """Return a display name without using Yahoo's fundamentals endpoint."""

    if ticker in TICKER_NAMES:
        return TICKER_NAMES[ticker]

    return ticker

# News and market-group helpers

def get_headlines(ticker, limit=5):
    """Return deduplicated Yahoo Finance headlines for a ticker."""

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
            }
        )

        if len(out) >= limit:
            break

    return out


def gather_group(
    tickers,
    with_headlines=False,
    headline_limit=5,
):
    """
    Gather quote/performance data for a group of tickers.

    Returns rows in the format expected by the HTML tables
    and AI analysis.
    """

    rows = []

    for ticker in tickers:

        current, daily, ytd = (
            get_quote_and_ytd(
                ticker
            )
        )

        if current is None:
            print(
                f"Could not fetch data for {ticker}"
            )
            continue

        row = {
            "ticker": ticker,
            "name": company_name(ticker),
            "price": current,
            "daily": daily,
            "ytd": ytd,
            "headlines": [],
        }

if with_headlines:

    row["headlines"] = get_headlines(
        ticker,
        limit=headline_limit,
    )

    # Supplement Yahoo's feed when it returns too little coverage.
    if len(row["headlines"]) < headline_limit:
        extra = search_company_news(
            ticker,
            limit=headline_limit,
        )

        seen_titles = {
            h["title"].strip().lower()
            for h in row["headlines"]
        }

        for headline in extra:
            key = headline["title"].strip().lower()

            if key not in seen_titles:
                row["headlines"].append(headline)
                seen_titles.add(key)

            if len(row["headlines"]) >= headline_limit:
                break

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

    today = market_today()

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

    results = []
    seen_urls = set()
    seen_titles = set()

    for query in queries:

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
                    "days": 2,
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

            results.append(
                {
                    "title": title,
                    "publisher": (
                        result.get(
                            "source",
                            ""
                        )
                        or "Web source"
                    ),
                    "url": url,
                    "snippet": content,
                }
            )

    # Keep the strongest first results while preventing one query
    # from dominating the entire list.
    results = results[:limit]

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

def search_company_news(ticker, limit=5):
    """Search broadly for company-specific news using Tavily."""

    api_key = getattr(
        config,
        "TAVILY_API_KEY",
        "",
    )

    if not api_key:
        return []

    name = company_name(ticker)

    try:
        response = requests.post(
            "https://api.tavily.com/search",
            headers={
                "Content-Type": "application/json",
            },
            json={
                "api_key": api_key,
                "query": (
                    f'"{name}" {ticker} company stock news'
                ),
                "topic": "news",
                "search_depth": "advanced",
                "days": 7,
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

        key = title.lower()

        if key in seen:
            continue

        seen.add(key)

        headlines.append({
            "title": title,
            "publisher": (
                result.get("source")
                or "Web source"
            ),
            "url": url,
            "snippet": str(
                result.get("content", "") or ""
            ).strip(),
        })

    return headlines

def gather_labeled_etfs(mapping):
    """Gather performance for sector and factor ETF proxies."""

    rows = []

    for label, ticker in mapping.items():

        current, daily, ytd = (
            get_quote_and_ytd(
                ticker
            )
        )

        if current is None:
            print(
                f"Could not fetch ETF data "
                f"for {label} ({ticker})"
            )
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

# Portfolio calculations

def calculate_weighted_portfolio_return(
    rows,
    field,
):
    """Calculate a weighted portfolio return when weights are available."""

    if not PORTFOLIO_WEIGHTS:
        return None

    weighted_sum = 0.0
    total_weight = 0.0

    for row in rows:

        ticker = row["ticker"]
        value = row.get(field)
        weight = (
            PORTFOLIO_WEIGHTS.get(
                ticker
            )
        )

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


# FRED macro data

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
        "2Y Treasury": "DGS2",
        "10Y Treasury": "DGS10",
        "30Y Treasury": "DGS30",
    }

    for label, series_id in (
        treasury_series.items()
    ):

        result = get_fred_series(
            series_id
        )

        if not result:
            continue

        previous, latest = result

        change_bps = (
            latest["value"]
            - previous["value"]
        ) * 100

        data["rates"].append(
            {
                "label": label,
                "value": latest["value"],
                "daily_bps": change_bps,
                "as_of": latest["date"],
            }
        )

    # Cross-asset markets

    cross_asset_map = {
        "DXY": "DX-Y.NYB",
        "Gold": "GC=F",
        "WTI Crude": "CL=F",
        "VIX": "^VIX",
    }

    for label, ticker in (
        cross_asset_map.items()
    ):

        current, daily, ytd = (
            get_quote_and_ytd(
                ticker
            )
        )

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

    today = market_today()

    horizon = (
        today
        + timedelta(days=7)
    )

    # Earnings

    tracked_for_earnings = list(
        dict.fromkeys(
            config.TICKERS
            + config.PORTFOLIO
        )
    )

    for ticker in tracked_for_earnings:

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
                    <= earnings_date
                    <= horizon
                ):

                    lookahead[
                        "earnings"
                    ].append(
                        {
                            "ticker":
                                ticker,
                            "name":
                                company_name(
                                    ticker
                                ),
                            "date":
                                earnings_date.isoformat(),
                        }
                    )

                    break

        except Exception as e:
            print(
                f"Calendar check failed "
                f"for {ticker}: {e}"
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
    """Make a Groq API call and return the parsed JSON response."""

    groq_url = (
        "https://api.groq.com/openai/v1/"
        "chat/completions"
    )

    headers = {
        "Authorization": (
            f"Bearer {config.GROQ_API_KEY}"
        ),
        "Content-Type": "application/json",
    }

    payload = {
        "model": (
            "openai/gpt-oss-120b"
        ),
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

        response = requests.post(
            groq_url,
            headers=headers,
            data=json.dumps(payload),
            timeout=120,
        )

    except Exception as e:
        print(
            f"Groq request exception: {e}"
        )
        return None

    print(
        f"Groq HTTP status: "
        f"{response.status_code}"
    )

    if response.status_code != 200:

        print(
            "Groq error body: "
            f"{response.text[:1000]}"
        )

        return None

    try:

        body = response.json()

    except Exception as e:

        print(
            "Groq non-JSON body: "
            f"{response.text[:500]} "
            f"({e})"
        )

        return None

    if (
        "choices" not in body
        or not body["choices"]
    ):

        print(
            "Groq response missing "
            f"'choices': {json.dumps(body)[:1000]}"
        )

        return None

    raw = (
        body["choices"][0]
        ["message"]
        ["content"]
        .strip()
    )

    # Remove markdown code fences if present.

    if raw.startswith("```"):

        lines = raw.splitlines()

        if (
            lines
            and
            lines[0]
            .strip()
            .startswith("```")
        ):
            lines = lines[1:]

        if (
            lines
            and
            lines[-1]
            .strip()
            == "```"
        ):
            lines = lines[:-1]

        raw = "\n".join(
            lines
        ).strip()

    # Extract the JSON object from the response.

    first_brace = raw.find(
        "{"
    )

    last_brace = raw.rfind(
        "}"
    )

    if (
        first_brace != -1
        and
        last_brace != -1
    ):

        raw = raw[
            first_brace:
            last_brace + 1
        ]

    try:

        return json.loads(
            raw
        )

    except json.JSONDecodeError as e:

        print(
            f"Groq JSON parse failed: {e}"
        )

        print(
            "Raw content: "
            f"{raw[:2000]}"
        )

        return None


# AI analysis

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

    # Prompt formatting helpers

    def fmt_indices(rows):

        return "\n".join(
            f"{r['ticker']} "
            f"({r['name']}) | "
            f"daily "
            f"{r['daily']:+.2f}% | "
            f"YTD "
            f"{r['ytd']:+.2f}%"
            for r in rows
        )

    def fmt_sectors(rows):

        return "\n".join(
            f"{r['label']} "
            f"({r['ticker']}) | "
            f"daily "
            f"{r['daily']:+.2f}% | "
            f"YTD "
            f"{r['ytd']:+.2f}%"
            for r in rows
        )

    def fmt_factors(rows):

        return "\n".join(
            f"{r['label']} "
            f"({r['ticker']}) | "
            f"daily "
            f"{r['daily']:+.2f}% | "
            f"YTD "
            f"{r['ytd']:+.2f}%"
            for r in rows
        )

    def fmt_tickers(rows):

        lines = []

        for r in rows:

            lines.append(
                f"\n{r['ticker']} "
                f"({r['name']}) | "
                f"daily "
                f"{r['daily']:+.2f}% | "
                f"YTD "
                f"{r['ytd']:+.2f}%"
            )

            if r["headlines"]:

                for i, headline in enumerate(
                    r["headlines"],
                    start=1,
                ):

                    lines.append(
                        f"  [{i}] "
                        f"{headline['title']} "
                        f"— {headline['publisher']}"
                    )

            else:

                lines.append(
                    "  (no company-specific "
                    "headlines available)"
                )

        return "\n".join(lines)

    def fmt_portfolio(rows):

        return "\n".join(
            f"{r['ticker']} "
            f"({r['name']}) | "
            f"daily "
            f"{r['daily']:+.2f}% | "
            f"YTD "
            f"{r['ytd']:+.2f}%"
            for r in rows
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
                    f"daily "
                    f"{r['daily']:+.2f}% | "
                    f"YTD "
                    f"{r['ytd']:+.2f}%"
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
                f"{h['publisher']}"
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


    def fmt_lookahead(lookahead_data):

        lines = []

        if lookahead_data[
            "earnings"
        ]:

            lines.append(
                "UPCOMING EARNINGS"
            )

            for earnings in sorted(
                lookahead_data[
                    "earnings"
                ],
                key=lambda x: x["date"],
            ):

                lines.append(
                    f"{earnings['date']}: "
                    f"{earnings['ticker']} "
                    f"({earnings['name']})"
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
You are writing a professional daily investment review for someone
building experience for a career in wealth management and asset management.

The report should resemble a concise internal investment note:
data first, analysis second, implications third.

Respond with ONE JSON OBJECT ONLY.
Do not include markdown outside the JSON.

==================================================
MARKET DATA
==================================================

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

==================================================
MACRO / CROSS-ASSET
==================================================

{fmt_macro(macro)}

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
  explanations.- Do not fabricate company events, facts or sources.
- Do not spend the entire note repeating the daily percentage move.

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
- Mention specific upcoming tracked-company earnings where available.
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
    if value > 0:
        return "positive"

    if value < 0:
        return "negative"

    return "neutral"


def format_pct(value):
    return f"{value:+.2f}%"


def build_quote_table(
    rows,
    include_price=False,
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
        "<th>YTD</th>"
        "</tr>"
        "</thead>"
    )

    html.append(
        "<tbody>"
    )

    for row in rows:

        daily = row["daily"]
        ytd = row["ytd"]

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
            f"{html_escape(row['name'])}"
            f"</td>"
        )

        if include_price:

            html.append(
                f"<td class=\"number\">"
                f"{row['price']:.2f}"
                f"</td>"
            )

        html.append(
            f"<td class=\"number "
            f"{pct_class(daily)}\">"
            f"{format_pct(daily)}"
            f"</td>"
        )

        html.append(
            f"<td class=\"number "
            f"{pct_class(ytd)}\">"
            f"{format_pct(ytd)}"
            f"</td>"
        )

        html.append(
            "</tr>"
        )

    html.append(
        "</tbody>"
        "</table>"
    )

    return "".join(html)


def build_labeled_table(rows):
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
        "</tr>"
        "</thead>"
        "<tbody>"
    )

    for row in rows:

        daily = row["daily"]
        ytd = row["ytd"]

        html.append(
            "<tr>"
            f"<td class=\"name\">"
            f"{html_escape(row['label'])}"
            f"</td>"
            f"<td class=\"ticker\">"
            f"{html_escape(row['ticker'])}"
            f"</td>"
            f"<td class=\"number "
            f"{pct_class(daily)}\">"
            f"{format_pct(daily)}"
            f"</td>"
            f"<td class=\"number "
            f"{pct_class(ytd)}\">"
            f"{format_pct(ytd)}"
            f"</td>"
            "</tr>"
        )

    html.append(
        "</tbody>"
        "</table>"
    )

    return "".join(html)


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
                f"<td class=\"number "
                f"{pct_class(daily)}\">"
                f"{format_pct(daily)}"
                f"</td>"
                f"<td class=\"number "
                f"{pct_class(ytd)}\">"
                f"{format_pct(ytd)}"
                f"</td>"
                "</tr>"
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
                for p in summary.split(
                    "\n\n"
                )
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


# Email composition

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
    ).strftime(
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

<h1>Daily Investment Review</h1>

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


<h2>Sector Performance</h2>

{build_labeled_table(
    sorted(
        sectors,
        key=lambda x: x["daily"],
        reverse=True,
    )
)}


<h2>Factor / Style Performance</h2>

{build_labeled_table(
    sorted(
        factors,
        key=lambda x: x["daily"],
        reverse=True,
    )
)}


<h2>Tracked Tickers</h2>

{build_quote_table(
    tickers,
    include_price=True,
)}

<h2>Company-by-Company Review</h2>

{build_ticker_review(
    tickers,
    ai
)}


<h2>Portfolio Movements</h2>
"""

    # Add weighted portfolio performance when available.

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

        html += f"""
<p>
<strong>Weighted Portfolio:</strong>
<span class="{pct_class(portfolio_daily)}">
{format_pct(portfolio_daily)}
</span>
today
&nbsp;&nbsp;|&nbsp;&nbsp;
YTD
<span class="{pct_class(portfolio_ytd)}">
{format_pct(portfolio_ytd)}
</span>
</p>
"""

    html += f"""

{build_quote_table(
    portfolio,
    include_price=True,
)}


<!-- 3. Rates, Macro & Cross-Asset -->

<h1>3. Rates, Macro &amp; Cross-Asset</h1>

{build_macro_table(
    macro
)}



<!-- 4. Top Market Stories -->

<h1>4. Top Market Stories</h1>

{build_market_stories(
    market_news,
    ai
)}


<!-- 5. Investment Implications -->

<h1>5. Investment Implications</h1>


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


<!-- 6. Looking Ahead -->

<h1>6. Looking Ahead</h1>


<h2>Upcoming Earnings</h2>
"""

    if lookahead[
        "earnings"
    ]:

        html += "<ul>"

        for earnings in sorted(
            lookahead["earnings"],
            key=lambda x: x["date"],
        ):

            html += (
                "<li>"
                f"<strong>"
                f"{html_escape(earnings['date'])}"
                f"</strong>: "
                f"{html_escape(earnings['ticker'])} "
                f"("
                f"{html_escape(earnings['name'])}"
                f")"
                "</li>"
            )

        html += "</ul>"

    else:

        html += (
            "<p>"
            "No upcoming tracked-company "
            "earnings found."
            "</p>"
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


<!-- 7. Bottom Line -->

<h1>7. Bottom Line</h1>

{build_paragraph(
    ai.get(
        "bottom_line",
        ""
    )
)}


<div class="footer">
Generated automatically via
Yahoo Finance, Tavily, FRED &amp; Groq
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

    # Confirm fresh market data.

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
    )

    print(
        "Gathering portfolio..."
    )

    portfolio = gather_group(
        config.PORTFOLIO,
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
        FACTOR_ETFS
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
    ):

        print(
            "No data gathered — "
            "exiting without email."
        )

        return

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
    )

    # Build and send the email.

    today_str = (
        market_today()
        .strftime(
            "%a, %b %d %Y"
        )
    )

    subject = (
        "Daily Investment Review — "
        f"{today_str}"
    )

    html_body = compose_email(
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
        subject=subject,
        html_body=html_body,
    )

    print(
        "Daily investment review "
        "email sent."
    )

    # Record successful completion.

    mark_run_success()


if __name__ == "__main__":
    main()
