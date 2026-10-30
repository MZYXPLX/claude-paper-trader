"use strict";

(function () {
  const MARKETS = ["crypto", "stocks"];
  const MARKET_NAMES = { crypto: "Crypto", stocks: "US stocks" };
  const LAST_RUN_LABELS = { crypto: "Last crypto run", stocks: "Last US stocks run" };
  const SERIES = { claude_sonnet: "--series-1", rule_trend: "--series-2", buy_hold: "--series-3", claude_haiku: "--series-4" };
  const state = { summary: null, markets: {}, current: "crypto", chart: null };

  const $ = (id) => document.getElementById(id);

  // Build DOM nodes. Text always goes through textContent, so data can never inject markup.
  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const child of [].concat(children || [])) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  // --- formatting ---------------------------------------------------------

  const usdFmt = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const isNum = (x) => typeof x === "number" && Number.isFinite(x);

  function fmtUsd(x) {
    return isNum(x) ? usdFmt.format(x) : "n/a";
  }

  // API calls cost fractions of a cent, so show 4 decimals below $1.
  function fmtCost(x) {
    if (!isNum(x)) return "n/a";
    return Math.abs(x) < 1 && x !== 0 ? "$" + x.toFixed(4) : usdFmt.format(x);
  }

  function fmtPrice(x) {
    if (!isNum(x)) return "n/a";
    const abs = Math.abs(x);
    const digits = abs >= 100 ? 2 : abs >= 1 ? 4 : 6;
    return "$" + x.toLocaleString("en-US", { minimumFractionDigits: Math.min(digits, 2), maximumFractionDigits: digits });
  }

  function fmtQty(x) {
    if (!isNum(x)) return "n/a";
    return x.toLocaleString("en-US", { maximumSignificantDigits: 6 });
  }

  function signed(x, digits, suffix) {
    if (!isNum(x)) return el("span", { class: "muted", text: "n/a" });
    const rounded = Number(x.toFixed(digits));
    const sign = rounded > 0 ? "+" : rounded < 0 ? "-" : "";
    const cls = rounded > 0 ? "pos" : rounded < 0 ? "neg" : "";
    return el("span", { class: cls, text: sign + Math.abs(rounded).toFixed(digits) + suffix });
  }

  function pad(n) {
    return String(n).padStart(2, "0");
  }

  function fmtUtc(iso) {
    if (!iso) return "n/a";
    const d = new Date(iso);
    return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`;
  }

  function fmtLocal(iso) {
    if (!iso) return "";
    return new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) + " your time";
  }

  function fmtDate(iso) {
    return iso ? fmtUtc(iso).slice(0, 10) : "n/a";
  }

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function seriesColor(id, index) {
    return cssVar(SERIES[id] || `--series-${(index % 5) + 1}`);
  }

  const STATUS_TEXT = {
    ok: "OK",
    skipped_market_closed: "Skipped: market closed",
    skipped_disabled: "Skipped: market disabled",
    failed_data: "Failed: no data",
    decision_error: "Decision error",
    budget_exceeded: "Budget exceeded",
    failed: "Failed",
  };

  function statusTag(status) {
    const bad = ["failed", "failed_data", "decision_error", "budget_exceeded"].includes(status);
    return el("span", { class: "tag" + (bad ? " tag-error" : ""), text: STATUS_TEXT[status] || status });
  }

  function modeTag(mode) {
    if (mode === "live") return el("span", { class: "tag tag-live", text: "Live (Claude API)" });
    if (mode === "dry_run") return el("span", { class: "tag tag-dry", text: "Dry run" });
    if (mode === "mixed") return el("span", { class: "tag tag-error", text: "Mixed: reset needed" });
    return null;
  }

  function table(target, headers, rows, emptyText) {
    const t = $(target);
    t.replaceChildren();
    if (!rows.length) {
      t.append(el("tbody", {}, el("tr", {}, el("td", { class: "empty", text: emptyText }))));
      return;
    }
    t.append(
      el("thead", {}, el("tr", {}, headers.map((h) => el("th", { scope: "col", class: h.num ? "num" : null, text: h.label })))),
      el("tbody", {}, rows.map((cells) => el("tr", {}, cells)))
    );
  }

  const td = (content, cls) => el("td", { class: cls || null }, content);
  const tdNum = (content) => el("td", { class: "num" }, content);

  // --- header, banners, spend, runs ----------------------------------------

  function renderHeader() {
    const s = state.summary;
    const label = $("mode-label");
    label.textContent = s.mode_label;
    label.className = "tag " + (s.mode_label.startsWith("Live") ? "tag-live" : "tag-dry");
    if (isNum(s.starting_capital_usd)) {
      $("capital-note").textContent = `Every portfolio starts with ${fmtUsd(s.starting_capital_usd)} of pretend money.`;
    }
    const facts = $("header-facts");
    facts.replaceChildren();
    const fact = (term, ...value) => facts.append(el("div", {}, [el("dt", { text: term }), el("dd", {}, value)]));
    fact("Experiment started", s.experiment_started_at ? fmtDate(s.experiment_started_at) : "Not started yet");
    for (const m of MARKETS) {
      const last = s.markets[m] && s.markets[m].last_run;
      if (!last) {
        fact(LAST_RUN_LABELS[m], el("span", { class: "muted", text: "No runs yet" }));
        continue;
      }
      fact(
        LAST_RUN_LABELS[m],
        statusTag(last.status),
        " ",
        el("span", { class: "tabular", text: fmtUtc(last.started_at) }),
        el("br"),
        el("span", { class: "muted", text: fmtLocal(last.started_at) })
      );
    }
  }

  function renderBanners() {
    const box = $("banners");
    box.replaceChildren();
    for (const b of state.summary.banners || []) {
      const prefix = b.market ? MARKET_NAMES[b.market] + ":" : null;
      box.append(el("div", { class: "banner" + (b.level === "error" ? " banner-error" : ""), role: "status" }, [prefix ? el("strong", { text: prefix }) : null, b.text]));
    }
  }

  function renderSpend() {
    const c = state.summary.costs;
    const box = $("spend");
    const pct = c.cap > 0 ? Math.min(100, (c.month_to_date / c.cap) * 100) : 0;
    box.replaceChildren(
      el("p", { class: "tabular" }, [
        `Month to date (${c.month}): `,
        el("strong", { text: fmtCost(c.month_to_date) }),
        ` of the ${fmtUsd(c.cap)} monthly cap (${pct.toFixed(1)}%)`,
      ]),
      el("div", { class: "meter", role: "img", "aria-label": `${pct.toFixed(1)}% of the monthly cap used` }, el("span", { style: `width:${pct}%` })),
      el("p", { class: "tabular" }, ["Total since the first run: ", el("strong", { text: fmtCost(c.total) })])
    );
  }

  function renderRuns() {
    const rows = (state.summary.runs || []).map((r) => [
      td(el("span", { class: "tabular", text: fmtUtc(r.started_at) }), "sym"),
      td(MARKET_NAMES[r.market] || r.market, "sym"),
      td(statusTag(r.status)),
      td((r.mode === "live" ? "Live" : "Dry run") + (r.offline ? ", offline" : "") + (r.reset ? ", reset" : "")),
      tdNum(String(r.trades)),
      td(r.errors && r.errors.length ? r.errors.join(" | ") : r.next_open ? `Next open ${fmtUtc(r.next_open)}` : "", "wrap-any"),
    ]);
    table(
      "runs-table",
      [{ label: "Time (UTC)" }, { label: "Market" }, { label: "Status" }, { label: "Mode" }, { label: "Trades", num: true }, { label: "Notes" }],
      rows,
      "No runs yet."
    );
  }

  // --- market panel --------------------------------------------------------

  function renderMarketMeta(rep) {
    const meta = $("market-meta");
    if (!rep.enabled) {
      meta.textContent = "This market is disabled in config.yaml.";
      return;
    }
    const parts = [];
    parts.push(rep.started_at ? `Started ${fmtUtc(rep.started_at)}.` : "Not started yet.");
    parts.push(`Benchmark ${rep.benchmark}.`);
    parts.push(`Universe: ${rep.symbols.join(", ")}.`);
    if (rep.last_run && rep.last_run.errors && rep.last_run.errors.length && !rep.strategies.length) {
      parts.push(`Last run: ${rep.last_run.errors.join(" ")}`);
    }
    meta.textContent = parts.join(" ");
  }

  function renderChart(rep) {
    const canvas = $("equity-chart");
    const empty = $("chart-empty");
    if (state.chart) {
      state.chart.destroy();
      state.chart = null;
    }
    const strategies = rep.strategies.filter((s) => s.equity_curve && s.equity_curve.length);
    if (!strategies.length || typeof Chart === "undefined") {
      canvas.hidden = true;
      empty.hidden = false;
      empty.textContent = typeof Chart === "undefined" && strategies.length ? "The chart library did not load." : "No equity data yet.";
      return;
    }
    canvas.hidden = false;
    empty.hidden = true;
    const text = cssVar("--muted");
    const grid = cssVar("--grid");
    const datasets = strategies.map((s, i) => {
      const color = seriesColor(s.id, i);
      return {
        label: s.label,
        data: s.equity_curve.map(([x, y]) => ({ x, y })),
        borderColor: color,
        backgroundColor: color,
        borderWidth: 2,
        borderDash: s.id === "buy_hold" ? [6, 4] : [],
        pointRadius: s.equity_curve.length <= 3 ? 3 : 0,
        pointHoverRadius: 4,
        tension: 0,
      };
    });
    state.chart = new Chart(canvas, {
      type: "line",
      data: { datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        parsing: false,
        interaction: { mode: "nearest", axis: "x", intersect: false },
        scales: {
          x: {
            type: "linear",
            ticks: { color: text, maxTicksLimit: 7, callback: (v) => fmtUtc(new Date(v).toISOString()).slice(5, 16) },
            grid: { color: grid },
          },
          y: {
            ticks: { color: text, callback: (v) => usdFmt.format(v).replace(/\.00$/, "") },
            grid: { color: grid },
          },
        },
        plugins: {
          legend: { labels: { color: cssVar("--text"), boxWidth: 12, boxHeight: 12 } },
          tooltip: {
            callbacks: {
              title: (items) => (items.length ? fmtUtc(new Date(items[0].parsed.x).toISOString()) : ""),
              label: (item) => `${item.dataset.label}: ${fmtUsd(item.parsed.y)}`,
            },
          },
        },
      },
    });
  }

  function strategyCell(s, index) {
    return td(
      [
      el("span", { class: "swatch", style: `background:${seriesColor(s.id, index)}`, "aria-hidden": "true" }),
      s.label,
      s.enabled ? null : el("span", { class: "muted", text: " (disabled)" }),
        s.is_claude && s.mode ? " " : null,
        s.is_claude ? modeTag(s.mode) : null,
      ],
      "name"
    );
  }

  function renderMetrics(rep) {
    const rows = rep.strategies.map((s, i) => {
      const m = s.metrics;
      const cells = [
        strategyCell(s, i),
        tdNum(fmtUsd(m.equity)),
        tdNum(signed(m.total_return_pct, 2, "%")),
        tdNum(s.id === "buy_hold" ? el("span", { class: "muted", text: "Benchmark" }) : signed(m.excess_return_vs_buy_hold_pct, 2, " pp")),
        tdNum(
          s.is_claude
            ? [signed(m.return_net_of_api_cost_pct, 2, "%"), el("span", { class: "muted", text: ` (API ${fmtCost(m.api_cost)})` })]
            : el("span", { class: "muted", text: "No API cost" })
        ),
        tdNum(signed(isNum(m.max_drawdown_pct) ? -m.max_drawdown_pct : null, 2, "%")),
      ];
      if (m.risk_stats_note) {
        cells.push(el("td", { colspan: "2", class: "muted", text: m.risk_stats_note }));
      } else {
        cells.push(tdNum(isNum(m.volatility_pct) ? m.volatility_pct.toFixed(1) + "%" : "n/a"), tdNum(isNum(m.sharpe) ? m.sharpe.toFixed(2) : "n/a"));
      }
      cells.push(
        tdNum(String(m.trades)),
        tdNum(isNum(m.turnover) ? m.turnover.toFixed(2) + "x" : "n/a"),
        tdNum(fmtUsd(m.fees_paid)),
        tdNum(fmtUsd(m.slippage_paid)),
        tdNum(m.round_trips_closed ? `${m.win_rate_pct.toFixed(0)}% of ${m.round_trips_closed}` : el("span", { class: "muted", text: "No closed trades" })),
        tdNum(isNum(m.days_running) ? m.days_running.toFixed(1) : "n/a")
      );
      return cells;
    });
    table(
      "metrics-table",
      [
        { label: "Strategy" },
        { label: "Equity", num: true },
        { label: "Return", num: true },
        { label: "vs buy and hold", num: true },
        { label: "Net of API cost", num: true },
        { label: "Max drawdown", num: true },
        { label: "Volatility", num: true },
        { label: "Sharpe", num: true },
        { label: "Trades", num: true },
        { label: "Turnover", num: true },
        { label: "Fees", num: true },
        { label: "Slippage", num: true },
        { label: "Win rate", num: true },
        { label: "Days", num: true },
      ],
      rows,
      "No portfolios yet."
    );
  }

  function renderDecisions(rep) {
    const box = $("claude-decisions");
    box.replaceChildren();
    const claude = rep.strategies.filter((s) => s.is_claude);
    if (!claude.length) {
      box.append(el("p", { class: "empty", text: "No Claude decision yet." }));
      return;
    }
    for (const s of claude) {
      const d = s.latest_decision;
      const wrap = el("div", { class: "decision" });
      const head = el("div", { class: "decision-head" }, [el("h3", { text: s.label }), modeTag(s.mode)]);
      wrap.append(head);
      if (!d) {
        wrap.append(el("p", { class: "empty", text: "No Claude decision yet." }));
        box.append(wrap);
        continue;
      }
      head.append(statusTag(d.status), el("span", { class: "muted tabular", text: fmtUtc(d.timestamp) }));
      if (d.risk_level) head.append(el("span", { class: "tag", text: `Risk: ${d.risk_level}` }));
      if (s.mode === "dry_run" || d.source === "mock_claude") {
        wrap.append(el("p", { class: "muted", text: "Dry run: these are mock decisions, not Claude. They do not count toward Claude's record." }));
      }
      if (d.error) wrap.append(el("p", { class: "neg", text: d.error }));
      if (d.market_view) wrap.append(el("p", {}, [el("strong", { text: "Market view: " }), d.market_view]));
      if (d.notes_for_next_run) wrap.append(el("p", { class: "muted" }, [el("strong", { text: "Notes for next run: " }), d.notes_for_next_run]));
      if (d.targets && d.targets.length && d.source) {
        const t = el("table");
        t.append(
          el("thead", {}, el("tr", {}, ["Symbol", "Proposed", "After guardrails", "Conviction", "Reason"].map((h, i) => el("th", { scope: "col", class: i > 0 && i < 4 ? "num" : null, text: h })))),
          el(
            "tbody",
            {},
            d.targets.map((x) =>
              el("tr", {}, [
                td(x.symbol, "sym"),
                tdNum(isNum(x.proposed) ? (x.proposed * 100).toFixed(1) + "%" : "n/a"),
                tdNum(isNum(x.final) ? (x.final * 100).toFixed(1) + "%" : "n/a"),
                tdNum(isNum(x.conviction) ? `${x.conviction} of 5` : "n/a"),
                td(x.reason || "", "reason"),
              ])
            )
          )
        );
        wrap.append(el("div", { class: "table-scroll" }, t));
      }
      const extras = [...(d.warnings || []), ...(d.guardrail_adjustments || [])];
      if (extras.length) {
        wrap.append(el("details", {}, [el("summary", { text: `Guardrail adjustments and warnings (${extras.length})` }), el("ul", {}, extras.map((x) => el("li", { text: x })))]));
      }
      box.append(wrap);
    }
  }

  function renderHoldings(rep) {
    const box = $("holdings");
    box.replaceChildren();
    if (!rep.strategies.length) {
      box.append(el("p", { class: "empty", text: "No portfolios yet." }));
      return;
    }
    rep.strategies.forEach((s, i) => {
      const card = el("div", {}, [
        el("h3", {}, [el("span", { class: "swatch", style: `background:${seriesColor(s.id, i)}`, "aria-hidden": "true" }), s.label]),
        el("p", { class: "muted tabular", text: `Cash ${fmtUsd(s.cash)}, equity ${fmtUsd(s.metrics.equity)}` }),
      ]);
      if (!s.holdings.length) {
        card.append(el("p", { class: "empty", text: "No positions. All cash." }));
      } else {
        const t = el("table");
        t.append(
          el("thead", {}, el("tr", {}, ["Symbol", "Quantity", "Price", "Value", "Weight", "Unrealized"].map((h, j) => el("th", { scope: "col", class: j ? "num" : null, text: h })))),
          el(
            "tbody",
            {},
            s.holdings.map((h) =>
              el("tr", {}, [
                td(h.symbol, "sym"),
                tdNum(fmtQty(h.quantity)),
                tdNum(fmtPrice(h.price)),
                tdNum(fmtUsd(h.value)),
                tdNum(isNum(h.weight_pct) ? h.weight_pct.toFixed(1) + "%" : "n/a"),
                tdNum(signed(h.unrealized_pct, 2, "%")),
              ])
            )
          )
        );
        card.append(el("div", { class: "table-scroll" }, t));
      }
      box.append(card);
    });
  }

  function renderTrades(rep) {
    const rows = rep.recent_trades.map((t) => [
      td(el("span", { class: "tabular", text: fmtUtc(t.timestamp) }), "sym"),
      td(t.strategy_label, "sym"),
      td(t.symbol, "sym"),
      td(el("span", { class: t.side === "buy" ? "pos" : "neg", text: t.side === "buy" ? "Buy" : "Sell" })),
      tdNum(fmtQty(t.quantity)),
      tdNum(fmtPrice(t.fill_price)),
      tdNum(fmtUsd(t.notional)),
      tdNum(fmtUsd(t.fee)),
      tdNum(t.side === "sell" ? signed(t.realized_pnl, 2, "") : ""),
      td(t.reason || "", "reason"),
    ]);
    table(
      "trades-table",
      [
        { label: "Time (UTC)" },
        { label: "Strategy" },
        { label: "Symbol" },
        { label: "Side" },
        { label: "Quantity", num: true },
        { label: "Fill price", num: true },
        { label: "Value", num: true },
        { label: "Fee", num: true },
        { label: "Realized P&L ($)", num: true },
        { label: "Reason" },
      ],
      rows,
      "No trades yet."
    );
  }

  function renderMarket() {
    const rep = state.markets[state.current];
    renderMarketMeta(rep);
    renderChart(rep);
    renderMetrics(rep);
    renderDecisions(rep);
    renderHoldings(rep);
    renderTrades(rep);
  }

  function selectMarket(market, focus) {
    if (!MARKETS.includes(market)) market = "crypto";
    state.current = market;
    for (const m of MARKETS) {
      const tab = $("tab-" + m);
      const on = m === market;
      tab.setAttribute("aria-selected", on ? "true" : "false");
      tab.tabIndex = on ? 0 : -1;
      if (on && focus) tab.focus();
    }
    $("panel").setAttribute("aria-labelledby", "tab-" + market);
    if (location.hash !== "#" + market) history.replaceState(null, "", "#" + market);
    renderMarket();
  }

  function bindTabs() {
    for (const m of MARKETS) {
      $("tab-" + m).addEventListener("click", () => selectMarket(m));
    }
    document.querySelector(".tabs").addEventListener("keydown", (e) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      const i = MARKETS.indexOf(state.current);
      const next = MARKETS[(i + (e.key === "ArrowRight" ? 1 : MARKETS.length - 1)) % MARKETS.length];
      selectMarket(next, true);
      e.preventDefault();
    });
  }

  async function getJSON(path) {
    const res = await fetch(path, { cache: "no-store" });
    if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
    return res.json();
  }

  async function init() {
    bindTabs();
    try {
      const [summary, ...reports] = await Promise.all(["data/summary.json", ...MARKETS.map((m) => `data/${m}.json`)].map(getJSON));
      state.summary = summary;
      MARKETS.forEach((m, i) => (state.markets[m] = reports[i]));
    } catch (err) {
      const box = $("load-error");
      box.hidden = false;
      box.textContent =
        location.protocol === "file:"
          ? "Browsers block this page from reading its data files when it is opened from disk. Serve the docs folder instead, for example: python -m http.server --directory docs 8000, then open http://localhost:8000."
          : "Could not load the dashboard data (" + err.message + "). The first scheduled run creates it.";
      $("mode-label").textContent = "No data";
      return;
    }
    renderHeader();
    renderBanners();
    renderSpend();
    renderRuns();
    selectMarket(location.hash.replace("#", ""));
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => renderMarket());
  }

  document.addEventListener("DOMContentLoaded", init);
})();
