"""
ChronosMatch High-Throughput Market Simulator (Market Firehose).
Asynchronously simulates a firehose of Nasdaq/NYSE financial tick data
and blasts orders directly into the zero-copy memory-mapped ring buffer.
"""

import os
import sys
import time
import math
import random
import asyncio
from typing import Optional, Dict

from engine.protocol import (
    Side,
    OrderType,
    OrderFlags,
    Symbol,
    SYMBOL_NAMES,
    price_to_fixed,
    fixed_to_price,
)
from engine.ipc_ring_buffer import MmapRingBuffer


class MarketFirehose:
    """
    High-throughput market data generator.
    Simulates real-world limit order flow, spread dynamics, cancellations,
    and massive "Whale" sweep orders.
    """

    def __init__(
        self,
        ring_buffer: MmapRingBuffer,
        target_rate: int = 100_000,  # Orders per second
        symbols: Optional[Dict[int, float]] = None,
    ):
        self.ring_buffer = ring_buffer
        self.target_rate = target_rate
        self.running = False
        self.order_counter = 1

        # Initial fair prices (mid-prices in dollars)
        if symbols is None:
            self.fair_prices = {
                Symbol.AAPL: 150.00,
                Symbol.NVDA: 125.00,
                Symbol.TSLA: 210.00,
                Symbol.MSFT: 420.00,
            }
        else:
            self.fair_prices = dict(symbols)

        # Active resting order IDs for generating cancellations
        self.active_orders = []
        self.max_tracked_orders = 10_000

        # Stats
        self.total_generated = 0
        self.total_whales = 0
        self.start_time = 0.0

    def generate_single_order(self) -> tuple:
        """
        Generate a single realistic market order with fixed-point arithmetic.
        """
        self.order_counter += 1
        order_id = self.order_counter
        now_ns = time.perf_counter_ns()

        symbol_id = random.choice(list(self.fair_prices.keys()))
        mid_price = self.fair_prices[symbol_id]

        # Geometric Brownian drift (random walk fair price)
        drift = random.gauss(0, 0.02)
        mid_price = max(10.0, mid_price + drift)
        self.fair_prices[symbol_id] = mid_price

        # Determine order type and side
        roll = random.random()

        # 5% Whale Sweep Order (Massive market sweep!)
        if roll < 0.05:
            side = Side.BUY if random.random() < 0.5 else Side.SELL
            qty = random.randint(5_000, 25_000)
            flags = OrderFlags.WHALE_SWEEP
            # Aggressive limit price well beyond mid price to sweep multiple book levels
            if side == Side.BUY:
                price = mid_price + random.uniform(0.15, 0.50)
            else:
                price = max(1.0, mid_price - random.uniform(0.15, 0.50))
            order_type = OrderType.LIMIT
            self.total_whales += 1

        # 15% Cancellation Order (O(1) book removal)
        elif roll < 0.20 and self.active_orders:
            cancel_id = self.active_orders.pop(random.randrange(len(self.active_orders)))
            return (
                cancel_id,
                now_ns,
                0,
                symbol_id,
                Side.BUY,
                OrderType.CANCEL,
                OrderFlags.NONE,
                0,
                0,
            )

        # 15% Aggressive Crossing Order (Immediate execution against resting book)
        elif roll < 0.35:
            side = Side.BUY if random.random() < 0.5 else Side.SELL
            qty = random.choice([50, 100, 200, 500, 1000])
            flags = OrderFlags.NONE
            order_type = OrderType.LIMIT
            # Cross spread by 1-3 ticks
            spread_offset = random.uniform(0.01, 0.04)
            if side == Side.BUY:
                price = mid_price + spread_offset
            else:
                price = max(1.0, mid_price - spread_offset)

        # 65% Passive Liquidity Provider (Resting Limit Order)
        else:
            side = Side.BUY if random.random() < 0.5 else Side.SELL
            qty = random.choice([100, 200, 300, 500, 1000, 2000])
            flags = OrderFlags.NONE
            order_type = OrderType.LIMIT
            # Place 1 to 10 cents away from mid price
            distance = random.uniform(0.01, 0.12)
            if side == Side.BUY:
                price = max(1.0, mid_price - distance)
            else:
                price = mid_price + distance

            # Track for potential cancellation
            if len(self.active_orders) < self.max_tracked_orders:
                self.active_orders.append(order_id)

        fp_price = price_to_fixed(price)
        return (
            order_id,
            now_ns,
            0,
            symbol_id,
            side,
            order_type,
            flags,
            fp_price,
            qty,
        )

    def blast_benchmark(self, count: int = 100_000) -> float:
        """
        Blasts `count` orders synchronously as fast as raw CPU allows.
        Returns elapsed seconds.
        """
        start = time.perf_counter()
        written = 0

        for _ in range(count):
            (
                order_id,
                ts_ns,
                exec_ns,
                sym_id,
                side,
                o_type,
                flags,
                price,
                qty,
            ) = self.generate_single_order()

            while not self.ring_buffer.write_order_raw(
                order_id=order_id,
                timestamp_ns=ts_ns,
                exec_timestamp_ns=exec_ns,
                symbol_id=sym_id,
                side=side,
                order_type=o_type,
                flags=flags,
                price=price,
                quantity=qty,
            ):
                # Back-off spin if buffer is temporarily full
                time.sleep(0.00001)

            written += 1

        elapsed = time.perf_counter() - start
        self.total_generated += written
        return elapsed

    async def run_paced(self, duration_sec: Optional[float] = None, orders_per_sec: int = 2000):
        """
        Runs asynchronous paced generation for live terminal visualization.
        """
        self.running = True
        self.start_time = time.perf_counter()
        batch_size = max(1, orders_per_sec // 50)  # 50 Hz tick frequency
        sleep_interval = 1.0 / 50.0

        print(f"[*] Firehose active: Generating {orders_per_sec:,} orders/sec (Batch size: {batch_size})")

        while self.running:
            loop_start = time.perf_counter()

            for _ in range(batch_size):
                (
                    order_id,
                    ts_ns,
                    exec_ns,
                    sym_id,
                    side,
                    o_type,
                    flags,
                    price,
                    qty,
                ) = self.generate_single_order()

                self.ring_buffer.write_order_raw(
                    order_id=order_id,
                    timestamp_ns=ts_ns,
                    exec_timestamp_ns=exec_ns,
                    symbol_id=sym_id,
                    side=side,
                    order_type=o_type,
                    flags=flags,
                    price=price,
                    quantity=qty,
                )
                self.total_generated += 1

            if duration_sec and (time.perf_counter() - self.start_time) >= duration_sec:
                break

            elapsed = time.perf_counter() - loop_start
            sleep_time = max(0.001, sleep_interval - elapsed)
            await asyncio.sleep(sleep_time)

    def stop(self):
        """Stop firehose generator."""
        self.running = False
