"""CLI entry point — Indian Equity Long-Term Investor."""
from __future__ import annotations

import asyncio
import re
from datetime import date
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from src.agent.pipeline import InvestmentPipeline
from src.config import settings
from src.logging_config import configure_logging
from src.models import AnalysisState
from src.portfolio.tracker import PortfolioTracker

_TICKER_RE = re.compile(r"^[A-Z0-9&\-\.]{1,20}$")


def _validate_ticker(ticker: str) -> str:
    """Normalise and validate a ticker symbol. Raises click.BadParameter on invalid input."""
    normalised = ticker.upper().strip()
    if not _TICKER_RE.match(normalised):
        raise click.BadParameter(
            f"'{ticker}' is not a valid NSE ticker. "
            "Expected 1–20 uppercase alphanumeric characters (e.g. RELIANCE, M&M, BAJAJ-AUTO)."
        )
    return normalised

console = Console()


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------


@click.group()
@click.version_option("1.0.0")
def cli() -> None:
    """Indian Equity Long-Term Investor — AI-powered stock analysis.

    Uses a 9-step research pipeline backed by Claude AI to evaluate
    long-term investment opportunities on NSE/BSE.
    """
    configure_logging(log_level=settings.log_level, log_format=settings.log_format)


# ---------------------------------------------------------------------------
# analyze command
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("ticker")
@click.option(
    "--save/--no-save",
    default=True,
    help="Save report to analysis/reports/ (default: True)",
)
def analyze(ticker: str, save: bool) -> None:
    """Run full 9-step analysis on a stock TICKER.

    Example: investor analyze RELIANCE
    """
    ticker = _validate_ticker(ticker)

    async def _run() -> AnalysisState:
        pipeline = InvestmentPipeline()
        return await pipeline.analyze(ticker)

    with console.status(f"[bold green]Analysing {ticker.upper()}...[/bold green]"):
        state = asyncio.run(_run())

    if state.formatted_output:
        console.print(state.formatted_output)
    else:
        console.print(f"[yellow]No formatted output generated for {ticker}[/yellow]")

    if save and state.formatted_output:
        _save_report(ticker, state)


def _save_report(ticker: str, state: AnalysisState) -> None:
    """Save the formatted report to analysis/reports/."""
    reports_dir = Path("analysis") / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    report_path = reports_dir / f"{ticker.upper()}_{today}.md"
    report_path.write_text(state.formatted_output or "", encoding="utf-8")
    console.print(f"[dim]Report saved → {report_path}[/dim]")


# ---------------------------------------------------------------------------
# portfolio command
# ---------------------------------------------------------------------------


@cli.command()
def portfolio() -> None:
    """Show current portfolio holdings and summary."""
    tracker = PortfolioTracker()
    holdings = tracker.get_holdings()

    if not holdings:
        console.print("[yellow]No holdings found in portfolio/holdings.md[/yellow]")
        return

    table = Table(title="Portfolio Holdings", show_header=True, header_style="bold cyan")
    table.add_column("Ticker", style="bold")
    table.add_column("Company")
    table.add_column("Avg Cost", justify="right")
    table.add_column("Qty", justify="right")
    table.add_column("Purchase Date")
    table.add_column("Allocation %", justify="right")

    total_alloc = 0.0
    for h in holdings:
        table.add_row(
            h["ticker"],
            h.get("company_name", ""),
            f"₹{h['avg_cost']:.2f}",
            str(h["quantity"]),
            h.get("purchase_date", ""),
            f"{h['allocation_pct']:.1f}%",
        )
        total_alloc += h["allocation_pct"]

    console.print(table)
    console.print(f"\n[bold]Total Allocation: {total_alloc:.1f}%[/bold]")


# ---------------------------------------------------------------------------
# add-trade command
# ---------------------------------------------------------------------------


