"""
End-to-end integration test for ChronosMatch.
Tests complete lifecycle: Firehose -> IPC Bus -> Matching Engine -> Trades Buffer -> SQLite Ledger.
"""

import os
import time
import pytest
from engine.protocol import (
    Side,
    OrderType,
    OrderFlags,
    Symbol,
    ORDER_SIZE,
    TRADE_SIZE,
    price_to_fixed,
)
from engine.ipc_ring_buffer import MmapRingBuffer
from engine.engine_wrapper import NativeOrderBook
from simulator.market_firehose import MarketFirehose
from resiliency.trade_ledger import TradeAuditLedger


def test_full_pipeline_integration(tmp_path):
    orders_path = str(tmp_path / "orders.dat")
    trades_path = str(tmp_path / "trades.dat")
    db_path = str(tmp_path / "audit.db")

    orders_ring = MmapRingBuffer(orders_path, capacity=1024, slot_size=ORDER_SIZE, create=True)
    trades_ring = MmapRingBuffer(trades_path, capacity=1024, slot_size=TRADE_SIZE, create=True)
    book = NativeOrderBook(Symbol.AAPL)
    ledger = TradeAuditLedger(db_path=db_path, trade_ring_buffer=trades_ring)

    # 1. Producer generates 100 orders
    firehose = MarketFirehose(orders_ring, target_rate=10_000)
    for _ in range(100):
        o = firehose.generate_single_order()
        orders_ring.write_order_raw(
            order_id=o[0],
            timestamp_ns=o[1],
            exec_timestamp_ns=o[2],
            symbol_id=o[3],
            side=o[4],
            order_type=o[5],
            flags=o[6],
            price=o[7],
            quantity=o[8],
        )

    assert orders_ring.get_total_written() == 100

    # 2. Matching Engine processes from ring buffer
    batch = orders_ring.read_order_batch(100)
    assert len(batch) == 100

    executed_trade_count = 0
    for raw_tuple in batch:
        trades = book.process_raw_tuple(raw_tuple)
        for t in trades:
            executed_trade_count += 1
            trades_ring.write_trade_raw(
                trade_id=t["trade_id"],
                maker_order_id=t["maker_order_id"],
                taker_order_id=t["taker_order_id"],
                symbol_id=t["symbol_id"],
                side=t["side"],
                flags=t["flags"],
                price=t["price"],
                quantity=t["quantity"],
                latency_ns=t["latency_ns"],
                match_timestamp_ns=t["match_timestamp_ns"],
            )

    # 3. Flusher drains trade ring buffer to SQLite ledger
    flushed = ledger.drain_ring_buffer_step(500)
    assert flushed == executed_trade_count

    stats = ledger.get_summary_stats()
    assert stats["total_trades"] == executed_trade_count
    if executed_trade_count > 0:
        assert stats["avg_latency_us"] >= 0.0
        assert stats["total_volume"] > 0

    orders_ring.close()
    trades_ring.close()
