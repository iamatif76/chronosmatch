"""
ChronosMatch Engine Wrapper.
Provides seamless high-performance interface to the compiled C/Cython matching engine
via native DLL (libchronos_engine.dll) or Cython extension.
"""

import os
import sys
import ctypes
import time
from typing import Dict, Any, List, Optional, Tuple
from engine.protocol import (
    Side,
    OrderType,
    OrderStatus,
    OrderFlags,
    Symbol,
    price_to_fixed,
    fixed_to_price,
)

# ------------------------------------------------------------------------------
# Ctypes Struct Definitions for Native DLL Interop
# ------------------------------------------------------------------------------

class C_RawOrder(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("order_id", ctypes.c_uint64),
        ("timestamp_ns", ctypes.c_uint64),
        ("exec_timestamp_ns", ctypes.c_uint64),
        ("symbol_id", ctypes.c_uint32),
        ("side", ctypes.c_uint8),
        ("order_type", ctypes.c_uint8),
        ("flags", ctypes.c_uint16),
        ("price", ctypes.c_uint64),
        ("quantity", ctypes.c_uint32),
        ("filled_qty", ctypes.c_uint32),
        ("match_price", ctypes.c_uint64),
        ("match_counterparty_id", ctypes.c_uint32),
        ("status", ctypes.c_uint8),
        ("reserved", ctypes.c_uint8 * 3),
    ]

class C_RawTrade(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("trade_id", ctypes.c_uint64),
        ("maker_order_id", ctypes.c_uint64),
        ("taker_order_id", ctypes.c_uint64),
        ("symbol_id", ctypes.c_uint32),
        ("side", ctypes.c_uint8),
        ("flags", ctypes.c_uint8),
        ("reserved_h", ctypes.c_uint16),
        ("price", ctypes.c_uint64),
        ("quantity", ctypes.c_uint32),
        ("latency_ns", ctypes.c_uint32),
        ("match_timestamp_ns", ctypes.c_uint64),
        ("padding", ctypes.c_uint8 * 8),
    ]

class C_L2Entry(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("price", ctypes.c_uint64),
        ("quantity", ctypes.c_uint32),
        ("order_count", ctypes.c_uint32),
    ]

class C_L2DepthSnapshot(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("bid_count", ctypes.c_uint32),
        ("ask_count", ctypes.c_uint32),
        ("bids", C_L2Entry * 10),
        ("asks", C_L2Entry * 10),
        ("best_bid", ctypes.c_uint64),
        ("best_ask", ctypes.c_uint64),
        ("spread", ctypes.c_uint64),
        ("total_trades", ctypes.c_uint64),
        ("total_volume", ctypes.c_uint64),
        ("last_match_latency_ns", ctypes.c_uint64),
    ]