@cli.command("add-trade")
@click.argument("ticker")
@click.argument("action", type=click.Choice(["BUY", "SELL"], case_sensitive=False))
@click.argument("qty", type=int)
@click.argument("price", type=float)
@click.option(
    "--date",
    "txn_date",
    default=None,
    help="Trade date in YYYY-MM-DD format (default: today).",
)
@click.option("--company", default="", help="Company name (used only when adding a new BUY holding).")
@click.option("--allocation", "allocation_pct", default=0.0, type=float, help="Allocation % of portfolio (BUY only).")
@click.option("--notes", default="", help="Optional notes recorded in the transaction log.")
def add_trade(
    ticker: str,
    action: str,
    qty: int,
    price: float,
    txn_date: str | None,
    company: str,
    allocation_pct: float,
    notes: str,
) -> None:
    """Record a BUY or SELL transaction.

    Updates transaction-log.md and (for BUY) adds a row to holdings.md and
    tax-tracker.md so LTCG eligibility is tracked automatically.

    \b
    Examples:
      investor add-trade RELIANCE BUY 10 2850.50
      investor add-trade INFY SELL 5 1920.00 --date 2026-05-15 --notes "partial exit"
      investor add-trade HDFCBANK BUY 20 1650.00 --company "HDFC Bank" --allocation 5.0
    """
    ticker = _validate_ticker(ticker)
    action = action.upper()

    # Parse / default the date
    try:
        trade_date = date.fromisoformat(txn_date) if txn_date else date.today()
    except ValueError:
        raise click.BadParameter(
            f"'{txn_date}' is not a valid date. Use YYYY-MM-DD format.",
            param_hint="--date",
        )

    if qty <= 0:
        raise click.BadParameter("QTY must be a positive integer.", param_hint="qty")
    if price <= 0:
        raise click.BadParameter("PRICE must be positive.", param_hint="price")

    tracker = PortfolioTracker()

    # Always log to transaction-log.md
    tracker.add_transaction(
        ticker=ticker,
        action=action,
        price=price,
        quantity=qty,
        txn_date=trade_date,
        notes=notes,
    )
    console.print(
        f"[green]✓[/green] Transaction logged: {action} {qty} × {ticker} @ ₹{price:.2f} on {trade_date}"
    )

    if action == "BUY":
        # Add row to holdings.md
        tracker.add_holding(
            ticker=ticker,
            avg_cost=price,
            quantity=qty,
            purchase_date=trade_date,
            allocation_pct=allocation_pct,
            company_name=company or ticker,
        )
        console.print(f"[green]✓[/green] Holding added to holdings.md")

        # Add LTCG eligibility row (1 year from purchase date)
        from datetime import timedelta
        ltcg_date = trade_date.replace(year=trade_date.year + 1)
        tracker.update_tax_tracker(
            ticker=ticker,
            purchase_date=trade_date,
            ltcg_date=ltcg_date,
            avg_cost=price,
        )
        console.print(
            f"[green]✓[/green] Tax tracker updated — LTCG eligible from {ltcg_date} "
            f"(gains > ₹1.25L taxed at 12.5% after that date)"
        )

    if action == "SELL":
        console.print(
            "[dim]Tip: update holdings.md manually to reflect reduced quantity / exit.[/dim]"
        )


# ---------------------------------------------------------------------------
# watchlist command
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("ticker")
@click.argument("tier", type=click.Choice(["1", "2", "3"]))
@click.option("--reason", default="", help="Reason for watchlist addition")
def watchlist(ticker: str, tier: str, reason: str) -> None:
    """Show how to add TICKER to the watchlist.

    Watchlist entries are now managed automatically by the analysis pipeline
    and persisted to the SQLite database (investor.db).

    Run a full analysis instead:  investor analyze TICKER
    View watchlist alerts:         investor watchlist-alerts
    View all watchlist tickers:    investor db-recommendations --type WATCHLIST
    """
    console.print(
        f"[yellow]ℹ Watchlist is now managed automatically by the analysis pipeline.[/yellow]\n"
        f"Run [bold]investor analyze {ticker.upper()}[/bold] to evaluate and add to watchlist.\n"
        f"View current watchlist: [bold]investor db-recommendations --type WATCHLIST[/bold]\n"
        f"Check entry alerts:     [bold]investor watchlist-alerts[/bold]"
    )


# ---------------------------------------------------------------------------
# correction-scan command
# ---------------------------------------------------------------------------


@cli.command("correction-scan")
def correction_scan() -> None:
    """Check current market mode and show Tier-1 watchlist entry opportunities."""

    async def _run() -> tuple:
        from src.agent.mode_detector import detect_mode
        from src.api.nse import NSEClient
        from src.db.repository import get_watchlist_with_targets
        from src.models import AnalysisState

        nifty_state = AnalysisState(ticker="NIFTY")
        async with NSEClient() as nse_client:
            mode = await detect_mode(nse_client, nifty_state)
        tier1_rows = await get_watchlist_with_targets(settings.db_path)
        tier1_rows = [r for r in tier1_rows if (r.get("watchlist_tier") or 99) == 1]
        return mode, nifty_state, tier1_rows

    mode, nifty_state, tier1_rows = asyncio.run(_run())

    console.print(f"\n[bold]Market Mode: {mode.value.upper()}[/bold]")
    if nifty_state.nifty_level:
        console.print(f"Nifty 50: {nifty_state.nifty_level:,.2f}")
    if nifty_state.nifty_52w_high:
        console.print(f"52W High: {nifty_state.nifty_52w_high:,.2f}")
    if nifty_state.nifty_decline_pct:
        console.print(f"Decline from peak: {nifty_state.nifty_decline_pct:.2f}%")

    # Show Tier 1 watchlist from SQLite
    if tier1_rows:
        console.print("\n[bold cyan]Tier 1 Watchlist (from DB):[/bold cyan]")
        t1_table = Table(show_header=True, header_style="bold")
        t1_table.add_column("Ticker", style="bold", width=12)
        t1_table.add_column("Company", width=24)
        t1_table.add_column("Analysis Date", width=13)
        t1_table.add_column("CMP @ Analysis", justify="right", width=15)
        t1_table.add_column("Target Buy", justify="right", width=12)
        t1_table.add_column("Req MoS%", justify="right", width=10)
        for r in tier1_rows:
            cmp_val = r.get("cmp_at_analysis")
            target = r.get("target_buy_price")
            t1_table.add_row(
                r.get("ticker", ""),
                r.get("company_name", "") or "",
                r.get("analysis_date", ""),
                f"₹{cmp_val:.2f}" if cmp_val else "—",
                f"₹{target:.2f}" if target else "—",
                f"{r.get('required_mos_pct', '')}%",
            )
        console.print(t1_table)
        console.print(
            "[dim]Run [bold]investor watchlist-alerts[/bold] to compare live prices against targets.[/dim]"
        )
    else:
        console.print("\n[dim]No Tier 1 watchlist entries in database yet.[/dim]")


