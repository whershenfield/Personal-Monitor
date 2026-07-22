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
config.EMAIL_SENDER = os.environ.get("EMAIL_SENDER", config.EMAIL_SENDER)
config.EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", config.EMAIL_PASSWORD)
config.EMAIL_RECIPIENT = os.environ.get("EMAIL_RECIPIENT", ",".join(config.EMAIL_RECIPIENT)).split(",")
config.GROQ_API_KEY = os.environ.get("GROQ_API_KEY", config.GROQ_API_KEY)
config.GIST_ID = os.environ.get("GIST_ID", config.GIST_ID)
config.GIST_TOKEN = os.environ.get("GIST_TOKEN_GITHUB", config.GIST_TOKEN)


def is_market_open():
    et = pytz.timezone("America/New_York")
    now = datetime.now(et)
    if now.weekday() >= 5:
        return False
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= now <= market_close


def get_quote(ticker):
    try:
        stock = yf.Ticker(ticker)

        hist = stock.history(period="2d")
        if len(hist) < 2:
            return None, None
        prev_close = hist["Close"].iloc[-2]

        current = stock.fast_info.last_price

        if not current or not prev_close:
            return None, None
        return current, prev_close
    except Exception as e:
        print(f"Error fetching {ticker}: {e}")
        return None, None


def pct_change(current, prev_close):
    return ((current - prev_close) / prev_close) * 100


def get_alert_state():
    headers = {
        "Authorization": f"token {config.GIST_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    r = requests.get(f"https://api.github.com/gists/{config.GIST_ID}", headers=headers)
    if r.status_code != 200:
        print(f"Failed to fetch Gist: {r.status_code}")
        return {}
    files = r.json().get("files", {})
    content = files.get("alerted_today.json", {}).get("content", "{}")
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return {}


def save_alert_state(data):
    headers = {
        "Authorization": f"token {config.GIST_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    payload = {
        "files": {
            "alerted_today.json": {
                "content": json.dumps(data)
            }
        }
    }
    r = requests.patch(
        f"https://api.github.com/gists/{config.GIST_ID}",
        headers=headers,
        data=json.dumps(payload)
    )
    if r.status_code != 200:
        print(f"Failed to save Gist: {r.status_code}")


def already_alerted(ticker, state):
    today = date.today().isoformat()
    return state.get(ticker) == today


def get_ai_summary(ticker, chg, current, prev_close):
    try:
        stock = yf.Ticker(ticker)
        company_name = stock.info.get("longName", ticker)
    except Exception:
        company_name = ticker

    try:
        stock = yf.Ticker(ticker)
        news = stock.news
        headlines = "\n".join([
            a.get("content", {}).get("title", "")
            for a in news[:5]
            if a.get("content", {}).get("title")
        ])
    except Exception:
        headlines = ""

    if not headlines.strip():
        return company_name, "No recent news found to explain the move."

    groq_url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "llama-3.3-70b-versatile",
        "max_tokens": 300,
        "messages": [
            {
                "role": "user",
                "content": (
                    f"{ticker} ({company_name}) is {chg:+.2f}% today "
                    f"(prev close: ${prev_close:.2f}, current: ${current:.2f}). "
                    f"Based on these recent headlines, respond in exactly two sections:\n\n"
                    f"WHAT HAPPENED: 2-3 sentences summarizing any direct, factual reasons "
                    f"for the stock's move based strictly on the headlines provided.\n\n"
                    f"BROADER CONTEXT: 2-3 sentences speculating on wider sector trends, "
                    f"macro factors, or industry dynamics that may also be influencing "
                    f"the stock, even if not directly mentioned in the headlines.\n\n"
                    f"Be concise and factual in the first section, clearly speculative "
                    f"in the second. Headlines:\n{headlines}"
                )
            }
        ]
    }
    response = requests.post(groq_url, headers=headers, data=json.dumps(payload))
    result = response.json()

    try:
        return company_name, result["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        return company_name, "Could not generate summary at this time."


def send_email(subject, body):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = config.EMAIL_SENDER
    msg["To"] = ", ".join(config.EMAIL_RECIPIENT)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(config.EMAIL_SENDER, config.EMAIL_PASSWORD)
        server.sendmail(config.EMAIL_SENDER, config.EMAIL_RECIPIENT, msg.as_string())


def check_tickers():
    et = pytz.timezone("America/New_York")
    now = datetime.now(et).strftime("%I:%M %p ET")
    today = date.today().isoformat()

    state = get_alert_state()

    for ticker in config.TICKERS:
        current, prev_close = get_quote(ticker)
        if current is None:
            print(f"Could not fetch data for {ticker}")
            continue

        chg = pct_change(current, prev_close)
        direction = "UP" if chg > 0 else "DOWN"
        print(f"{ticker}: {chg:+.2f}%")

        if abs(chg) >= config.THRESHOLD_PCT:
            if already_alerted(ticker, state):
                print(f"{ticker}: already alerted today, skipping")
                continue
            company_name, summary = get_ai_summary(ticker, chg, current, prev_close)
            subject = f"GVE & CDG Alert: {company_name} ({ticker}) is {direction} {abs(chg):.2f}% today"
            body = (
                f"{ticker} has moved {chg:+.2f}% from yesterday's close.\n\n"
                f"Previous close: ${prev_close:.2f}\n"
                f"Current price:  ${current:.2f}\n\n"
                f"Why it moved:\n{summary}\n\n"
                f"Checked at {now} automatically via Yahoo Finance & Groq"
            )
            send_email(subject, body)
            state[ticker] = today
            save_alert_state(state)
            print(f"Alert sent for {ticker}")


if __name__ == "__main__":
    if is_market_open():
        check_tickers()
    else:
        print("Outside market hours, exiting.")