class NativeOrderBook:
    """
    High-Performance Native C / Cython Order Book Interface.
    Directly invokes compiled C DLL functions with pointers.
    """

    def __init__(self, symbol_id: int = Symbol.AAPL):
        self.symbol_id = symbol_id

        # Locate DLL
        dll_dir = os.path.dirname(os.path.abspath(__file__))
        dll_path = os.path.join(dll_dir, "libchronos_engine.dll")
        if not os.path.exists(dll_path):
            # Fallback path if in ChronosMatch root
            dll_path = os.path.join(os.path.dirname(dll_dir), "engine", "libchronos_engine.dll")

        if not os.path.exists(dll_path):
            raise FileNotFoundError(f"Native engine DLL not found at: {dll_path}")

        self._lib = ctypes.CDLL(dll_path)

        # Function signatures
        self._lib.orderbook_create.argtypes = [ctypes.c_uint32]
        self._lib.orderbook_create.restype = ctypes.c_void_p

        self._lib.orderbook_destroy.argtypes = [ctypes.c_void_p]
        self._lib.orderbook_destroy.restype = None

        self._lib.orderbook_reset.argtypes = [ctypes.c_void_p]
        self._lib.orderbook_reset.restype = None

        self._lib.orderbook_process_order.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(C_RawOrder),
            ctypes.POINTER(C_RawTrade),
            ctypes.c_int,
        ]
        self._lib.orderbook_process_order.restype = ctypes.c_int

        self._lib.orderbook_cancel_order.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        self._lib.orderbook_cancel_order.restype = ctypes.c_bool

        self._lib.orderbook_get_l2_depth.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(C_L2DepthSnapshot),
        ]
        self._lib.orderbook_get_l2_depth.restype = None

        self._lib.get_current_time_ns.argtypes = []
        self._lib.get_current_time_ns.restype = ctypes.c_uint64

        self._book_ptr = self._lib.orderbook_create(symbol_id)
        if not self._book_ptr:
            raise MemoryError("Failed to allocate native order book")

        # Pre-allocate reusable C structs to prevent GC memory allocations during matching
        self._order_struct = C_RawOrder()
        self._trade_buffer = (C_RawTrade * 128)()
        self._depth_struct = C_L2DepthSnapshot()

    def __del__(self):
        if hasattr(self, "_book_ptr") and self._book_ptr:
            self._lib.orderbook_destroy(self._book_ptr)
            self._book_ptr = None

    def reset(self) -> None:
        """Reset order book state."""
        self._lib.orderbook_reset(self._book_ptr)

    def now_ns(self) -> int:
        """Get high-precision nanoseconds from engine timer."""
        return self._lib.get_current_time_ns()

    def process_order(
        self,
        order_id: int,
        timestamp_ns: int,
        symbol_id: int,
        side: int,
        order_type: int,
        flags: int,
        price: int,
        quantity: int,
    ) -> List[Dict[str, Any]]:
        """
        Submits an order to the matching engine.
        Executes without triggering Python Garbage Collection pauses.
        """
        o = self._order_struct
        o.order_id = order_id
        o.timestamp_ns = timestamp_ns
        o.exec_timestamp_ns = 0
        o.symbol_id = symbol_id
        o.side = side
        o.order_type = order_type
        o.flags = flags
        o.price = price
        o.quantity = quantity
        o.filled_qty = 0
        o.match_price = 0
        o.match_counterparty_id = 0
        o.status = OrderStatus.NEW

        trade_count = self._lib.orderbook_process_order(
            self._book_ptr,
            ctypes.byref(o),
            self._trade_buffer,
            128,
        )

        if trade_count == 0:
            return []

        trades = []
        for i in range(trade_count):
            t = self._trade_buffer[i]
            trades.append({
                "trade_id": t.trade_id,
                "maker_order_id": t.maker_order_id,
                "taker_order_id": t.taker_order_id,
                "symbol_id": t.symbol_id,
                "side": t.side,
                "flags": t.flags,
                "price": t.price,
                "quantity": t.quantity,
                "latency_ns": t.latency_ns,
                "latency_us": t.latency_ns / 1000.0,
                "match_timestamp_ns": t.match_timestamp_ns,
            })
        return trades

    def process_raw_tuple(self, order_tuple: Tuple) -> List[Dict[str, Any]]:
        """
        Process an order tuple directly unpacked from mmap ring buffer.
        """
        (
            order_id,
            timestamp_ns,
            _,
            symbol_id,
            side,
            order_type,
            flags,
            price,
            quantity,
            _,
            _,
            _,
            _,
            _,
        ) = order_tuple

        return self.process_order(
            order_id=order_id,
            timestamp_ns=timestamp_ns,
            symbol_id=symbol_id,
            side=side,
            order_type=order_type,
            flags=flags,
            price=price,
            quantity=quantity,
        )

    def cancel_order(self, order_id: int) -> bool:
        """Cancel an order by ID."""
        return bool(self._lib.orderbook_cancel_order(self._book_ptr, order_id))

    def get_l2_depth(self) -> Dict[str, Any]:
        """Fetch top 10 Bid and Ask levels and market statistics."""
        self._lib.orderbook_get_l2_depth(self._book_ptr, ctypes.byref(self._depth_struct))
        d = self._depth_struct

        bids = []
        for i in range(d.bid_count):
            bids.append({
                "price": d.bids[i].price,
                "quantity": d.bids[i].quantity,
                "order_count": d.bids[i].order_count,
            })

        asks = []
        for i in range(d.ask_count):
            asks.append({
                "price": d.asks[i].price,
                "quantity": d.asks[i].quantity,
                "order_count": d.asks[i].order_count,
            })

        return {
            "bids": bids,
            "asks": asks,
            "best_bid": d.best_bid,
            "best_ask": d.best_ask,
            "spread": d.spread,
            "total_trades": d.total_trades,
            "total_volume": d.total_volume,
            "last_match_latency_ns": d.last_match_latency_ns,
            "last_match_latency_us": d.last_match_latency_ns / 1000.0,
        }