# ---------------------------------------------------------------------------
# scan command
# ---------------------------------------------------------------------------


@cli.command("scan")
@click.option(
    "--index",
    default="NIFTY 500",
    show_default=True,
    help="NSE index to scan (e.g. 'NIFTY 50', 'NIFTY 100', 'NIFTY 500')",
)
@click.option(
    "--top",
    default=10,
    show_default=True,
    help="Max number of full analyses to run after pre-screening",
)
@click.option(
    "--min-score",
    default=5,
    show_default=True,
    help="Minimum Step-0 pre-screen score (0–9) required to proceed to full analysis",
)
@click.option(
    "--prescreen-only",
    is_flag=True,
    default=False,
    help="Stop after Step-0 pre-screen; skip full 9-step analysis",
)
@click.option(
    "--concurrency",
    default=None,
    show_default=True,
    help=(
        "Max parallel HTTP requests during pre-screen. "
        "Defaults to settings.scan_concurrency (8). "
        "Use 3–5 on a cold scan; 10–15 once the SQLite warm cache is seeded."
    ),
)
def scan(
    index: str,
    top: int,
    min_score: int,
    prescreen_only: bool,
    concurrency: int | None,
) -> None:
    """Screen a stock universe and surface the best investment opportunities.

    Phase 1 — Pre-screen every stock in the index using deterministic Step 0
    (no Claude calls, no cost). Filters by minimum score.

    Phase 2 — Run the full 9-step pipeline on the shortlisted candidates.

    Examples:\n
      investor scan --index "NIFTY 50" --prescreen-only\n
      investor scan --index "NIFTY 100" --top 5 --min-score 7\n
      investor scan --concurrency 3
    """
    from src.agent.batch_scanner import BatchScanner

    async def _run():
        scanner = BatchScanner(concurrency=concurrency)
        return await scanner.scan(
            index=index,
            prescreen_min_score=min_score,
            max_full_analyses=top,
            prescreen_only=prescreen_only,
        )

    with console.status(f"[bold green]Scanning {index}...[/bold green]"):
        summaries, results = asyncio.run(_run())

    # --- Pre-screen table ---
    from rich.table import Table

    passed = [s for s in summaries if not s.error and s.score >= min_score]
    failed = [s for s in summaries if not s.error and s.score < min_score]
    errors = [s for s in summaries if s.error]

    ps_table = Table(
        title=f"Pre-Screen Results — {index}",
        show_header=True,
        header_style="bold cyan",
    )
    ps_table.add_column("Ticker", style="bold")
    ps_table.add_column("Score", justify="center")
    ps_table.add_column("Gate")
    ps_table.add_column("Failed Metrics", style="dim")

    gate_colours = {
        "pass_green": "green",
        "pass_conditional": "yellow",
        "fail": "red",
        "not_run": "dim",
    }

    for s in sorted(passed, key=lambda x: x.score, reverse=True):
        colour = gate_colours.get(s.gate.value, "white")
        ps_table.add_row(
            s.ticker,
            f"[{colour}]{s.score}/9[/{colour}]",
            f"[{colour}]{s.gate.value.upper()}[/{colour}]",
            ", ".join(s.failed_metrics[:3]) + ("…" if len(s.failed_metrics) > 3 else ""),
        )

    console.print(ps_table)
    console.print(
        f"\n[bold]Pre-screen summary:[/bold] "
        f"{len(passed)} passed / {len(failed)} failed / {len(errors)} errors "
        f"out of {len(summaries)} tickers"
    )

    if prescreen_only or not results:
        if not prescreen_only and passed:
            console.print(
                "\n[yellow]No full analyses completed "
                "(all candidates may have been rejected at Step 0).[/yellow]"
            )
        return

    # --- Full analysis table ---
    fa_table = Table(
        title="Full Analysis Results (ranked)",
        show_header=True,
        header_style="bold cyan",
    )
    fa_table.add_column("Ticker", style="bold")
    fa_table.add_column("Recommendation")
    fa_table.add_column("Conviction")
    fa_table.add_column("MoS %", justify="right")
    fa_table.add_column("Governance", justify="center")
    fa_table.add_column("Terminated At")

    rec_colours = {"BUY": "green", "WATCHLIST": "yellow", "PEER_SWITCH": "cyan", "REJECT": "red"}

    for state in results:
        rec = state.recommendation_type or "REJECT"
        colour = rec_colours.get(rec, "white")
        mos = (
            f"{state.valuation.margin_of_safety_pct:.1f}%"
            if state.valuation and state.valuation.margin_of_safety_pct is not None
            else "—"
        )
        gov = (
            f"{state.governance.score}/15"
            if state.governance
            else "—"
        )
        term = (
            f"Step {state.terminated_at_step}"
            if state.terminated_at_step is not None
            else "—"
        )
        fa_table.add_row(
            state.ticker,
            f"[{colour}]{rec}[/{colour}]",
            state.conviction.value.upper() if state.conviction else "—",
            mos,
            gov,
            term,
        )

    console.print(fa_table)

    # Print full report for BUY recommendations
    buy_states = [s for s in results if s.recommendation_type == "BUY"]
    if buy_states:
        console.print(f"\n[bold green]Found {len(buy_states)} BUY recommendation(s):[/bold green]")
        for state in buy_states:
            if state.formatted_output:
                console.print(state.formatted_output)
    else:
        console.print("\n[yellow]No BUY recommendations from this scan.[/yellow]")

    # Save reports and update watchlist/rejection tracker
    reports_dir = Path("analysis") / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    from datetime import date
    today = date.today().isoformat()
    for state in results:
        if state.formatted_output:
            path = reports_dir / f"{state.ticker}_{today}.md"
            path.write_text(state.formatted_output, encoding="utf-8")
    if results:
        console.print(f"[dim]Reports saved → {reports_dir}/[/dim]")


