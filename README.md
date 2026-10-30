# Claude Paper Trader

A small, automated paper-trading experiment that answers one question: **does Claude make better trading decisions than simple baselines when it manages pretend money on real, live market prices?**

Every portfolio starts with $10,000 of pretend money. Nothing here can place a real trade: there is no live-trading code path.

## 1. What the experiment is

Each scheduled run on GitHub Actions fetches real prices, asks each strategy for target weights, applies the same code-enforced risk rules to all of them, simulates the fills with the same fee and slippage model, records everything, and rebuilds a dashboard on GitHub Pages.

Strategies, per market:

| Strategy | What it does |
|---|---|
| `claude_sonnet` | Claude (`claude-sonnet-5-5`) reads a JSON snapshot of the portfolio, the rules and the market features, and returns target weights. This is the strategy under test. |
| `rule_trend` | Deterministic trend rule: hold a symbol when its last daily close is above its 50-day average and the 20-day average is above the 50-day average. Equal weights, capped at 25%. This is the baseline Claude must beat. |
| `buy_hold` | Buys the market's benchmark (BTC-USD or SPY) on day one and never trades again. |
| `claude_haiku` | Optional, off by default. Same as `claude_sonnet` with `claude-haiku-5-5`, to compare the cheaper model. |

Markets: crypto (BTC, ETH, SOL, XRP, LINK, DOGE, trading 24/7, every 4 hours) and US stocks (12 large names and ETFs, 3 times per trading day).

**Why it is forward-tested only.** Claude's training data includes past market history, so a backtest of the Claude strategy would partly measure its memory of what happened. The experiment only counts decisions made on live data after the bot starts. There is deliberately no backtesting code for the Claude strategy.

## 2. Setup

1. **Create a public GitHub repository** and push this folder to it. It must be public because GitHub Pages is free on public repositories; Pages on private repositories needs a paid GitHub plan.
2. **Add secrets** under Settings > Secrets and variables > Actions > New repository secret:
   - `ANTHROPIC_API_KEY` (optional at first: without it the bot runs in dry-run mode)
   - `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` (needed for US stocks; without them the stocks market is skipped with a clear message)
3. **Enable GitHub Pages** under Settings > Pages > Build and deployment > Source: **GitHub Actions**. The workflows publish the `docs/` folder from `main` themselves (see Decisions for why this is not "Deploy from a branch").
4. **Enable Actions** under the Actions tab if GitHub asks you to. If a run fails to push, check Settings > Actions > General > Workflow permissions and allow read and write permissions.
5. **Start it**: open the Actions tab, pick "Crypto paper trading", click "Run workflow". Do the same for "Stocks paper trading". After that the schedules take over.

Schedules and GitHub behavior to know about:

- Cron times are in **UTC**. Crypto runs at minute 0 every 4 hours. Stocks run at 14:35, 17:35 and 19:35 UTC on weekdays, which is inside US market hours in both winter and summer time. Holidays are handled by Alpaca's market clock.
- GitHub may **delay scheduled runs** by minutes at busy times, and occasionally drops one.
- In public repositories GitHub **disables scheduled workflows after 60 days without repository activity**. The bot's own commits count as activity, so this should not trigger while it runs, but if it ever happens, re-enable the workflows in the Actions tab.
- Both workflows share one concurrency group, so two runs never write state at the same time.

## 3. Getting the keys

