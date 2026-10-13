"""
Unit tests for ChronosMatch Cython/C Limit Order Book Matching Engine.
Verifies:
1. Resting Limit Orders
2. Instant Price Crossing Fills
3. Price-Time Priority (FIFO at price level)
4. Partial fills & remaining resting balance
5. Whale Sweep orders clearing multiple price levels
6. O(1) Order Cancellations
7. Level 2 Depth Snapshots & Spread calculation
"""

import pytest
from engine.protocol import (
    Side,
    OrderType,
    OrderFlags,
    Symbol,
    price_to_fixed,
    fixed_to_price,
)
from engine.engine_wrapper import NativeOrderBook


@pytest.fixture
def order_book():
    book = NativeOrderBook(Symbol.AAPL)
    book.reset()
    return book


def test_resting_order_placement(order_book):
    book = order_book
    # Place resting BUY at $150.00 (qty 100)
    trades = book.process_order(
        order_id=1,
        timestamp_ns=book.now_ns(),
        symbol_id=Symbol.AAPL,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        flags=OrderFlags.NONE,
        price=price_to_fixed(150.00),
        quantity=100,
    )
    assert len(trades) == 0

    depth = book.get_l2_depth()
    assert len(depth["bids"]) == 1
    assert depth["bids"][0]["price"] == price_to_fixed(150.00)
    assert depth["bids"][0]["quantity"] == 100
    assert depth["best_bid"] == price_to_fixed(150.00)
    assert depth["best_ask"] == 0


def test_instant_full_match(order_book):
    book = order_book
    # Maker: Resting BUY 100 @ $150.00
    book.process_order(1, book.now_ns(), Symbol.AAPL, Side.BUY, OrderType.LIMIT, 0, price_to_fixed(150.00), 100)

    # Taker: Aggressive SELL 100 @ $150.00
    trades = book.process_order(2, book.now_ns(), Symbol.AAPL, Side.SELL, OrderType.LIMIT, 0, price_to_fixed(150.00), 100)

    assert len(trades) == 1
    t = trades[0]
    assert t["maker_order_id"] == 1
    assert t["taker_order_id"] == 2
    assert t["price"] == price_to_fixed(150.00)
    assert t["quantity"] == 100
    assert t["latency_ns"] >= 0
    assert t["latency_us"] < 50.0  # Must be sub-50 microseconds!

    # Book should be completely cleared
    depth = book.get_l2_depth()
    assert len(depth["bids"]) == 0
    assert len(depth["asks"]) == 0
    assert depth["total_trades"] == 1
    assert depth["total_volume"] == 100


def test_price_time_priority_fifo(order_book):
    book = order_book
    # Maker 1: Order #1 placed at T0, price $150.00, qty 50
    book.process_order(1, 1000, Symbol.AAPL, Side.BUY, OrderType.LIMIT, 0, price_to_fixed(150.00), 50)
    # Maker 2: Order #2 placed at T1, same price $150.00, qty 50
    book.process_order(2, 2000, Symbol.AAPL, Side.BUY, OrderType.LIMIT, 0, price_to_fixed(150.00), 50)

    # Taker arrives for 60 shares @ $150.00
    trades = book.process_order(3, 3000, Symbol.AAPL, Side.SELL, OrderType.LIMIT, 0, price_to_fixed(150.00), 60)

    assert len(trades) == 2
    # First 50 shares MUST execute against Maker 1 (FIFO Time Priority!)
    assert trades[0]["maker_order_id"] == 1
    assert trades[0]["quantity"] == 50

    # Remaining 10 shares MUST execute against Maker 2
    assert trades[1]["maker_order_id"] == 2
    assert trades[1]["quantity"] == 10

    # Maker 2 should still have 40 shares resting on the book
    depth = book.get_l2_depth()
    assert len(depth["bids"]) == 1
    assert depth["bids"][0]["quantity"] == 40


def test_whale_sweep_multi_level(order_book):
    book = order_book
    # Construct an Ask ladder across 3 distinct price levels:
    # Level 1: 100 shares @ $150.10
    # Level 2: 200 shares @ $150.20
    # Level 3: 500 shares @ $150.30
    book.process_order(10, book.now_ns(), Symbol.AAPL, Side.SELL, OrderType.LIMIT, 0, price_to_fixed(150.10), 100)
    book.process_order(11, book.now_ns(), Symbol.AAPL, Side.SELL, OrderType.LIMIT, 0, price_to_fixed(150.20), 200)
    book.process_order(12, book.now_ns(), Symbol.AAPL, Side.SELL, OrderType.LIMIT, 0, price_to_fixed(150.30), 500)

    # Whale Taker arrives: BUY 600 shares @ $150.30 (Sweeps through levels 1, 2, and into 3!)
    trades = book.process_order(
        order_id=99,
        timestamp_ns=book.now_ns(),
        symbol_id=Symbol.AAPL,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        flags=OrderFlags.WHALE_SWEEP,
        price=price_to_fixed(150.30),
        quantity=600,
    )

    assert len(trades) == 3
    # Trade 1: Clears Level 1
    assert trades[0]["maker_order_id"] == 10
    assert trades[0]["price"] == price_to_fixed(150.10)
    assert trades[0]["quantity"] == 100

    # Trade 2: Clears Level 2
    assert trades[1]["maker_order_id"] == 11
    assert trades[1]["price"] == price_to_fixed(150.20)
    assert trades[1]["quantity"] == 200

    # Trade 3: Partially fills Level 3
    assert trades[2]["maker_order_id"] == 12
    assert trades[2]["price"] == price_to_fixed(150.30)
    assert trades[2]["quantity"] == 300

    # Level 3 should still have 200 shares resting
    depth = book.get_l2_depth()
    assert len(depth["asks"]) == 1
    assert depth["asks"][0]["price"] == price_to_fixed(150.30)
    assert depth["asks"][0]["quantity"] == 200


def test_order_cancellation(order_book):
    book = order_book
    book.process_order(1, book.now_ns(), Symbol.AAPL, Side.BUY, OrderType.LIMIT, 0, price_to_fixed(150.00), 100)
    book.process_order(2, book.now_ns(), Symbol.AAPL, Side.BUY, OrderType.LIMIT, 0, price_to_fixed(150.00), 200)

    depth = book.get_l2_depth()
    assert depth["bids"][0]["quantity"] == 300

    # Cancel order 1 in O(1) time
    cancelled = book.cancel_order(1)
    assert cancelled is True

    depth = book.get_l2_depth()
    assert depth["bids"][0]["quantity"] == 200

    # Non-existent order cancellation returns False
    assert book.cancel_order(9999) is False