# ---------------------------------------------------------------------------
# portfolio-review command
# ---------------------------------------------------------------------------


@cli.command("portfolio-review")
@click.option(
    "--concurrency",
    default=2,
    show_default=True,
    help="Max parallel analyses (keep low — each uses Claude API)",
)
@click.option(
    "--save/--no-save",
    default=True,
    help="Save individual reports to analysis/reports/",
)
def portfolio_review(concurrency: int, save: bool) -> None:
    """Run a full 9-step analysis on every holding and recommend HOLD or SELL.

    Reads portfolio/holdings.md, analyses each ticker via the investment
    pipeline, then prints a ranked action table:

    \b
    - BUY    → HOLD + consider adding on dips
    - WATCHLIST → HOLD with caution (set alert)
    - REJECT → SELL — thesis has broken down

    Reports are saved to analysis/reports/.
    """
    import asyncio as _asyncio
    import time

    tracker = PortfolioTracker()
    holdings = tracker.get_holdings()

    if not holdings:
        console.print(
            "[yellow]No holdings found. "
            "Add your Zerodha positions to portfolio/holdings.md first.[/yellow]"
        )
        return

    tickers = [h["ticker"] for h in holdings]
    holding_map = {h["ticker"]: h for h in holdings}

    console.print(
        f"[bold cyan]Portfolio Review — {len(tickers)} holdings[/bold cyan]\n"
        f"[dim]Running full 9-step analysis on: {', '.join(tickers)}[/dim]\n"
    )

    async def _run_all():
        import asyncio
        sem = asyncio.Semaphore(concurrency)

        async def _analyse_one(ticker: str):
            async with sem:
                pipeline = InvestmentPipeline()
                return await pipeline.analyze(ticker)

        tasks = [_analyse_one(t) for t in tickers]
        return await asyncio.gather(*tasks, return_exceptions=True)

    start = time.monotonic()
    with console.status("[bold green]Analysing portfolio...[/bold green]"):
        raw_results = asyncio.run(_run_all())
    elapsed = time.monotonic() - start

    # Build result table
    action_table = Table(
        title="Portfolio Review — Hold / Sell Actions",
        show_header=True,
        header_style="bold cyan",
        show_lines=True,
    )
    action_table.add_column("Ticker", style="bold", width=12)
    action_table.add_column("Company", width=22)
    action_table.add_column("Avg Cost", justify="right", width=10)
    action_table.add_column("Qty", justify="right", width=6)
    action_table.add_column("Alloc %", justify="right", width=8)
    action_table.add_column("Pipeline", justify="center", width=12)
    action_table.add_column("Gov /15", justify="center", width=8)
    action_table.add_column("MoS %", justify="right", width=8)
    action_table.add_column("ACTION", style="bold", width=14)
    action_table.add_column("Reason", width=30)

    action_colours = {
        "HOLD_ADD": "green",
        "HOLD": "cyan",
        "HOLD_WATCH": "yellow",
        "SELL": "red",
    }

    holds, sells, errors = [], [], []

    for ticker, result in zip(tickers, raw_results):
        h = holding_map[ticker]

        if isinstance(result, Exception):
            errors.append(ticker)
            action_table.add_row(
                ticker,
                h.get("company_name", ""),
                f"₹{h['avg_cost']:.2f}",
                str(h["quantity"]),
                f"{h['allocation_pct']:.1f}%",
                "[red]ERROR[/red]",
                "—", "—",
                "[red]SKIP[/red]",
                str(result)[:30],
            )
            continue

        state: AnalysisState = result
        rec = state.recommendation_type or "REJECT"
        gov = f"{state.governance.score}/15" if state.governance else "—"
        mos = (
            f"{state.valuation.margin_of_safety_pct:.1f}%"
            if state.valuation and state.valuation.margin_of_safety_pct is not None
            else "—"
        )

        # Determine action
        if rec == "BUY":
            action = "HOLD_ADD"
            reason = "Thesis intact — add on dips"
            holds.append(ticker)
        elif rec == "WATCHLIST":
            action = "HOLD_WATCH"
            tier = state.watchlist_tier or 2
            reason = f"Tier {tier} — set re-entry alert"
            holds.append(ticker)
        else:
            action = "SELL"
            reason = (state.termination_reason or "Failed pipeline gate")[:30]
            sells.append(ticker)

        colour = action_colours.get(action, "white")
        action_table.add_row(
            ticker,
            h.get("company_name", ticker),
            f"₹{h['avg_cost']:.2f}",
            str(h["quantity"]),
            f"{h['allocation_pct']:.1f}%",
            f"[{'green' if rec == 'BUY' else 'yellow' if rec == 'WATCHLIST' else 'red'}]{rec}[/]",
            gov,
            mos,
            f"[{colour}]{action}[/{colour}]",
            reason,
        )

        # Save individual report
        if save and state.formatted_output:
            reports_dir = Path("analysis") / "reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            today = date.today().isoformat()
            path = reports_dir / f"{ticker}_{today}_portfolio_review.md"
            path.write_text(state.formatted_output, encoding="utf-8")

    console.print(action_table)

    # Count how many non-error results were saved (pipeline saves automatically)
    saved_count = sum(1 for r in raw_results if not isinstance(r, Exception))

    # Summary
    console.print(
        f"\n[bold]Summary:[/bold] "
        f"[green]{len(holds)} HOLD[/green]  |  "
        f"[red]{len(sells)} SELL[/red]"
        + (f"  |  [yellow]{len(errors)} ERROR[/yellow]" if errors else "")
        + f"  |  elapsed {elapsed:.0f}s"
    )
    console.print(
        f"[dim]Results saved to SQLite: {saved_count} analyses · {settings.db_path}[/dim]\n"
        "[dim]Use 'investor db-summary' to view all results · "
        "'investor db-history TICKER' for trend[/dim]"
    )

    if sells:
        console.print(
            f"\n[bold red]⚠ Sell candidates:[/bold red] {', '.join(sells)}\n"
            "Review individual reports in analysis/reports/ before acting.\n"
            "[dim]Tax note: check LTCG eligibility (>1Y = 12.5% on gains > ₹1.25L)[/dim]"
        )

    if errors:
        console.print(
            f"\n[yellow]Could not analyse: {', '.join(errors)}. "
            "Re-run individually with: investor analyze TICKER[/yellow]"
        )