- **Anthropic API key**: create one in the [Claude Console](https://platform.claude.com/). API usage is billed with prepaid credits, separately from any Claude.ai subscription. The budget cap in `config.yaml` (default $10 per month) stops Claude calls before that amount is spent.
- **Alpaca paper keys**: sign up for a free paper-only account at [Alpaca](https://alpaca.markets/), open the paper trading dashboard and generate API keys there. They include free market data on the IEX feed, which is what this project uses. The code only builds Alpaca clients in paper mode and only reads market data and the market clock.

## 4. Switching from dry run to live

Until `ANTHROPIC_API_KEY` is set, Claude strategies use a mock that makes small random tilts around equal weight. Mock decisions are labeled "Dry run" everywhere and never count as Claude's record.

To go live:

1. Add the `ANTHROPIC_API_KEY` secret.
2. Run **each** workflow once manually with **reset: true**. This moves that market's state into `archive/<UTC timestamp>/` and starts every portfolio in that market fresh at the same moment, so Claude and the baselines start together.

If a Claude portfolio ever holds both mock and live decisions, the dashboard shows a banner asking for a reset.

## 5. Changing things

Everything lives in `config.yaml`, which is validated on load (a typo fails the run with a clear error):

- **Schedule**: edit the `cron` lines in `.github/workflows/crypto.yml` and `stocks.yml` (UTC).
- **Symbols**: `markets.crypto.symbols` (Coinbase product IDs like `BTC-USD`) and `markets.stocks.symbols`. The benchmark must be one of the symbols. Changing the universe mid-experiment makes comparisons harder; consider a reset.
- **Risk limits**: the `risk` section (position cap, cash floor, turnover cap, minimum trade size, minimum weight change, drawdown halt). `experiment.trading_enabled: false` is a kill switch: prices and equity are still recorded but no simulated trades are placed.
- **Models**: `strategies[].model`. Turn on `claude_haiku` with `enabled: true`. Every Claude model needs a price entry under `budget.prices_per_mtok`.
- **Budget**: `budget.monthly_usd_cap`. Keep `prices_per_mtok` in sync with Anthropic's pricing page.
- **Claude settings**: `claude.max_tokens`, `claude.effort`, `claude.history_runs_in_prompt`.

## 6. Running locally

Requires Python 3.12.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip
cp .env.example .env                              # then fill in keys, or leave them empty for dry run
```

Run one cycle:

```bash
python -m bot.main --market crypto --offline     # recorded fixtures, no network calls
python -m bot.main --market crypto               # live prices (dry run unless a key is set)
python -m bot.main --market crypto --dry-run     # live prices, mock Claude even with a key
python -m bot.main --market crypto --reset       # archive this market's state and start fresh
python -m bot.report                             # rebuild docs/data/*.json
```

Offline mode reads `tests/fixtures/<market>/`. Crypto fixtures are included. Stock fixtures need Alpaca keys to record:

```bash
python -m bot.data.fixtures --market stocks
```

Tests (no test calls a real API; network access is blocked during tests):

```bash
python -m pytest
```

View the dashboard locally. Browsers block pages opened from disk from reading their data files, so serve the folder:

```bash
python -m http.server --directory docs 8000
```

Then open http://localhost:8000.

Local runs write to the same `state/` and `docs/data/` folders the workflows commit. Do not commit local test runs; reset or delete those folders' contents first.

## 7. How to read the results

- **Wait at least 4 to 8 weeks and dozens of trades** before drawing conclusions. Early returns are mostly noise, and one market regime can favor any strategy for a while.
- Compare Claude to **both** `rule_trend` and `buy_hold`, and look at **return net of API cost**: Claude has to earn back what its decisions cost.
- Volatility and Sharpe appear once a strategy has 30 daily data points. Max drawdown matters as much as return.
- Check trades and turnover: a strategy that trades a lot pays more in fees and slippage.
- Claude's market view, per-symbol reasons and notes are on the dashboard and in `state/<market>/claude_sonnet/decisions.jsonl` with full API logs (model, token usage, latency, raw response).

## 8. Files

```
config.yaml                     all settings
bot/main.py                     one run: data, features, strategies, guardrails, fills, logs
bot/data/                       Coinbase + Kraken (crypto), Alpaca (stocks), features, fixtures
bot/strategies/                 buy_hold, rule_trend, claude, mock_claude, system prompt
bot/risk.py                     guardrails, in order, every adjustment logged
bot/execution.py                simulated fills, FIFO lots
bot/ledger.py                   state files, archive and reset
bot/costs.py                    API cost tracking and the monthly cap
bot/metrics.py, bot/report.py   metrics and dashboard data
state/                          portfolios, trades, equity, decisions, runs.jsonl, costs.jsonl
archive/                        earlier experiments moved here by a reset
docs/                           the static dashboard (GitHub Pages)
```

## 9. Decisions

Defaults chosen during the build, where the brief left room or where current docs required a change:

**Claude API**
- **Structured outputs instead of a forced tool call.** `claude-sonnet-5-5` rejects `tool_choice` set to a specific tool (HTTP 400), so the decision comes back through `output_config.format` with a JSON schema (symbols as an enum), then pydantic validates ranges and lengths. The last line of the system prompt says to submit the decision as one JSON object instead of calling a tool.
- **`max_tokens` 4000 instead of 2000, and `effort: low`.** Sonnet 5.5 uses adaptive thinking by default and thinking tokens count toward `max_tokens`; 2000 risked cutting off a 12-stock decision. Low effort keeps thinking short. Estimated spend for `claude_sonnet` in both markets is roughly $5 to $7 per month (about 250 calls of 4,000 to 5,000 input tokens). Responses cut off at `max_tokens` count as invalid and are retried once.
- **No prompt caching.** The system prompt is about 400 tokens, below the minimum cacheable size.
- **No refusal fallback to another model.** A fallback would let a different model make some of "Claude Sonnet's" decisions. A refusal counts as an invalid response.
- **Retries**: the Anthropic SDK retries rate-limit, overloaded, server and connection errors with exponential backoff (`claude.max_retries`). After that the run logs `decision_error` and that portfolio holds.
- **Budget check** uses a worst-case estimate (input characters / 3 plus the full `max_tokens` of output) before each call. Cost includes prompt-cache tokens at 1.25x (writes) and 0.1x (reads) the input price.

**Guardrails and execution**
- **`buy_hold` is exempt from the 25% position cap and the turnover cap.** Otherwise it could only hold 25% of its benchmark and would take two runs to buy in. It still keeps the 5% cash floor.
- **Full exits are always allowed**, even below `min_weight_change` or `min_trade_usd`, so small leftover positions can be closed.
- A change exactly at a threshold (for example a $25.00 trade) is allowed.
- **Turnover counts both sides** (selling 30% and buying 30% is 60%). After scaling trades down to the turnover cap, any trade below `min_trade_usd` is dropped.
- **Stale symbols keep their weight** and are excluded when scaling to the cash floor, so a stale symbol is never traded.
- Cost basis includes the buy fee and slippage. Each sell, matched FIFO against earlier buys, is one closed round trip for the win rate. Turnover is total traded value divided by starting capital.
- The **drawdown halt** is re-checked every run against the portfolio's peak equity and lifts if equity recovers.

**Data and features**
- Crypto latest price is the close of the newest hourly candle; it counts as observed at the end of that candle (or now, if the candle is still open). If Coinbase fails for a symbol after retries, that symbol falls back to Kraken.
- Features use completed daily bars only (today's forming bar is dropped). Returns compare the live price with the close N bars back: 1, 7 and 30 days for crypto; 1, 5 and 21 trading days for stocks. RSI14 and ATR14 use Wilder's smoothing. Realized volatility is the sample standard deviation of 30 daily log returns, annualized with 365 (crypto) or 252 (stocks). Volume ratio is the last completed day's volume over the 20-day mean including that day.
- Runs that get no prices (`skipped_market_closed`, `failed_data`) write no equity snapshot, so weekends and holidays do not add flat days to the stock return series. Every run that gets prices writes one for every strategy, even with no trades.

**Runs, state and reset**
- **Reset is per market**: `--reset` moves `state/<market>/` into `archive/<UTC timestamp>/<market>/`, so resetting crypto never touches stocks. `runs.jsonl` and `costs.jsonl` stay in place: the runs log is history, and API spend must keep counting toward the monthly cap.
- Two run statuses were added to the brief's list: `skipped_disabled` (market disabled in config, or Alpaca keys missing) and `failed` (an unexpected error; the traceback is in the workflow log).
- Claude portfolios count their mock and live decisions, which is how the dashboard labels them "Dry run", "Live" or "Mixed: reset needed".
- The mock used in dry run tilts equal weights by up to 30% with a random generator seeded by run ID and strategy.
- Offline mode shifts fixture timestamps so the newest candle lines up with the current time (whole days for daily bars, whole hours for hourly bars). Prices are not changed.
- `mirror_claude_to_alpaca_paper` is accepted in config but not implemented yet (optional phase 5); a run with it on notes that it was ignored.

**GitHub and the dashboard**
- **Pages is deployed by the workflows** (`actions/upload-pages-artifact` and `actions/deploy-pages`), with the Pages source set to "GitHub Actions". GitHub's documentation states that commits pushed by a workflow using `GITHUB_TOKEN` do not trigger a Pages build, so "Deploy from a branch" would never show new data. The deployed content is still the `docs/` folder on `main`. Changes you push to `docs/` yourself go live with the next scheduled run, or run either workflow manually.
- Action versions: `actions/checkout@v7`, `actions/setup-python@v7`, `actions/upload-pages-artifact@v5`, `actions/deploy-pages@v5`.
- Chart.js is pinned to 4.5.1 with a subresource integrity hash (4.6.1 was released too recently to pin).
- Python dependencies are pinned in `requirements.txt` to the versions the tests ran against.
- The project lives in its own folder (`claude-paper-trader/`) so it can be pushed as its own repository.

## 10. Disclaimer

Simulated trading with pretend money. Not financial advice. Past or simulated performance says nothing reliable about future results.
