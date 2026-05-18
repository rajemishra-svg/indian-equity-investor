"""CLI entry point — Indian Equity Long-Term Investor."""
from __future__ import annotations

import asyncio
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

    _post_pipeline_actions(state)


def _post_pipeline_actions(state: AnalysisState) -> None:
    """Update watchlist / rejection tracker after any pipeline run."""
    tracker = PortfolioTracker()
    if state.recommendation_type == "REJECT" and state.terminated_at_step is not None:
        reasons = [state.termination_reason or "Gate failure"]
        if state.governance and state.governance.immediate_triggers:
            reasons.extend(state.governance.immediate_triggers)
        tracker.add_rejection(
            ticker=state.ticker,
            step=state.terminated_at_step,
            reasons=reasons,
            re_eval_condition="Review when underlying cause is resolved",
        )
    elif state.recommendation_type == "WATCHLIST":
        tier = int(state.watchlist_tier) if state.watchlist_tier else 2
        tracker.add_to_watchlist(ticker=state.ticker, tier=tier, analysis_result=state)


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
# watchlist command
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("ticker")
@click.argument("tier", type=click.Choice(["1", "2", "3"]))
@click.option("--reason", default="", help="Reason for watchlist addition")
def watchlist(ticker: str, tier: str, reason: str) -> None:
    """Add TICKER to the specified watchlist TIER.

    Example: investor watchlist HDFC 1 --reason "Valuation too expensive now"
    """
    tracker = PortfolioTracker()
    tracker.add_to_watchlist(ticker=ticker.upper(), tier=int(tier), reason=reason)
    console.print(
        f"[green]✓ {ticker.upper()} added to Tier {tier} watchlist[/green]"
    )


# ---------------------------------------------------------------------------
# correction-scan command
# ---------------------------------------------------------------------------


@cli.command("correction-scan")
def correction_scan() -> None:
    """Check current market mode and show Tier-1 watchlist status."""

    async def _run() -> tuple:
        from src.agent.mode_detector import detect_mode
        from src.api.nse import NSEClient
        from src.models import AnalysisState

        state = AnalysisState(ticker="NIFTY")
        async with NSEClient() as nse_client:
            mode = await detect_mode(nse_client, state)
        return mode, state

    mode, state = asyncio.run(_run())

    console.print(f"\n[bold]Market Mode: {mode.value.upper()}[/bold]")
    if state.nifty_level:
        console.print(f"Nifty 50: {state.nifty_level:,.2f}")
    if state.nifty_52w_high:
        console.print(f"52W High: {state.nifty_52w_high:,.2f}")
    if state.nifty_decline_pct:
        console.print(f"Decline from peak: {state.nifty_decline_pct:.2f}%")

    # Show Tier 1 watchlist
    tier1_path = Path("analysis") / "watchlist" / "tier1.md"
    if tier1_path.exists():
        console.print("\n[bold cyan]Tier 1 Watchlist:[/bold cyan]")
        console.print(tier1_path.read_text(encoding="utf-8"))


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
    default=5,
    show_default=True,
    help="Max parallel HTTP requests during pre-screen (keep ≤ 5 for Screener.in)",
)
def scan(
    index: str,
    top: int,
    min_score: int,
    prescreen_only: bool,
    concurrency: int,
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
        _post_pipeline_actions(state)
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

        _post_pipeline_actions(state)

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


if __name__ == "__main__":
    cli()