# ---------------------------------------------------------------------------
# db-history command
# ---------------------------------------------------------------------------


@cli.command("db-history")
@click.argument("ticker")
@click.option("--limit", default=10, show_default=True, help="Number of past analyses to show")
def db_history(ticker: str, limit: int) -> None:
    """Show analysis history for TICKER from the local SQLite database.

    Example: investor db-history RELIANCE --limit 5
    """
    import asyncio as _asyncio

    from src.db.repository import get_analysis_history

    rows = _asyncio.run(get_analysis_history(settings.db_path, ticker.upper(), limit))
    if not rows:
        console.print(f"[yellow]No history found for {ticker.upper()} in {settings.db_path}[/yellow]")
        return

    table = Table(
        title=f"Analysis History — {ticker.upper()}",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Date")
    table.add_column("Recommendation")
    table.add_column("Conviction")
    table.add_column("Sector")
    table.add_column("Gov /15", justify="center")
    table.add_column("Fin /7", justify="center")
    table.add_column("MoS %", justify="right")
    table.add_column("Terminated At")

    rec_colours = {"BUY": "green", "WATCHLIST": "yellow", "PEER_SWITCH": "cyan", "REJECT": "red"}

    for row in rows:
        rec = row.get("recommendation") or "—"
        colour = rec_colours.get(rec, "white")
        mos = f"{row['mos_pct']:.1f}%" if row.get("mos_pct") is not None else "—"
        term = f"Step {row['terminated_at_step']}" if row.get("terminated_at_step") is not None else "—"
        table.add_row(
            row.get("analysis_date", ""),
            f"[{colour}]{rec}[/{colour}]",
            row.get("conviction") or "—",
            row.get("sector_name") or "—",
            str(row.get("governance_score") or "—"),
            str(row.get("financial_score") or "—"),
            mos,
            term,
        )

    console.print(table)


# ---------------------------------------------------------------------------
# db-summary command
# ---------------------------------------------------------------------------


@cli.command("db-summary")
def db_summary() -> None:
    """Show a summary of all analyses in the local SQLite database.

    Displays all analysed tickers ranked by recommendation type.
    """
    import asyncio as _asyncio

    from src.db.repository import get_summary

    rows = _asyncio.run(get_summary(settings.db_path))
    if not rows:
        console.print(f"[yellow]No analyses found in {settings.db_path}[/yellow]")
        return

    table = Table(
        title="Database Summary — All Analyses",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Ticker", style="bold")
    table.add_column("Company")
    table.add_column("Date")
    table.add_column("Recommendation")
    table.add_column("Conviction")
    table.add_column("Sector")
    table.add_column("Cap")
    table.add_column("MoS %", justify="right")
    table.add_column("Terminated At")

    rec_colours = {"BUY": "green", "WATCHLIST": "yellow", "PEER_SWITCH": "cyan", "REJECT": "red"}

    for row in rows:
        rec = row.get("recommendation") or "—"
        colour = rec_colours.get(rec, "white")
        mos = f"{row['mos_pct']:.1f}%" if row.get("mos_pct") is not None else "—"
        term = f"Step {row['terminated_at_step']}" if row.get("terminated_at_step") is not None else "—"
        table.add_row(
            row.get("ticker", ""),
            (row.get("company_name") or "")[:25],
            row.get("analysis_date", ""),
            f"[{colour}]{rec}[/{colour}]",
            row.get("conviction") or "—",
            row.get("sector_name") or "—",
            row.get("cap_size") or "—",
            mos,
            term,
        )

    console.print(table)
    console.print(f"\n[dim]Total: {len(rows)} analyses in {settings.db_path}[/dim]")


# ---------------------------------------------------------------------------
# db-recommendations command
# ---------------------------------------------------------------------------


@cli.command("db-recommendations")
@click.option(
    "--type",
    "rec_type",
    default="BUY",
    type=click.Choice(["BUY", "WATCHLIST", "PEER_SWITCH", "REJECT"], case_sensitive=False),
    show_default=True,
    help="Recommendation type to filter",
)
def db_recommendations(rec_type: str) -> None:
    """List all stocks with a given recommendation from the SQLite database.

    \b
    Examples:
      investor db-recommendations --type BUY
      investor db-recommendations --type WATCHLIST
    """
    import asyncio as _asyncio

    from src.db.repository import list_recommendations

    rows = _asyncio.run(list_recommendations(settings.db_path, rec_type.upper()))
    if not rows:
        console.print(f"[yellow]No {rec_type.upper()} recommendations in {settings.db_path}[/yellow]")
        return

    rec_colours = {"BUY": "green", "WATCHLIST": "yellow", "PEER_SWITCH": "cyan", "REJECT": "red"}
    colour = rec_colours.get(rec_type.upper(), "white")

    table = Table(
        title=f"[{colour}]{rec_type.upper()}[/{colour}] Recommendations",
        show_header=True,
        header_style="bold cyan",
        show_lines=False,
    )
    table.add_column("Ticker", style="bold", width=12)
    table.add_column("Company", width=28)
    table.add_column("Date", width=12)
    table.add_column("Conviction", width=10)
    table.add_column("Sector", width=20)
    table.add_column("Cap", width=10)
    table.add_column("Gov /15", justify="center", width=8)
    table.add_column("Fin /7", justify="center", width=7)
    table.add_column("MoS %", justify="right", width=8)
    table.add_column("DCF ₹", justify="right", width=10)

    for row in rows:
        mos = f"{row['mos_pct']:.1f}%" if row.get("mos_pct") is not None else "—"
        dcf = f"₹{row['dcf_intrinsic_weighted']:.0f}" if row.get("dcf_intrinsic_weighted") else "—"
        table.add_row(
            row.get("ticker", ""),
            (row.get("company_name") or "")[:28],
            row.get("analysis_date", ""),
            row.get("conviction") or "—",
            row.get("sector_name") or "—",
            row.get("cap_size") or "—",
            str(row.get("governance_score") or "—"),
            str(row.get("financial_score") or "—"),
            mos,
            dcf,
        )

    console.print(table)
    console.print(f"\n[dim]{len(rows)} stocks · {settings.db_path}[/dim]")


# ---------------------------------------------------------------------------
# db-snapshots command
# ---------------------------------------------------------------------------


@cli.command("db-snapshots")
@click.argument("ticker")
@click.option(
    "--type",
    "data_type",
    default=None,
    type=click.Choice(["quote", "financials", "governance", "valuation"]),
    help="Filter by data type (default: show all)",
)
def db_snapshots(ticker: str, data_type: str | None) -> None:
    """Show raw data snapshots stored for TICKER in the SQLite database.

    \b
    Examples:
      investor db-snapshots RELIANCE
      investor db-snapshots RELIANCE --type financials
    """
    import asyncio as _asyncio
    import json

    import aiosqlite

    ticker = ticker.upper()

    async def _fetch():
        from src.db.repository import init_db as _init_db
        await _init_db(settings.db_path)
        async with aiosqlite.connect(settings.db_path) as db:
            db.row_factory = aiosqlite.Row
            if data_type:
                sql = (
                    "SELECT snapshot_date, data_type, source, data_json FROM data_snapshots "
                    "WHERE ticker = ? AND data_type = ? ORDER BY snapshot_date DESC, data_type"
                )
                params = (ticker, data_type)
            else:
                sql = (
                    "SELECT snapshot_date, data_type, source, data_json FROM data_snapshots "
                    "WHERE ticker = ? ORDER BY snapshot_date DESC, data_type"
                )
                params = (ticker,)
            async with db.execute(sql, params) as cursor:
                return [dict(r) async for r in cursor]

    rows = _asyncio.run(_fetch())

    if not rows:
        msg = f"[yellow]No snapshots for {ticker}"
        if data_type:
            msg += f" ({data_type})"
        console.print(msg + f" in {settings.db_path}[/yellow]")
        return

    table = Table(
        title=f"Data Snapshots — {ticker}",
        show_header=True,
        header_style="bold cyan",
        show_lines=True,
    )
    table.add_column("Date", width=12)
    table.add_column("Type", width=12)
    table.add_column("Source", width=12)
    table.add_column("Fields (preview)", width=60)

    for row in rows:
        try:
            data = json.loads(row["data_json"])
            # Show only a compact preview of the stored fields
            keys = list(data.keys())
            preview_keys = keys[:6]
            preview = ", ".join(
                f"{k}={data[k]!r}" for k in preview_keys if data[k] is not None
            )
            if len(keys) > 6:
                preview += f"  … +{len(keys) - 6} more"
        except Exception:
            preview = row["data_json"][:80]

        table.add_row(
            row.get("snapshot_date", ""),
            row.get("data_type", ""),
            row.get("source", ""),
            preview,
        )

    console.print(table)
    console.print(f"\n[dim]{len(rows)} snapshot(s) for {ticker} · {settings.db_path}[/dim]")


# ---------------------------------------------------------------------------
# watchlist-alerts command  (P2-2)
# ---------------------------------------------------------------------------


@cli.command("watchlist-alerts")
def watchlist_alerts() -> None:
    """Compare every WATCHLIST ticker's DCF target buy price against live CMP.

    Fetches current prices via Yahoo Finance and flags tickers that have
    entered (or are close to) their required margin-of-safety zone.

    \b
    Alert levels:
      🟢 ENTER ZONE   — CMP ≤ target buy price (MoS met — run full analysis now)
      🟡 APPROACHING  — CMP within 10 % above target
      ⚪ MONITORING   — CMP still above target zone
    """
    import asyncio as _asyncio

    from src.db.repository import get_watchlist_with_targets

    rows = _asyncio.run(get_watchlist_with_targets(settings.db_path))

    if not rows:
        console.print(
            f"[yellow]No WATCHLIST tickers found in {settings.db_path}. "
            "Run 'investor analyze TICKER' to add stocks to the watchlist.[/yellow]"
        )
        return

    async def _fetch_prices(tickers: list[str]) -> dict[str, float | None]:
        """Fetch live CMP for a list of tickers via YFinance."""
        from src.api.yfinance_client import YFinanceClient

        async with YFinanceClient() as yf_client:
            results: dict[str, float | None] = {}
            for t in tickers:
                quote = await yf_client.get_stock_quote(t)
                results[t] = quote.cmp if quote else None
        return results

    tickers = [r["ticker"] for r in rows]
    with console.status("[bold green]Fetching live prices...[/bold green]"):
        live_prices = asyncio.run(_fetch_prices(tickers))

    table = Table(
        title="Watchlist Alerts — Live CMP vs DCF Target",
        show_header=True,
        header_style="bold cyan",
        show_lines=True,
    )
    table.add_column("Ticker", style="bold", width=10)
    table.add_column("Company", width=22)
    table.add_column("Tier", justify="center", width=5)
    table.add_column("Last Analysis", width=12)
    table.add_column("CMP @ Analysis", justify="right", width=14)
    table.add_column("Live CMP", justify="right", width=10)
    table.add_column("Target Buy ₹", justify="right", width=12)
    table.add_column("Gap %", justify="right", width=8)
    table.add_column("Status", width=16)

    enter_zone, approaching, monitoring = [], [], []

    for row in rows:
        ticker = row["ticker"]
        live_cmp = live_prices.get(ticker)
        target = row.get("target_buy_price")
        cmp_at_analysis = row.get("cmp_at_analysis")
        tier = row.get("watchlist_tier") or "—"

        live_str = f"₹{live_cmp:.2f}" if live_cmp else "[dim]N/A[/dim]"
        target_str = f"₹{target:.2f}" if target else "—"
        old_cmp_str = f"₹{cmp_at_analysis:.2f}" if cmp_at_analysis else "—"

        if live_cmp and target:
            gap_pct = (live_cmp - target) / target * 100
            gap_str = f"{gap_pct:+.1f}%"
            if gap_pct <= 0:
                status = "[green]🟢 ENTER ZONE[/green]"
                enter_zone.append(ticker)
            elif gap_pct <= 10:
                status = "[yellow]🟡 APPROACHING[/yellow]"
                approaching.append(ticker)
            else:
                status = "[dim]⚪ MONITORING[/dim]"
                monitoring.append(ticker)
        else:
            gap_str = "—"
            status = "[dim]⚪ NO TARGET[/dim]"
            monitoring.append(ticker)

        table.add_row(
            ticker,
            (row.get("company_name") or "")[:22],
            str(tier),
            row.get("analysis_date", ""),
            old_cmp_str,
            live_str,
            target_str,
            gap_str,
            status,
        )

    console.print(table)

    summary_parts = []
    if enter_zone:
        summary_parts.append(f"[green]{len(enter_zone)} in buy zone: {', '.join(enter_zone)}[/green]")
    if approaching:
        summary_parts.append(f"[yellow]{len(approaching)} approaching: {', '.join(approaching)}[/yellow]")
    if monitoring:
        summary_parts.append(f"[dim]{len(monitoring)} monitoring[/dim]")
    console.print("\n" + "  |  ".join(summary_parts))

    if enter_zone:
        console.print(
            f"\n[bold green]⚡ Action required:[/bold green] "
            f"Run full analysis on: {', '.join(f'investor analyze {t}' for t in enter_zone)}"
        )
    console.print(
        f"\n[dim]Prices via Yahoo Finance (~15-20 min delayed). "
        f"Targets derived from DCF intrinsic at time of last analysis.[/dim]"
    )


# ---------------------------------------------------------------------------
# surveillance command  (P2-1)
# ---------------------------------------------------------------------------


@cli.command("surveillance")
@click.option(
    "--days-since",
    default=30,
    show_default=True,
    help="Flag tickers whose last analysis is older than this many days",
)
def surveillance(days_since: int) -> None:
    """Light-touch surveillance sweep across all tracked BUY and WATCHLIST positions.

    For each ticker in the database with a BUY or WATCHLIST recommendation:
      1. Fetches live CMP via Yahoo Finance
      2. Checks price drift since last analysis (big drops may mean thesis change)
      3. Flags stale analyses (> --days-since days old)
      4. Highlights watchlist tickers that have entered their buy zone

    This is a quick, zero-LLM scan (~5s for 30 tickers).  When it flags
    a ticker for re-analysis, run: investor analyze TICKER

    \b
    Drift alerts:
      ▲ CMP up > 20 % since analysis  → may no longer offer MoS; re-evaluate
      ▼ CMP down > 20 % since analysis → possible buying opportunity or thesis break
    """
    import asyncio as _asyncio
    from datetime import datetime as _dt

    from src.db.repository import get_all_tracked_tickers

    rows = _asyncio.run(get_all_tracked_tickers(settings.db_path))

    if not rows:
        console.print(
            f"[yellow]No BUY or WATCHLIST tickers found in {settings.db_path}.[/yellow]"
        )
        return

    async def _fetch_prices(tickers: list[str]) -> dict[str, float | None]:
        from src.api.yfinance_client import YFinanceClient

        async with YFinanceClient() as yf_client:
            out: dict[str, float | None] = {}
            for t in tickers:
                quote = await yf_client.get_stock_quote(t)
                out[t] = quote.cmp if quote else None
        return out

    tickers = [r["ticker"] for r in rows]
    with console.status("[bold green]Fetching live prices for surveillance...[/bold green]"):
        live_prices = asyncio.run(_fetch_prices(tickers))

    today = date.today()

    table = Table(
        title="Surveillance — All BUY / WATCHLIST Positions",
        show_header=True,
        header_style="bold cyan",
        show_lines=False,
    )
    table.add_column("Ticker", style="bold", width=10)
    table.add_column("Rec", width=10)
    table.add_column("Last Analysis", width=12)
    table.add_column("Stale?", justify="center", width=8)
    table.add_column("CMP @ Analysis", justify="right", width=14)
    table.add_column("Live CMP", justify="right", width=10)
    table.add_column("Drift %", justify="right", width=9)
    table.add_column("Target ₹", justify="right", width=10)
    table.add_column("Zone?", justify="center", width=8)
    table.add_column("Action", width=24)

    re_analyse = []
    enter_zone = []

    for row in rows:
        ticker = row["ticker"]
        rec = row.get("recommendation", "")
        analysis_date_str = row.get("analysis_date", "")
        cmp_prev = row.get("cmp_at_analysis")
        target = row.get("target_buy_price")
        live_cmp = live_prices.get(ticker)

        # Staleness
        try:
            analysis_date = _dt.fromisoformat(analysis_date_str).date()
            age_days = (today - analysis_date).days
            stale = age_days > days_since
        except Exception:
            age_days = 999
            stale = True
        stale_str = f"[red]{age_days}d[/red]" if stale else f"[green]{age_days}d[/green]"

        # Price drift
        drift_str = "—"
        drift_action = ""
        if live_cmp and cmp_prev and cmp_prev > 0:
            drift_pct = (live_cmp - cmp_prev) / cmp_prev * 100
            drift_str = f"{drift_pct:+.1f}%"
            if drift_pct > 20:
                drift_str = f"[yellow]{drift_str}[/yellow]"
                drift_action = "↑ Re-evaluate MoS"
            elif drift_pct < -20:
                drift_str = f"[cyan]{drift_str}[/cyan]"
                drift_action = "↓ Opportunity / thesis?"

        # Watchlist zone check
        zone_str = "—"
        if rec == "WATCHLIST" and live_cmp and target:
            gap = (live_cmp - target) / target * 100
            if gap <= 0:
                zone_str = "[green]✓ IN[/green]"
                enter_zone.append(ticker)
            elif gap <= 10:
                zone_str = "[yellow]~10%[/yellow]"
            else:
                zone_str = f"[dim]+{gap:.0f}%[/dim]"

        # Recommended action
        if stale:
            action = "[yellow]Re-run analysis (stale)[/yellow]"
            re_analyse.append(ticker)
        elif drift_action:
            action = drift_action
            re_analyse.append(ticker)
        elif zone_str.startswith("[green]"):
            action = "[green]Run full analysis now[/green]"
        else:
            action = "[dim]Continue monitoring[/dim]"

        rec_colour = "green" if rec == "BUY" else "yellow"
        live_str = f"₹{live_cmp:.2f}" if live_cmp else "[dim]N/A[/dim]"
        prev_str = f"₹{cmp_prev:.2f}" if cmp_prev else "—"
        target_str = f"₹{target:.2f}" if target else "—"

        table.add_row(
            ticker,
            f"[{rec_colour}]{rec}[/{rec_colour}]",
            analysis_date_str,
            stale_str,
            prev_str,
            live_str,
            drift_str,
            target_str,
            zone_str,
            action,
        )

    console.print(table)

    # Summary actions
    if enter_zone:
        console.print(
            f"\n[bold green]🟢 {len(enter_zone)} ticker(s) in buy zone:[/bold green] "
            + ", ".join(enter_zone)
        )
    if re_analyse:
        unique_reanalyse = list(dict.fromkeys(re_analyse))  # dedupe, preserve order
        console.print(
            f"\n[bold yellow]⚡ {len(unique_reanalyse)} ticker(s) need re-analysis:[/bold yellow] "
            + ", ".join(f"[bold]{t}[/bold]" for t in unique_reanalyse[:10])
        )
        console.print(
            "[dim]Run: " + "  |  ".join(f"investor analyze {t}" for t in unique_reanalyse[:5])
            + ("[dim]  …" if len(unique_reanalyse) > 5 else "") + "[/dim]"
        )

    console.print(
        f"\n[dim]Prices via Yahoo Finance (~15-20 min delayed). "
        f"Stale threshold: {days_since} days. "
        f"DB: {settings.db_path}[/dim]"
    )


if __name__ == "__main__":
    cli()
