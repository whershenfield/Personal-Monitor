GROQ_API_KEY = ""
GIST_ID = ""
GIST_TOKEN = ""
TAVILY_API_KEY = ""

REPORT_AUTHOR = "Will Hershenfield" # Your name, shown as "Prepared by ..." next to the date

RATES_CHART_DAYS = 90 # This can only go back as far as Jan 1 of the previous year

INDICES = ["^GSPC", "^IXIC", "^RUT", "^DJI", "EFA", "^GSPTSE"]
TICKERS = ["EL.PA", "SPGI"]

PORTFOLIO_TITLE = "Factor Strategy (Current)"
GROWTH_PORTFOLIO_TITLE = "Growth Strategy"
PASSIVE_PORTFOLIO_TITLE = "Passive Core"
CANADIAN_COMPARISON_TITLE = "Canadian Equity: Passive vs. Factor"

PORTFOLIO = ["CAUS.TO", "CALV.TO", "VCN.TO", "CADE.TO"]
PORTFOLIO_WEIGHTS = {"CAUS.TO": 0.35,"CALV.TO": 0.15,"VCN.TO": 0.20,"CADE.TO": 0.30}

GROWTH_PORTFOLIO = ["QQC.TO", "ZUQ.TO", "VGG.TO", "VCN.TO", "ZEA.TO"]
GROWTH_PORTFOLIO_WEIGHTS = {"QQC.TO": 0.5/3,"ZUQ.TO": 0.5/3,"VGG.TO": 0.5/3,"VCN.TO": 0.20,"ZEA.TO": 0.30}

PASSIVE_PORTFOLIO = ["ZSP.TO", "VCN.TO", "ZEA.TO"]
PASSIVE_PORTFOLIO_WEIGHTS = {"ZSP.TO": 0.50,"VCN.TO": 0.20,"ZEA.TO": 0.30}

CANADIAN_COMPARISON = ["VCN.TO", "CACE.TO"] # Side-by-side comparison (no weights). Leave as [] to hide.

EMAIL_SENDER = ""
EMAIL_PASSWORD = ""
EMAIL_RECIPIENT = []
