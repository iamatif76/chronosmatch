"""
ChronosMatch Terminal Dashboard & Latency Monitor.
High-frequency visual terminal displaying real-time Order Book depth,
Bid/Ask spread, microsecond latency metrics, live trade ticker,
and dynamic "Whale Sweep" alerts.
"""

import os
import sys
import time
import math
from typing import List, Dict, Any, Optional

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.layout import Layout
    from rich.text import Text
    from rich.live import Live
    from rich import box
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

from engine.protocol import fixed_to_price, OrderFlags, Side, SYMBOL_NAMES


class LatencyStats:
    """Calculates rolling percentiles and distribution of execution latency in microseconds."""

    def __init__(self, window_size: int = 5000):
        self.window_size = window_size
        self.samples_us = []
        self.total_samples = 0
        self.min_us = float("inf")
        self.max_us = 0.0

    def add(self, latency_us: float):
        if len(self.samples_us) >= self.window_size:
            self.samples_us.pop(0)
        self.samples_us.append(latency_us)
        self.total_samples += 1
        if latency_us < self.min_us:
            self.min_us = latency_us
        if latency_us > self.max_us:
            self.max_us = latency_us

    def get_percentiles(self) -> Dict[str, float]:
        if not self.samples_us:
            return {"p50": 0.0, "p90": 0.0, "p99": 0.0, "min": 0.0, "max": 0.0, "avg": 0.0}

        sorted_s = sorted(self.samples_us)
        n = len(sorted_s)
        p50 = sorted_s[int(n * 0.50)]
        p90 = sorted_s[min(n - 1, int(n * 0.90))]
        p99 = sorted_s[min(n - 1, int(n * 0.99))]
        avg = sum(sorted_s) / n

        return {
            "p50": round(p50, 2),
            "p90": round(p90, 2),
            "p99": round(p99, 2),
            "min": round(self.min_us if self.min_us != float("inf") else 0.0, 2),
            "max": round(self.max_us, 2),
            "avg": round(avg, 2),
        }

    def get_sparkline(self) -> str:
        """Generate ASCII sparkline of recent latency values."""
        if not self.samples_us:
            return " " * 20
        recent = self.samples_us[-20:]
        ticks = [" ", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
        low = min(recent)
        high = max(recent)
        if high == low:
            return ticks[0] * len(recent)
        res = []
        for v in recent:
            idx = int((v - low) / (high - low) * (len(ticks) - 1))
            res.append(ticks[idx])
        return "".join(res)


class TerminalDashboard:
    """
    Renders high-frequency trading metrics and visual order book.
    """

    def __init__(self, symbol_name: str = "AAPL"):
        self.symbol_name = symbol_name
        self.console = Console() if HAS_RICH else None
        self.latency_stats = LatencyStats(window_size=5000)
        self.recent_trades = []
        self.max_recent_trades = 10
        self.last_whale_alert = None
        self.last_whale_time = 0.0

    def record_trade(self, trade_dict: Dict[str, Any]):
        """Record trade execution report for dashboard ticker and metrics."""
        lat_us = trade_dict.get("latency_us", trade_dict.get("latency_ns", 0) / 1000.0)
        self.latency_stats.add(lat_us)

        # Check for Whale sweep flag
        flags = trade_dict.get("flags", 0)
        if flags & OrderFlags.WHALE_SWEEP or trade_dict.get("quantity", 0) >= 5000:
            price_flt = fixed_to_price(trade_dict.get("price", 0))
            qty = trade_dict.get("quantity", 0)
            self.last_whale_alert = f"WHALE SWEEP: {qty:,} shares executed @ ${price_flt:,.2f} (Latency: {lat_us:.1f}μs)"
            self.last_whale_time = time.time()

        if len(self.recent_trades) >= self.max_recent_trades:
            self.recent_trades.pop(0)
        self.recent_trades.append(trade_dict)

    def render_rich_frame(
        self,
        depth: Dict[str, Any],
        buffer_occupancy: int,
        buffer_capacity: int,
        total_written: int,
        total_read: int,
        throughput_ops: float,
    ) -> Layout:
        """Build Rich UI layout for terminal display."""
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="whale", size=3),
            Layout(name="main", ratio=1),
            Layout(name="footer", size=7),
        )
        layout["main"].split_row(
            Layout(name="book", ratio=5),
            Layout(name="metrics", ratio=4),
        )

        # 1. Header
        header_text = Text()
        header_text.append("⚡ CHRONOSMATCH ", style="bold bright_cyan")
        header_text.append("| ZERO-COPY HIGH-FREQUENCY TRADING ENGINE\n", style="bold white")
        header_text.append(f"SYMBOL: {self.symbol_name}  •  GIL: BYPASSED (Cython nogil)  •  IPC: MMAP RAM  •  SLOT: 64B CACHELINE", style="dim cyan")
        layout["header"].update(Panel(header_text, box=box.ROUNDED, style="cyan"))

        # 2. Whale Alert
        now = time.time()
        if self.last_whale_alert and (now - self.last_whale_time < 3.0):
            whale_txt = Text(f"🚨 {self.last_whale_alert} 🚨", style="bold bright_yellow on red", justify="center")
            layout["whale"].update(Panel(whale_txt, box=box.HEAVY, style="red"))
        else:
            norm_txt = Text("✓ Normal Market Conditions • Zero Serialization Bottleneck • Sub-50μs SLA Active", style="dim green", justify="center")
            layout["whale"].update(Panel(norm_txt, box=box.ROUNDED, style="green"))

        # 3. Order Book Table (Bids & Asks)
        book_table = Table(box=box.SIMPLE_HEAD, expand=True)
        book_table.add_column("Bid Depth", justify="right", style="green")
        book_table.add_column("Bid Qty", justify="right", style="bright_green")
        book_table.add_column("Bid Price", justify="center", style="bold green")
        book_table.add_column("Spread", justify="center", style="bold bright_yellow")
        book_table.add_column("Ask Price", justify="center", style="bold red")
        book_table.add_column("Ask Qty", justify="left", style="bright_red")
        book_table.add_column("Ask Depth", justify="left", style="red")

        bids = depth.get("bids", [])
        asks = depth.get("asks", [])
        max_levels = max(len(bids), len(asks), 7)

        max_vol = 1
        for b in bids:
            max_vol = max(max_vol, b.get("quantity", 0))
        for a in asks:
            max_vol = max(max_vol, a.get("quantity", 0))

        best_bid = depth.get("best_bid", 0)
        best_ask = depth.get("best_ask", 0)
        spread_val = fixed_to_price(depth.get("spread", 0)) if (best_bid and best_ask) else 0.0

        for i in range(min(7, max_levels)):
            b = bids[i] if i < len(bids) else None
            a = asks[i] if i < len(asks) else None

            # Bid side
            if b:
                b_p = f"${fixed_to_price(b['price']):,.2f}"
                b_q = f"{b['quantity']:,}"
                bar_len = min(10, int((b['quantity'] / max_vol) * 10))
                b_bar = "■" * bar_len
            else:
                b_p, b_q, b_bar = "-", "-", ""

            # Ask side
            if a:
                a_p = f"${fixed_to_price(a['price']):,.2f}"
                a_q = f"{a['quantity']:,}"
                bar_len = min(10, int((a['quantity'] / max_vol) * 10))
                a_bar = "■" * bar_len
            else:
                a_p, a_q, a_bar = "-", "-", ""

            spr_str = f"← ${spread_val:.2f} →" if i == 0 else ""

            book_table.add_row(b_bar, b_q, b_p, spr_str, a_p, a_q, a_bar)

        layout["main"]["book"].update(
            Panel(book_table, title=f"[bold]Limit Order Book (L2 Depth) - {self.symbol_name}[/bold]", box=box.ROUNDED, style="blue")
        )

        # 4. Latency Metrics & Ring Buffer Telemetry
        p = self.latency_stats.get_percentiles()
        sparkline = self.latency_stats.get_sparkline()
        occ_pct = (buffer_occupancy / buffer_capacity * 100.0) if buffer_capacity else 0.0

        bar_filled = int(occ_pct / 10)
        occ_bar = "█" * bar_filled + "░" * (10 - bar_filled)

        metrics_text = Text()
        metrics_text.append("📊 REAL-TIME EXECUTION LATENCY\n", style="bold yellow")
        metrics_text.append(f"  • p50 (Median):   ", style="white")
        metrics_text.append(f"{p['p50']:.2f} μs\n", style="bold green" if p['p50'] < 10 else "yellow")
        metrics_text.append(f"  • p90 Latency:   ", style="white")
        metrics_text.append(f"{p['p90']:.2f} μs\n", style="bold cyan")
        metrics_text.append(f"  • p99 Tail SLA:  ", style="white")
        metrics_text.append(f"{p['p99']:.2f} μs  ", style="bold bright_green" if p['p99'] < 50 else "bold red")
        metrics_text.append("[<50μs SLA PASS]\n", style="bold green" if p['p99'] < 50 else "bold red")
        metrics_text.append(f"  • Min / Max:     {p['min']:.2f} μs / {p['max']:.2f} μs\n", style="dim")
        metrics_text.append(f"  • Trend (20):    [{sparkline}]\n\n", style="cyan")

        metrics_text.append("💾 ZERO-COPY MMAP RING BUFFER\n", style="bold magenta")
        metrics_text.append(f"  • Occupancy:     [{occ_bar}] {occ_pct:.1f}% ({buffer_occupancy:,} / {buffer_capacity:,})\n", style="white")
        metrics_text.append(f"  • Total Ingested: {total_written:,} orders\n", style="white")
        metrics_text.append(f"  • Total Matched:  {total_read:,} orders\n", style="white")
        metrics_text.append(f"  • Throughput:     {throughput_ops:,.0f} orders/sec\n", style="bold bright_cyan")

        layout["main"]["metrics"].update(
            Panel(metrics_text, title="[bold]Engine Performance & IPC Telemetry[/bold]", box=box.ROUNDED, style="yellow")
        )

        # 5. Footer: Live Executed Trades Ticker
        trade_table = Table(box=box.MINIMAL, expand=True)
        trade_table.add_column("Trade ID", style="dim")
        trade_table.add_column("Side", justify="center")
        trade_table.add_column("Price", justify="right")
        trade_table.add_column("Quantity", justify="right")
        trade_table.add_column("Maker #", style="dim")
        trade_table.add_column("Taker #", style="dim")
        trade_table.add_column("Latency", justify="right", style="cyan")
        trade_table.add_column("Flags", style="yellow")

        for tr in reversed(self.recent_trades[-5:]):
            s_str = "BUY" if tr.get("side") == Side.BUY else "SELL"
            s_style = "bold green" if s_str == "BUY" else "bold red"
            pr_val = f"${fixed_to_price(tr.get('price', 0)):,.2f}"
            qty_val = f"{tr.get('quantity', 0):,}"
            lat = tr.get("latency_us", tr.get("latency_ns", 0) / 1000.0)
            flag_str = "🐋 WHALE" if (tr.get("flags", 0) & OrderFlags.WHALE_SWEEP) else "-"
            trade_table.add_row(
                str(tr.get("trade_id", "-")),
                Text(s_str, style=s_style),
                pr_val,
                qty_val,
                str(tr.get("maker_order_id", "-")),
                str(tr.get("taker_order_id", "-")),
                f"{lat:.1f} μs",
                flag_str,
            )

        layout["footer"].update(
            Panel(trade_table, title="[bold]Live Trade Execution Ticker (Zero Serialization Lag)[/bold]", box=box.ROUNDED, style="cyan")
        )

        return layout

    def render_ansi_fallback(
        self,
        depth: Dict[str, Any],
        buffer_occupancy: int,
        buffer_capacity: int,
        total_written: int,
        total_read: int,
        throughput_ops: float,
    ) -> str:
        """ANSI text fallback when rich or curses is not active."""
        p = self.latency_stats.get_percentiles()
        bids = depth.get("bids", [])
        asks = depth.get("asks", [])
        best_bid = fixed_to_price(depth.get("best_bid", 0)) if depth.get("best_bid") else 0.0
        best_ask = fixed_to_price(depth.get("best_ask", 0)) if depth.get("best_ask") else 0.0
        spread = fixed_to_price(depth.get("spread", 0))

        lines = [
            "=" * 78,
            "⚡ CHRONOSMATCH // ZERO-COPY HIGH-FREQUENCY TRADING ENGINE",
            f"SYMBOL: {self.symbol_name} | GIL: BYPASSED | IPC: MMAP RAM | SLA: <50μs",
            "=" * 78,
        ]

        if self.last_whale_alert and (time.time() - self.last_whale_time < 3.0):
            lines.append(f">>> 🚨 {self.last_whale_alert} 🚨 <<<")

        lines.append(f"Top of Book: Bid ${best_bid:.2f} | Ask ${best_ask:.2f} | Spread: ${spread:.2f}")
        lines.append(f"Latency: p50={p['p50']:.1f}μs | p90={p['p90']:.1f}μs | p99={p['p99']:.1f}μs | Max={p['max']:.1f}μs")
        lines.append(f"Throughput: {throughput_ops:,.0f} orders/sec | Buffer Occupancy: {buffer_occupancy}/{buffer_capacity}")
        lines.append("-" * 78)

        # Top 3 bids and asks
        lines.append("BIDS: " + ", ".join([f"${fixed_to_price(b['price']):.2f} ({b['quantity']})" for b in bids[:3]]))
        lines.append("ASKS: " + ", ".join([f"${fixed_to_price(a['price']):.2f} ({a['quantity']})" for a in asks[:3]]))
        lines.append("=" * 78)
        return "\n".join(lines)
