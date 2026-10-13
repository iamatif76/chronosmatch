# cython: language_level=3
# cython: boundscheck=False
# cython: wraparound=False
# cython: cdivision=True
# cython: nonecheck=False
"""
ChronosMatch Cython Limit Order Book Engine.
High-frequency trading engine bypassing Python GIL with statically typed C-structs,
pointers, and zero-allocation matching loop to prevent Garbage Collector pauses.
"""

from libc.stdint cimport uint8_t, uint16_t, uint32_t, uint64_t
from libc.stdlib cimport malloc, free
from libc.string cimport memset, memcpy
cimport cython

cdef extern from "native_engine.h" nogil:
    ctypedef struct RawOrder:
        uint64_t order_id
        uint64_t timestamp_ns
        uint64_t exec_timestamp_ns
        uint32_t symbol_id
        uint8_t  side
        uint8_t  order_type
        uint16_t flags
        uint64_t price
        uint32_t quantity
        uint32_t filled_qty
        uint64_t match_price
        uint32_t match_counterparty_id
        uint8_t  status
        uint8_t  reserved[3]

    ctypedef struct RawTrade:
        uint64_t trade_id
        uint64_t maker_order_id
        uint64_t taker_order_id
        uint32_t symbol_id
        uint8_t  side
        uint8_t  flags
        uint16_t reserved_h
        uint64_t price
        uint32_t quantity
        uint32_t latency_ns
        uint64_t match_timestamp_ns
        uint8_t  padding[8]

    ctypedef struct L2Entry:
        uint64_t price
        uint32_t quantity
        uint32_t order_count

    ctypedef struct L2DepthSnapshot:
        uint32_t bid_count
        uint32_t ask_count
        L2Entry bids[10]
        L2Entry asks[10]
        uint64_t best_bid
        uint64_t best_ask
        uint64_t spread
        uint64_t total_trades
        uint64_t total_volume
        uint64_t last_match_latency_ns

    ctypedef struct OrderBook:
        pass

    OrderBook* orderbook_create(uint32_t symbol_id)
    void orderbook_destroy(OrderBook* book)
    void orderbook_reset(OrderBook* book)
    int orderbook_process_order(OrderBook* book, const RawOrder* in_order, RawTrade* out_trades, int max_trades)
    bint orderbook_cancel_order(OrderBook* book, uint64_t order_id)
    void orderbook_get_l2_depth(const OrderBook* book, L2DepthSnapshot* out_depth)
    uint64_t get_current_time_ns()


cdef class CythonOrderBook:
    """
    Cython-accelerated Limit Order Book wrapping native C pointers.
    Provides sub-50 microsecond execution without Python GC overhead.
    """
    cdef OrderBook* _c_book
    cdef uint32_t _symbol_id
    cdef RawTrade _trade_buffer[128]

    def __cinit__(self, uint32_t symbol_id=1):
        self._symbol_id = symbol_id
        self._c_book = orderbook_create(symbol_id)
        if self._c_book == NULL:
            raise MemoryError("Failed to allocate native C OrderBook")

    def __dealloc__(self):
        if self._c_book != NULL:
            orderbook_destroy(self._c_book)
            self._c_book = NULL

    cpdef void reset(self):
        """Reset order book state."""
        if self._c_book != NULL:
            orderbook_reset(self._c_book)

    @cython.nogil
    cdef int c_process_order(self, const RawOrder* in_order, RawTrade* out_trades, int max_trades) nogil:
        """Process order directly with C-types and nogil."""
        return orderbook_process_order(self._c_book, in_order, out_trades, max_trades)

    @cython.nogil
    cdef bint c_cancel_order(self, uint64_t order_id) nogil:
        """Cancel order by ID in O(1) time without GIL."""
        return orderbook_cancel_order(self._c_book, order_id)

    def process_order(
        self,
        uint64_t order_id,
        uint64_t timestamp_ns,
        uint32_t symbol_id,
        uint8_t side,
        uint8_t order_type,
        uint16_t flags,
        uint64_t price,
        uint32_t quantity,
    ):
        """
        Python entrypoint for processing an order.
        Converts arguments into stack-allocated C struct and calls C engine with nogil.
        """
        cdef RawOrder order
        memset(&order, 0, sizeof(RawOrder))
        order.order_id = order_id
        order.timestamp_ns = timestamp_ns
        order.symbol_id = symbol_id
        order.side = side
        order.order_type = order_type
        order.flags = flags
        order.price = price
        order.quantity = quantity

        cdef int trade_count = 0
        with nogil:
            trade_count = orderbook_process_order(self._c_book, &order, self._trade_buffer, 128)

        # Return executed trades as list of dicts for Python inspection if any
        cdef list trades = []
        cdef int i
        for i in range(trade_count):
            trades.append({
                "trade_id": self._trade_buffer[i].trade_id,
                "maker_order_id": self._trade_buffer[i].maker_order_id,
                "taker_order_id": self._trade_buffer[i].taker_order_id,
                "symbol_id": self._trade_buffer[i].symbol_id,
                "side": self._trade_buffer[i].side,
                "flags": self._trade_buffer[i].flags,
                "price": self._trade_buffer[i].price,
                "quantity": self._trade_buffer[i].quantity,
                "latency_ns": self._trade_buffer[i].latency_ns,
                "match_timestamp_ns": self._trade_buffer[i].match_timestamp_ns,
            })
        return trades

    def cancel_order(self, uint64_t order_id):
        """Cancel an order by ID."""
        cdef bint res
        with nogil:
            res = orderbook_cancel_order(self._c_book, order_id)
        return res

    def get_l2_depth(self):
        """Get top 10 Bids and Asks depth snapshot."""
        cdef L2DepthSnapshot depth
        with nogil:
            orderbook_get_l2_depth(self._c_book, &depth)

        bids = []
        asks = []
        cdef int i
        for i in range(depth.bid_count):
            bids.append({
                "price": depth.bids[i].price,
                "quantity": depth.bids[i].quantity,
                "order_count": depth.bids[i].order_count,
            })
        for i in range(depth.ask_count):
            asks.append({
                "price": depth.asks[i].price,
                "quantity": depth.asks[i].quantity,
                "order_count": depth.asks[i].order_count,
            })

        return {
            "bids": bids,
            "asks": asks,
            "best_bid": depth.best_bid,
            "best_ask": depth.best_ask,
            "spread": depth.spread,
            "total_trades": depth.total_trades,
            "total_volume": depth.total_volume,
            "last_match_latency_ns": depth.last_match_latency_ns,
        }

    @staticmethod
    def now_ns():
        """Get high-precision nanosecond timestamp."""
        return get_current_time_ns()
