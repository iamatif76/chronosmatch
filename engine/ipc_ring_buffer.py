"""
ChronosMatch Zero-Copy IPC Ring Buffer.
Uses memory-mapped files (mmap) and raw binary C-structs (struct.pack_into / unpack_from)
to achieve ultra-low-latency inter-process communication with zero serialization overhead.
"""

import os
import mmap
import time
import struct
from typing import Optional, Tuple, List
from engine.protocol import (
    MAGIC_BYTES,
    PROTOCOL_VERSION,
    HEADER_SIZE,
    HEADER_FORMAT,
    ORDER_SIZE,
    ORDER_FORMAT,
    TRADE_SIZE,
    TRADE_FORMAT,
    Side,
    OrderType,
    OrderStatus,
    OrderFlags,
    Symbol,
)


class RingBufferOverflowError(Exception):
    """Raised when writing to a saturated ring buffer with no available slots."""
    pass


class MmapRingBuffer:
    """
    High-performance Single-Producer Single-Consumer (SPSC) Zero-Copy Ring Buffer
    backed by memory-mapped system RAM.

    Layout:
    [ HEADER (64 bytes, cache-aligned) ]
    [ SLOT 0 (64 bytes) ]
    [ SLOT 1 (64 bytes) ]
    ...
    [ SLOT (capacity - 1) (64 bytes) ]
    """

    def __init__(
        self,
        filepath: str,
        capacity: int = 65536,
        slot_size: int = ORDER_SIZE,
        create: bool = False,
    ):
        """
        Initialize or attach to a shared memory ring buffer.

        :param filepath: Path to the backing file mapped to memory.
        :param capacity: Number of order slots (must be a power of 2 for fast bitmasking).
        :param slot_size: Byte size of each slot (default 64 bytes).
        :param create: If True, creates and initializes the backing file.
        """
        self.filepath = os.path.abspath(filepath)
        self.slot_size = slot_size

        # Ensure capacity is a power of 2
        if capacity & (capacity - 1) != 0:
            raise ValueError(f"Capacity must be a power of 2, got {capacity}")
        self.capacity = capacity
        self.mask = capacity - 1

        self.total_size = HEADER_SIZE + (self.capacity * self.slot_size)

        if create:
            os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
            if os.path.exists(self.filepath):
                try:
                    os.remove(self.filepath)
                except Exception:
                    pass
            try:
                self.file_obj = open(self.filepath, "w+b")
            except Exception:
                self.file_obj = open(self.filepath, "r+b")

            self.file_obj.seek(0)
            self.file_obj.truncate(self.total_size)
            self.file_obj.flush()
            self.mm = mmap.mmap(self.file_obj.fileno(), self.total_size)

            # Initialize Header
            # magic, version, capacity, slot_size, head, tail, dropped, total_written, total_read, padding
            header_bytes = struct.pack(
                HEADER_FORMAT,
                MAGIC_BYTES,
                PROTOCOL_VERSION,
                self.capacity,
                self.slot_size,
                0,  # head
                0,  # tail
                0,  # dropped
                0,  # total_written
                0,  # total_read
                b"\x00" * 8,
            )
            self.mm[0:HEADER_SIZE] = header_bytes
            self.mm.flush()
        else:
            if not os.path.exists(self.filepath):
                raise FileNotFoundError(f"Backing file not found: {self.filepath}")

            self.file_obj = open(self.filepath, "r+b")
            self.mm = mmap.mmap(self.file_obj.fileno(), 0)

            # Validate Header
            magic, version, cap, s_size, _, _, _, _, _, _ = struct.unpack_from(
                HEADER_FORMAT, self.mm, 0
            )
            if magic != MAGIC_BYTES:
                raise ValueError(f"Invalid magic bytes in ring buffer header: {magic}")
            self.capacity = cap
            self.mask = self.capacity - 1
            self.slot_size = s_size

        # Memoryview for zero-copy slicing
        self.buf = memoryview(self.mm)

    # --------------------------------------------------------------------------
    # Header Atomic Pointer Accessors (Offsets within 64-byte Header)
    # --------------------------------------------------------------------------
    # Offsets in HEADER_FORMAT ("<4sII I Q Q Q Q Q 8s"):
    #   magic (0..4)
    #   version (4..8)
    #   capacity (8..12)
    #   slot_size (12..16)
    #   head (16..24) - uint64
    #   tail (24..32) - uint64
    #   dropped (32..40) - uint64
    #   total_written (40..48) - uint64
    #   total_read (48..56) - uint64

    def get_head(self) -> int:
        """Get current write sequence number."""
        return struct.unpack_from("<Q", self.buf, 16)[0]

    def set_head(self, val: int) -> None:
        """Update write sequence number."""
        struct.pack_into("<Q", self.buf, 16, val)

    def get_tail(self) -> int:
        """Get current read sequence number."""
        return struct.unpack_from("<Q", self.buf, 24)[0]

    def set_tail(self, val: int) -> None:
        """Update read sequence number."""
        struct.pack_into("<Q", self.buf, 24, val)

    def get_dropped(self) -> int:
        """Get count of dropped orders due to buffer saturation."""
        return struct.unpack_from("<Q", self.buf, 32)[0]

    def inc_dropped(self) -> None:
        """Increment dropped order counter."""
        d = struct.unpack_from("<Q", self.buf, 32)[0]
        struct.pack_into("<Q", self.buf, 32, d + 1)

    def get_total_written(self) -> int:
        return struct.unpack_from("<Q", self.buf, 40)[0]

    def inc_total_written(self, count: int = 1) -> None:
        val = struct.unpack_from("<Q", self.buf, 40)[0]
        struct.pack_into("<Q", self.buf, 40, val + count)

    def get_total_read(self) -> int:
        return struct.unpack_from("<Q", self.buf, 48)[0]

    def inc_total_read(self, count: int = 1) -> None:
        val = struct.unpack_from("<Q", self.buf, 48)[0]
        struct.pack_into("<Q", self.buf, 48, val + count)

    @property
    def occupancy(self) -> int:
        """Number of unread slots currently queued in the ring buffer."""
        head = self.get_head()
        tail = self.get_tail()
        return head - tail

    @property
    def free_slots(self) -> int:
        """Available unwritten slots before ring buffer saturates."""
        return self.capacity - self.occupancy

    # --------------------------------------------------------------------------
    # Zero-Copy Order Writing
    # --------------------------------------------------------------------------

    def write_order_raw(
        self,
        order_id: int,
        timestamp_ns: int,
        exec_timestamp_ns: int,
        symbol_id: int,
        side: int,
        order_type: int,
        flags: int,
        price: int,
        quantity: int,
        filled_qty: int = 0,
        match_price: int = 0,
        match_counterparty_id: int = 0,
        status: int = OrderStatus.NEW,
    ) -> bool:
        """
        Packs an order directly into the next mmap slot at C-level speed.
        Returns True if successful, False if buffer was full (overflow).
        """
        head = self.get_head()
        tail = self.get_tail()

        if head - tail >= self.capacity:
            # Buffer is full
            self.inc_dropped()
            return False

        slot_idx = head & self.mask
        offset = HEADER_SIZE + (slot_idx * self.slot_size)

        struct.pack_into(
            ORDER_FORMAT,
            self.buf,
            offset,
            order_id,
            timestamp_ns,
            exec_timestamp_ns,
            symbol_id,
            side,
            order_type,
            flags,
            price,
            quantity,
            filled_qty,
            match_price,
            match_counterparty_id,
            status,
            b"\x00\x00\x00",
        )

        # Increment head write pointer
        self.set_head(head + 1)
        self.inc_total_written(1)
        return True

    def read_order_raw(self) -> Optional[Tuple]:
        """
        Reads the next order from the ring buffer directly from RAM.
        Returns unpacked tuple or None if ring buffer is empty.
        """
        head = self.get_head()
        tail = self.get_tail()

        if tail >= head:
            # Buffer is empty
            return None

        slot_idx = tail & self.mask
        offset = HEADER_SIZE + (slot_idx * self.slot_size)

        order_data = struct.unpack_from(ORDER_FORMAT, self.buf, offset)

        # Advance tail read pointer
        self.set_tail(tail + 1)
        self.inc_total_read(1)
        return order_data

    def read_order_batch(self, max_batch: int = 1024) -> List[Tuple]:
        """
        High-throughput batch read. Reads up to `max_batch` available orders
        with a single atomic update to the tail pointer.
        """
        head = self.get_head()
        tail = self.get_tail()
        available = head - tail

        if available <= 0:
            return []

        count = min(available, max_batch)
        orders = []
        mask = self.mask
        slot_sz = self.slot_size
        buf = self.buf

        for i in range(count):
            slot_idx = (tail + i) & mask
            offset = HEADER_SIZE + (slot_idx * slot_sz)
            orders.append(struct.unpack_from(ORDER_FORMAT, buf, offset))

        # Advance tail atomically by batch size
        self.set_tail(tail + count)
        self.inc_total_read(count)
        return orders

    # --------------------------------------------------------------------------
    # Zero-Copy Trade Report Writing (For Executions / Fills)
    # --------------------------------------------------------------------------

    def write_trade_raw(
        self,
        trade_id: int,
        maker_order_id: int,
        taker_order_id: int,
        symbol_id: int,
        side: int,
        flags: int,
        price: int,
        quantity: int,
        latency_ns: int,
        match_timestamp_ns: int,
    ) -> bool:
        """Packs a matched trade execution report into trade ring buffer."""
        head = self.get_head()
        tail = self.get_tail()

        if head - tail >= self.capacity:
            self.inc_dropped()
            return False

        slot_idx = head & self.mask
        offset = HEADER_SIZE + (slot_idx * self.slot_size)

        struct.pack_into(
            TRADE_FORMAT,
            self.buf,
            offset,
            trade_id,
            maker_order_id,
            taker_order_id,
            symbol_id,
            side,
            flags,
            0,  # reserved_h
            price,
            quantity,
            latency_ns,
            match_timestamp_ns,
            b"\x00" * 8,
        )

        self.set_head(head + 1)
        self.inc_total_written(1)
        return True

    def read_trade_raw(self) -> Optional[Tuple]:
        """Reads the next matched trade execution report."""
        head = self.get_head()
        tail = self.get_tail()

        if tail >= head:
            return None

        slot_idx = tail & self.mask
        offset = HEADER_SIZE + (slot_idx * self.slot_size)
        trade_data = struct.unpack_from(TRADE_FORMAT, self.buf, offset)

        self.set_tail(tail + 1)
        self.inc_total_read(1)
        return trade_data

    def read_trade_batch(self, max_batch: int = 1024) -> List[Tuple]:
        """Reads a batch of trade execution reports."""
        head = self.get_head()
        tail = self.get_tail()
        available = head - tail

        if available <= 0:
            return []

        count = min(available, max_batch)
        trades = []
        mask = self.mask
        slot_sz = self.slot_size
        buf = self.buf

        for i in range(count):
            slot_idx = (tail + i) & mask
            offset = HEADER_SIZE + (slot_idx * slot_sz)
            trades.append(struct.unpack_from(TRADE_FORMAT, buf, offset))

        self.set_tail(tail + count)
        self.inc_total_read(count)
        return trades

    def close(self) -> None:
        """Close memory map and file handles."""
        try:
            self.buf.release()
            self.mm.close()
            self.file_obj.close()
        except Exception:
            pass
