"""
ChronosMatch Binary Protocol Definition.
Zero-copy, cache-line aligned (64-byte) C-struct definitions for High-Frequency Trading.
"""

import struct
from enum import IntEnum

# Magic identifier for ChronosMatch ring buffers
MAGIC_BYTES = b"CHRN"
PROTOCOL_VERSION = 1

# Fixed-point price scale: prices stored as integer cents / ticks
# e.g., $150.25 -> 1,502,500 (scale factor 10,000 for sub-cent 4-decimal precision)
PRICE_SCALE = 10_000

class Side(IntEnum):
    BUY = 1   # Bid
    SELL = 2  # Ask

class OrderType(IntEnum):
    LIMIT = 1
    MARKET = 2
    CANCEL = 3

class OrderStatus(IntEnum):
    NEW = 0
    PARTIALLY_FILLED = 1
    FILLED = 2
    CANCELLED = 3
    REJECTED = 4

class OrderFlags(IntEnum):
    NONE = 0
    WHALE_SWEEP = 1 << 0  # Order designated to sweep multiple price levels
    IOC = 1 << 1          # Immediate or Cancel
    POST_ONLY = 1 << 2    # Add liquidity only

class Symbol(IntEnum):
    AAPL = 1
    NVDA = 2
    TSLA = 3
    MSFT = 4

SYMBOL_NAMES = {
    Symbol.AAPL: "AAPL",
    Symbol.NVDA: "NVDA",
    Symbol.TSLA: "TSLA",
    Symbol.MSFT: "MSFT",
}

SYMBOL_IDS = {name: sid for sid, name in SYMBOL_NAMES.items()}

# ==============================================================================
# Binary Struct Layouts (Cache-line aligned to 64 bytes)
# ==============================================================================
# Header format:
#   magic: 4s (4 bytes)
#   version: I (4 bytes)
#   capacity: I (4 bytes) - power of 2
#   slot_size: I (4 bytes) - 64 bytes
#   head: Q (8 bytes) - write pointer
#   tail: Q (8 bytes) - read pointer
#   dropped: Q (8 bytes)
#   total_written: Q (8 bytes)
#   total_read: Q (8 bytes)
#   padding: 8s (8 bytes)
# Total Header Size: 64 bytes
HEADER_FORMAT = "<4sII I Q Q Q Q Q 8s"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
assert HEADER_SIZE == 64, f"Header size must be exactly 64 bytes, got {HEADER_SIZE}"

# Order Struct Format (64 bytes):
#   order_id: Q (8 bytes)
#   timestamp_ns: Q (8 bytes) - ingestion / creation nanoseconds
#   exec_timestamp_ns: Q (8 bytes) - engine matching nanoseconds
#   symbol_id: I (4 bytes)
#   side: B (1 byte)
#   order_type: B (1 byte)
#   flags: H (2 bytes)
#   price: Q (8 bytes) - fixed-point price
#   quantity: I (4 bytes)
#   filled_qty: I (4 bytes)
#   match_price: Q (8 bytes)
#   match_counterparty_id: I (4 bytes)
#   status: B (1 byte)
#   reserved: 3s (3 bytes)
# Total Slot Size: 64 bytes
ORDER_FORMAT = "<QQQIBBHQIIQIB3s"
ORDER_SIZE = struct.calcsize(ORDER_FORMAT)
assert ORDER_SIZE == 64, f"Order struct size must be exactly 64 bytes, got {ORDER_SIZE}"

# Trade Execution Report Format (64 bytes):
#   trade_id: Q (8 bytes)
#   maker_order_id: Q (8 bytes)
#   taker_order_id: Q (8 bytes)
#   symbol_id: I (4 bytes)
#   side: B (1 byte) - Taker side
#   flags: B (1 byte)
#   reserved_h: H (2 bytes)
#   price: Q (8 bytes) - fixed-point execution price
#   quantity: I (4 bytes) - executed shares
#   latency_ns: I (4 bytes) - end-to-end execution latency in nanoseconds
#   match_timestamp_ns: Q (8 bytes)
#   padding: 8s (8 bytes)
TRADE_FORMAT = "<QQQIBBHQIIQ8s"
TRADE_SIZE = struct.calcsize(TRADE_FORMAT)
assert TRADE_SIZE == 64, f"Trade struct size must be exactly 64 bytes, got {TRADE_SIZE}"


def price_to_fixed(p: float) -> int:
    """Convert float price (e.g. 150.25) to fixed-point integer."""
    return int(round(p * PRICE_SCALE))

def fixed_to_price(fp: int) -> float:
    """Convert fixed-point integer back to float price."""
    return fp / PRICE_SCALE
