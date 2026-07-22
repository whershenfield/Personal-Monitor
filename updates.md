# Adding a stock
# Simply add the ticker to the TICKERS list in config.py. No other changes needed.

# Adding a new email
# Both config.py and the GitHub secret must be updated — the script uses the secret when
# running in GitHub Actions, so config.py alone is not enough.

# 1. Update the EMAIL_RECIPIENT list in config.py with the new address.

# 2. Navigate to Settings -> Security & Quality -> Secrets & Variables -> Actions ->
#    Repository Secrets -> 'EMAIL_RECIPIENT' -> click the pencil icon to edit.
#    Enter all emails as a comma-separated list with no quotes and no spaces after commas.
#    Example: Email_1@steinbergwealth.com,Email_2@steinbergwealth.com

# Tip: Write out the full list in config.py first, then copy it into the GitHub secret
#      and remove the quotation marks, spaces and brackets.


# Contact whershen@uwo.ca for any other questions!
