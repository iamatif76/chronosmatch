"""
Unit tests for ChronosMatch Zero-Copy IPC Ring Buffer.
"""

import os
import pytest
from engine.protocol import (
    Side,
    OrderType,
    OrderFlags,
    Symbol,
    ORDER_SIZE,
    TRADE_SIZE,
    price_to_fixed,
    fixed_to_price,
)
from engine.ipc_ring_buffer import MmapRingBuffer


@pytest.fixture
def temp_ring_buffer(tmp_path):
    path = str(tmp_path / "test_ring.dat")
    rb = MmapRingBuffer(path, capacity=16, slot_size=ORDER_SIZE, create=True)
    yield rb
    rb.close()


def test_ring_buffer_init(temp_ring_buffer):
    rb = temp_ring_buffer
    assert rb.capacity == 16
    assert rb.slot_size == ORDER_SIZE
    assert rb.occupancy == 0
    assert rb.get_head() == 0
    assert rb.get_tail() == 0
    assert rb.get_dropped() == 0


def test_write_and_read_single_order(temp_ring_buffer):
    rb = temp_ring_buffer
    ok = rb.write_order_raw(
        order_id=42,
        timestamp_ns=1000000,
        exec_timestamp_ns=0,
        symbol_id=Symbol.AAPL,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        flags=OrderFlags.NONE,
        price=price_to_fixed(150.50),
        quantity=300,
    )
    assert ok is True
    assert rb.occupancy == 1

    order = rb.read_order_raw()
    assert order is not None
    assert order[0] == 42  # order_id
    assert order[1] == 1000000  # timestamp_ns
    assert order[3] == Symbol.AAPL
    assert order[4] == Side.BUY
    assert order[5] == OrderType.LIMIT
    assert order[7] == price_to_fixed(150.50)
    assert order[8] == 300  # quantity
    assert rb.occupancy == 0


def test_ring_buffer_overflow(temp_ring_buffer):
    rb = temp_ring_buffer
    # Write capacity (16) orders
    for i in range(16):
        assert rb.write_order_raw(i + 1, 100, 0, Symbol.AAPL, Side.BUY, OrderType.LIMIT, 0, 100, 10) is True

    assert rb.occupancy == 16
    # 17th write must fail (overflow) and increment dropped counter
    assert rb.write_order_raw(999, 100, 0, Symbol.AAPL, Side.BUY, OrderType.LIMIT, 0, 100, 10) is False
    assert rb.get_dropped() == 1


def test_ring_buffer_wrap_around(temp_ring_buffer):
    rb = temp_ring_buffer
    # Write and read 50 orders sequentially, wrapping around the 16-slot ring multiple times
    for i in range(50):
        ok = rb.write_order_raw(i + 100, i * 10, 0, Symbol.NVDA, Side.SELL, OrderType.LIMIT, 0, 200, 50)
        assert ok is True
        order = rb.read_order_raw()
        assert order is not None
        assert order[0] == i + 100

    assert rb.get_total_written() == 50
    assert rb.get_total_read() == 50
    assert rb.occupancy == 0


def test_batch_read(temp_ring_buffer):
    rb = temp_ring_buffer
    for i in range(10):
        rb.write_order_raw(i + 1, 100, 0, Symbol.TSFT if hasattr(Symbol, "TSFT") else Symbol.AAPL, Side.BUY, OrderType.LIMIT, 0, 100, 10)

    batch = rb.read_order_batch(max_batch=6)
    assert len(batch) == 6
    assert [o[0] for o in batch] == list(range(1, 7))
    assert rb.occupancy == 4

    batch2 = rb.read_order_batch(max_batch=10)
    assert len(batch2) == 4
    assert [o[0] for o in batch2] == list(range(7, 11))
    assert rb.occupancy == 0
