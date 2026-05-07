# AGENTS.md

## Cursor Cloud specific instructions

This is a single-file Python project (VCP Scanner). See `README.md` for full documentation.

### Key commands
- **Install deps:** `pip install -r requirements.txt` (also install `html5lib` which the CI uses but is not in requirements.txt)
- **Run tests:** `python3 -m unittest test_vcp_scanner.py -v`
- **Run scanner:** `python3 vcp_scanner.py` (scans all S&P 500 stocks; takes ~2 minutes with network access)
- **Lint:** No linter is configured in this project.

### Gotchas
- The scanner downloads live stock data via `yfinance` (Yahoo Finance) and the S&P 500 list from Wikipedia. Both require internet access. If Wikipedia is unreachable, it falls back to a hardcoded 10-stock list.
- A full scan of 500+ stocks takes about 2 minutes. For faster iteration, set `MIN_SCORE` and `TREND_MIN_PASSED` env vars to filter more aggressively.
- Email sending is optional and only triggers when `EMAIL_SENDER`, `EMAIL_PASSWORD`, and `EMAIL_RECIPIENT` env vars are all set.
- Output files (`vcp_results.json`, `results/`, `vcp_scan.log`) are generated in the working directory.
