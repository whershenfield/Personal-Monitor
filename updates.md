# Daily Investment Review: How to Make Changes

Each trading day, GitHub Actions runs `monitor.py`. It pulls prices from Yahoo Finance, Treasury yields from FRED, and news from Tavily. It then has Groq write the commentary and emails the finished report through Gmail.

Almost every routine change happens in **`config.py`**. You only need to touch `monitor.py` to change the report's layout, the AI instructions, or the news searches (see the last section).

To edit a file on GitHub, open it, click the pencil icon, make your change, and click **Commit changes**. The next scheduled run picks it up automatically.

---

## Ticker symbols: getting them right

Use the symbol exactly as it appears on finance.yahoo.com. Exchange suffixes matter:

- Toronto (TSX): ends in `.TO` (e.g. `VCN.TO`)
- Paris: ends in `.PA` (e.g. `EL.PA`)
- London: ends in `.L`
- US stocks and ETFs: no suffix (e.g. `SPGI`)
- Indices start with `^` (e.g. `^GSPC` for the S&P 500)

If a symbol is wrong, the report doesn't fail. It just quietly leaves that ticker out. The run log will show `Skipping XYZ: no quote data available`.

Formatting rules in `config.py`: each symbol goes in double quotes, separated by commas, inside square brackets.

---

## Adding or removing a tracked stock

Add or delete the symbol in the `TICKERS` list:

```python
TICKERS = ["EL.PA", "SPGI", "MSFT"]
```

Each tracked stock gets a row in the Tracked Tickers table, a company-by-company write-up with news sources, and an earnings-date check in Looking Ahead.

**Also add a display name.** The script uses the company name to search for news. Without one, the report shows only the symbol and the news search is noticeably weaker. `config.py` doesn't have a names list yet, so the first time you need one, add this anywhere in the file:

```python
TICKER_NAMES = {
    "MSFT": "Microsoft",
}
```

Add more entries on new lines inside the braces. The names for everything currently in the report (EssilorLuxottica, S&P Global, the indices and all the portfolio ETFs) are already built in, so they don't need to be added. Anything you put in `TICKER_NAMES` overrides the built-in name.

When you remove a stock, its leftover `TICKER_NAMES` entry does no harm.

---

## Changing the model portfolios

The report shows three portfolios, in this order:

| Name in report    | Holdings list in config.py | Weights in config.py          |
|-------------------|----------------------------|-------------------------------|
| Index Portfolio   | `INDEX_PORTFOLIO`          | `INDEX_PORTFOLIO_WEIGHTS`     |
| Avantis Portfolio | `AVANTIS_PORTFOLIO`        | `AVANTIS_PORTFOLIO_WEIGHTS`   |
| Current Portfolio | `PORTFOLIO`                | `PORTFOLIO_WEIGHTS`           |

When changing a holding, **update both the holdings list and the weights**:

```python
PORTFOLIO = ["CAUS.TO", "CALV.TO", "VCN.TO", "CADE.TO"]
PORTFOLIO_WEIGHTS = {"CAUS.TO": 0.35, "CALV.TO": 0.15, "VCN.TO": 0.20, "CADE.TO": 0.30}
```

Rules for weights:

- Write them as decimals (0.35, not 35%).
- They should add up to 1.00.
- A holding with no weight still appears in the table but is **left out of the weighted return**, which will skew the portfolio's number without any warning.
- If you add a new ETF, add its name to `TICKER_NAMES` as well, or the Name column will just repeat the symbol.

Only the **Current Portfolio** feeds into the AI commentary and the upcoming-earnings check. The Index and Avantis portfolios appear in the dashboard only.

---

## Changing the major indices

Edit the `INDICES` list. Same formatting rules as `TICKERS`.

---

## Changing the sector or factor ETFs

By default the report uses the 11 SPDR sector ETFs (XLK, XLF, etc.) and six style ETFs (Growth, Momentum, Quality, Value, Minimum Volatility, Small Cap). To use a different set, add `SECTOR_ETFS` or `FACTOR_ETFS` to `config.py`. This **replaces** the default list, so include every row you want shown:

```python
FACTOR_ETFS = {
    "Growth": "VUG",
    "Value": "VTV",
    "Dividend": "SCHD",
}
```

The text on the left is the label shown in the report; the symbol on the right is the ETF.

---

## Adding or removing an email recipient

