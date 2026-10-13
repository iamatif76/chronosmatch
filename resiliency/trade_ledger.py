"""
ChronosMatch Resiliency Audit Ledger.
Asynchronously flushes matched trade reports from the zero-copy trade ring buffer
to a permanent SQLite ledger for regulatory auditing without blocking the low-latency critical path.
"""

import os
import time
import sqlite3
import datetime
from typing import Optional, List, Tuple
from engine.protocol import (
    Side,
    OrderFlags,
    Symbol,
    SYMBOL_NAMES,
    fixed_to_price,
)
from engine.ipc_ring_buffer import MmapRingBuffer


class TradeAuditLedger:
    """
    High-throughput asynchronous SQLite trade ledger.
    Drains trade execution reports from shared memory and persists them.
    """

    def __init__(self, db_path: str = "trades.db", trade_ring_buffer: Optional[MmapRingBuffer] = None):
        self.db_path = os.path.abspath(db_path)
        self.trade_ring = trade_ring_buffer
        self.running = False
        self.total_flushed = 0
        self.last_flush_time = 0.0

        os.makedirs(os.path.dirname(self.db_path) if os.path.dirname(self.db_path) else ".", exist_ok=True)
        self._init_db()

    def _init_db(self):
        """Initialize SQLite database with WAL mode for non-blocking concurrent writes."""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA synchronous=NORMAL;")
        cur.execute("PRAGMA temp_store=MEMORY;")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                trade_id INTEGER PRIMARY KEY,
                maker_order_id INTEGER,
                taker_order_id INTEGER,
                symbol TEXT,
                side TEXT,
                flags INTEGER,
                price REAL,
                quantity INTEGER,
                latency_ns INTEGER,
                latency_us REAL,
                match_timestamp_ns INTEGER,
                is_whale_sweep INTEGER,
                created_at TEXT
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_latency ON trades(latency_us);")
        conn.commit()
        conn.close()

    def flush_batch(self, trades_batch: List[Tuple]) -> int:
        """
        Inserts a batch of raw trade execution tuples into SQLite in a single transaction.
        Raw tuple format:
        (trade_id, maker_id, taker_id, symbol_id, side, flags, _, price, qty, latency_ns, match_ts_ns, _)
        """
        if not trades_batch:
            return 0

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()

        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        records = []

        for t in trades_batch:
            trade_id = t[0]
            maker_id = t[1]
            taker_id = t[2]
            symbol_id = t[3]
            side_val = t[4]
            flags = t[5]
            raw_price = t[7]
            qty = t[8]
            latency_ns = t[9]
            match_ts_ns = t[10]

            sym_str = SYMBOL_NAMES.get(symbol_id, f"SYM_{symbol_id}")
            side_str = "BUY" if side_val == Side.BUY else "SELL"
            float_price = fixed_to_price(raw_price)
            latency_us = latency_ns / 1000.0
            is_whale = 1 if (flags & OrderFlags.WHALE_SWEEP) else 0

            records.append((
                trade_id,
                maker_id,
                taker_id,
                sym_str,
                side_str,
                flags,
                float_price,
                qty,
                latency_ns,
                latency_us,
                match_ts_ns,
                is_whale,
                now_iso,
            ))

        cur.executemany("""
            INSERT OR IGNORE INTO trades (
                trade_id, maker_order_id, taker_order_id, symbol, side, flags,
                price, quantity, latency_ns, latency_us, match_timestamp_ns,
                is_whale_sweep, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, records)

        conn.commit()
        conn.close()

        count = len(records)
        self.total_flushed += count
        self.last_flush_time = time.perf_counter()
        return count

    def drain_ring_buffer_step(self, max_batch: int = 2000) -> int:
        """Reads available trades from ring buffer and flushes to database."""
        if not self.trade_ring:
            return 0

        batch = self.trade_ring.read_trade_batch(max_batch)
        if not batch:
            return 0

        return self.flush_batch(batch)

    def get_summary_stats(self) -> dict:
        """Query ledger for audit statistics."""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*), AVG(latency_us), MIN(latency_us), MAX(latency_us), SUM(quantity), SUM(is_whale_sweep) FROM trades;")
        row = cur.fetchone()
        conn.close()

        if not row or row[0] == 0:
            return {
                "total_trades": 0,
                "avg_latency_us": 0.0,
                "min_latency_us": 0.0,
                "max_latency_us": 0.0,
                "total_volume": 0,
                "whale_sweeps": 0,
            }

        return {
            "total_trades": row[0],
            "avg_latency_us": round(row[1] or 0.0, 2),
            "min_latency_us": round(row[2] or 0.0, 2),
            "max_latency_us": round(row[3] or 0.0, 2),
            "total_volume": row[4] or 0,
            "whale_sweeps": row[5] or 0,
        }
