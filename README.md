# Daily Market Review

An automated end-of-day market report, delivered by email after the U.S. close. It pulls prices, rates, news and earnings data, uses an AI model to write the commentary, and sends a formatted review of the day's markets.

## What's in the report

1. **Executive Summary**: the day's key takeaways across equities, rates, sectors, factors and earnings.
2. **Market Dashboard**: major indices, weighted returns for model portfolios, sector and factor/style performance, tracked stocks, and a company-by-company review that explains each move relative to its market and sector.
3. **Rates, Macro & Cross-Asset**: 2Y, 10Y and 30Y Treasury yields, the 2s10s spread, the dollar, gold, Brent crude and the VIX.
4. **Top Market Stories**: four of the day's most important non-earnings stories plus one opinion piece, each with a source link.
5. **Earnings Recap**: the largest companies that just reported, with results versus expectations and the stock's reaction, including after-hours moves for after-close reporters.
6. **Investment Implications**: bull case, bear case and a key question to monitor.
7. **Looking Ahead**: previews of major earnings in the next two business days, macro developments to watch, and a forward view on the broader market outlook.
8. **Bottom Line**: whether the day was constructive, mixed or defensive, and what matters next.

## How it works

The script runs on GitHub Actions. Each run, it:

- Checks that today's market data is available, so nothing goes out on holidays or before the close.
- Checks a small GitHub Gist to see whether the report has already been sent today. This means it's safe to trigger more than once a day.
- Gathers data, writes the analysis in several separate AI requests, builds the email and sends it.
- Records the run in the Gist.

**Data sources**

| Source | Used for |
| --- | --- |
| Yahoo Finance (via `yfinance`) | Prices and company headlines |
| U.S. Treasury | Daily yield curve |
| FRED | Fallback for Treasury yields |
| Nasdaq earnings calendar | Recent and upcoming earnings |
| Tavily | News search |
| Groq (`openai/gpt-oss-120b`) | Written analysis |

## Setup

### 1. Get your API keys and credentials

- **Groq API key**: from [console.groq.com](https://console.groq.com).
- **Tavily API key**: from [tavily.com](https://tavily.com).
- **Gmail app password**: for the sending account. This requires 2-step verification on that account. Do not use your regular Gmail password.
- **GitHub Gist**:
  1. Create a *secret* Gist containing one file named `run_state.json` with the contents `{}`.
  2. Note the Gist ID, which is the long string at the end of its URL.
  3. Create a GitHub personal access token with only the `gist` permission.

### 2. Add them as repository secrets

In your repo, go to **Settings → Secrets and variables → Actions** and add:

| Secret | Value |
| --- | --- |
| `GROQ_API_KEY` | Your Groq key |
| `TAVILY_API_KEY` | Your Tavily key |
| `EMAIL_SENDER` | The Gmail address that sends the report |
| `EMAIL_PASSWORD` | The Gmail app password |
| `EMAIL_RECIPIENT` | Recipient address(es), comma-separated |
| `GIST_ID` | Your Gist ID |
| `GIST_TOKEN_GITHUB` | Your Gist-only access token |

**Never put real keys or passwords in `config.py`.** Leave those fields blank and let the workflow supply them from secrets.

### 3. Run it

Go to the **Actions** tab, select **Stock Monitor**, and click **Run workflow**.

To run it automatically, add a schedule to `.github/workflows/monitor.yml`. For example, this runs at 4:45 PM and 5:30 PM New York time, Monday to Friday:

```yaml
on:
  workflow_dispatch:
  schedule:
    - cron: "45 20,21 * * 1-5"
    - cron: "30 21,22 * * 1-5"
```

GitHub's schedule uses UTC and can run late, so the lines above cover both daylight and standard time. The once-a-day check prevents duplicate emails.

## Customizing

Edit `config.py` to change what the report covers:

- `INDICES`: indices shown in the dashboard.
- `TICKERS`: individual stocks that get a company-by-company review.
- `PORTFOLIO`, `INDEX_PORTFOLIO` and `AVANTIS_PORTFOLIO`: the holdings in each model portfolio.
- `PORTFOLIO_WEIGHTS`, `INDEX_PORTFOLIO_WEIGHTS` and `AVANTIS_PORTFOLIO_WEIGHTS`: the weight of each holding in its portfolio.

You can also add these optional settings to `config.py`:

| Setting | Use |
| --- | --- |
| `SECTOR_ETFS` / `FACTOR_ETFS` | Replace the default sector and factor ETF proxies |
| `TICKER_NAMES` | Set display names, e.g. `{"EL.PA": "EssilorLuxottica"}` |
| `TICKER_KEYWORDS` | Words a headline must contain to count as news about a company |
| `TICKER_BENCHMARKS` | The benchmark(s) a stock is compared against |
| `RECENT_EARNINGS_MIN_CAP` | Minimum market cap for the earnings recap |
| `UPCOMING_EARNINGS_MIN_CAP` | Minimum market cap for upcoming earnings |
| `UPCOMING_EARNINGS_DAYS` | How many business days ahead to look for earnings |

## Running locally

```bash
pip install requests pytz yfinance
export GROQ_API_KEY=...        # and the other variables listed above
python monitor.py
```

## Limitations

- **Unofficial data sources.** Yahoo Finance and the Nasdaq earnings calendar are not official public APIs. They can change or break without notice, and individual data points are sometimes late or missing. Late prices are marked with a † in the report.
- **AI-generated commentary.** The model is instructed to use only the supplied data and sources, but it can still misstate facts or draw weak conclusions. Check anything important against the original sources.
- **After-hours moves are preliminary.** Volume after the close is thin, so these prices can change significantly by the next open.

## Disclaimer

This is a personal project for learning and market monitoring. Nothing it produces is investment advice or a recommendation to buy or sell any security. All data is provided as-is, with no guarantee of accuracy or completeness.