Both `config.py` and the GitHub secret need to change. When the report runs on GitHub, the secret takes priority, so editing `config.py` alone does nothing.

1. Update the `EMAIL_RECIPIENT` list in `config.py`:
   ```python
   EMAIL_RECIPIENT = ["first@example.com", "second@example.com"]
   ```
2. On GitHub, go to the repository's **Settings → Secrets and variables → Actions** (under Security in the left sidebar). Under Repository secrets, click the pencil icon next to `EMAIL_RECIPIENT`.
3. Enter every address as one comma-separated line, with **no quotes, no brackets, and no spaces after the commas**:
   `first@example.com,second@example.com`

   A space after a comma becomes part of the next address, and that email won't be delivered.

Tip: write the full list in `config.py` first, then copy it into the secret and remove the quotes, spaces and brackets.

---

## API keys and passwords

Real keys should live **only in GitHub Secrets**. Leave the values in `config.py` blank (`""`) so they aren't visible to anyone who can see the repository.

| GitHub secret name  | What it is                                    | Where to get a new one |
|---------------------|-----------------------------------------------|------------------------|
| `GROQ_API_KEY`      | Writes the AI commentary                      | console.groq.com → API Keys |
| `TAVILY_API_KEY`    | Searches for market and company news          | app.tavily.com |
| `GIST_ID`           | The Gist that remembers the last send date    | The string at the end of the Gist's URL |
| `GIST_TOKEN_GITHUB` | Lets the script update that Gist              | GitHub → Settings → Developer settings → Personal access tokens (needs the "gist" permission) |
| `EMAIL_SENDER`      | Gmail address that sends the report           | — |
| `EMAIL_PASSWORD`    | Gmail **app password** (not the normal login) | Google Account → Security → App passwords (requires 2-Step Verification) |
| `EMAIL_RECIPIENT`   | Who receives the report                       | See section above |

Note the Gist token's secret is named `GIST_TOKEN_GITHUB`, not `GIST_TOKEN`.

To replace a key, generate a new one with the provider, then edit the matching secret on GitHub. The workflow file in the `.github/workflows` folder passes these secrets to the script, so if a secret is ever renamed, that file has to be updated too.

---

## How a run decides whether to send

Every run makes two checks before doing anything:

1. **Is there fresh market data?** If Yahoo Finance has no S&P 500 data for today (New York date), the run stops. This is why no email arrives on weekends or US market holidays.
2. **Has it already sent today?** After a successful send, the script writes today's date into a file called `run_state.json` in the GitHub Gist. Any later run that day stops.

The date is only saved *after* the email goes out, so if a run fails partway, the next scheduled run will try again.

**To force a second send on the same day:** open the Gist, edit `run_state.json`, change the date to any earlier date, save, then re-run the workflow from the Actions tab.

**Canadian holidays:** on days when US markets are open but the TSX is closed, the report still sends, but the TSX-listed ETFs will repeat the previous day's move as their daily change.

---

## Troubleshooting

If an email doesn't arrive or looks incomplete, open the **Actions** tab, click the latest run, and read the log. Common messages:

- **`No fresh market data for today — exiting.`** Weekend, US holiday, or Yahoo hadn't updated yet. Expected behaviour.
- **`Already ran today — exiting.`** The email already went out today. See "force a second send" above.
- **`Skipping XYZ: no quote data available`** The symbol is wrong or missing its exchange suffix.
- **`Groq API key is missing`** or **`Groq HTTP status: 401`** The Groq key is missing or no longer valid. The email still sends, but every AI section reads "Unavailable."
- **`TAVILY_API_KEY is not configured`** or **`Tavily search failed`** The news key is missing, invalid, or out of monthly credits. Top Market Stories will be empty and company write-ups will have fewer sources.
- **`Failed to fetch Gist: 401`** The Gist token has expired. The report still sends, but the once-a-day guard stops working, so duplicate emails are possible until the token is replaced.
- **Gmail login error (`SMTPAuthenticationError`)** The Gmail app password was revoked or changed. Create a new one and update `EMAIL_PASSWORD`.

---

## Changes that require editing monitor.py

These aren't config settings. Make them carefully, or get help:

- Report layout, section order, fonts or colours
- The instructions given to the AI (tone, length, what each section covers)
- The AI model (currently `openai/gpt-oss-120b` on Groq)
- The search phrases used to find market news
- The built-in list of display names (easier to override with `TICKER_NAMES` in `config.py`)
