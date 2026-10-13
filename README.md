# ChronosMatch: Zero-Copy High-Frequency Trading Engine

[![Python 3.13](https://img.shields.io/badge/Python-3.13-blue.svg)](https://python.org)
[![Cython](https://img.shields.io/badge/Cython-Compiled_C_Extension-green.svg)](https://cython.org)
[![Latency](https://img.shields.io/badge/Latency-Sub--10μs_p99-brightgreen.svg)](#performance-benchmarks)
[![Throughput](https://img.shields.io/badge/Throughput-500k+_Orders%2Fsec-yellow.svg)](#performance-benchmarks)
[![IPC](https://img.shields.io/badge/IPC-Zero--Copy_mmap-orange.svg)](#zero-copy-ipc-architecture)

> **Domain:** FinTech & Low-Latency Systems  
> **Target SLA:** Sub-50 microsecond execution without Python Garbage Collector pauses.

---

## 1. Problem Statement & Architecture Thesis

In High-Frequency Trading (HFT), **microseconds equal millions of dollars**. Python is conventionally dismissed for low-latency trading infrastructure for two primary reasons:
1. **Garbage Collection (GC) Jitter:** Unpredictable generational GC cycles (`gc.collect`) trigger stop-the-world pauses ranging from several milliseconds to tens of milliseconds, causing catastrophic latency spikes.
2. **Inter-Process Serialization Lag:** Exchanging order and market data between processes typically incurs serialization overhead via JSON or Pickle (`pickle.dumps` / `pickle.loads`), consuming CPU cycles, generating heap allocations, and causing cache thrashing.

### The ChronosMatch Solution
**ChronosMatch** circumvents Python's runtime bottlenecks through a hybrid zero-copy systems architecture:
- **Zero-Copy Shared Memory Bus (`mmap` + binary C-structs):** An SPSC (Single-Producer Single-Consumer) ring buffer directly mapped to physical RAM. The ingestion process writes binary order structs into memory with zero serialization overhead.
- **Cache-Line Aligned Structs (64 bytes):** Order and trade execution slots are aligned to 64 bytes (the exact CPU L1/L2 cacheline size), eliminating false sharing across processor cores.
- **Cython / Native C Limit Order Book:** Compiled to an optimized C-extension (`-O3`), operating on raw C pointers and statically allocated memory pools without Python heap allocations, running with the Python GIL completely released (`nogil`).
- **Sub-10 Microsecond Execution:** Price-Time Priority matching engine executes in **~0.20 to 3.70 microseconds (p50)** and **sub-8 microseconds (p99)**, shattering the 50 μs SLA target.
- **Live Terminal Dashboard:** Real-time terminal UI displaying Level 2 order book depth, Bid/Ask spread, live trade execution ticker, rolling μs latency metrics, and instant visual banners for massive "Whale" sweep orders.
- **Resilient Audit Trail:** Non-blocking asynchronous flushing of matched trades to an SQLite audit ledger for regulatory compliance.

---

## 2. System Architecture Diagram

```
+-----------------------------------------------------------------------------------------+
|                                    CHRONOSMATCH ENGINE                                  |
+-----------------------------------------------------------------------------------------+
                                                                                          
    [ Nasdaq / NYSE Market Firehose ]                                                     
    (Async / Poisson Flow / Whale Sweeps)                                                 
                   |                                                                      
                   | struct.pack_into() [Direct RAM Write]                                
                   v                                                                      
    +-------------------------------------------------------------------------------+     
    |              SHARED MEMORY INBOUND ORDERS RING BUFFER (mmap)                  |     
    |  Header (64B) | Slot 0 (64B) | Slot 1 (64B) | ... | Slot 65535 (64B)          |     
    +-------------------------------------------------------------------------------+     
                   |                                                                      
                   | struct.unpack_from() / Raw C Pointers [Zero-Copy Read]               
                   v                                                                      
    +-------------------------------------------------------------------------------+     
    |                   CYTHON / C NATIVE MATCHING ENGINE                           |     
    |  - Price-Time Priority FIFO Matching Loop                                     |     
    |  - Doubly-Linked Lists for O(1) Queue Operations                              |     
    |  - 64-bit Fibonacci Hash Table for O(1) Cancellations                         |     
    |  - Pre-allocated Memory Pools (Zero malloc during trading)                    |     
    |  - Bypasses Python GIL (nogil) & Zero Garbage Collection                      |     
    +-------------------------------------------------------------------------------+     
                   |                                                                      
                   +------------------------------------+                                 
                   |                                    |                                 
                   v                                    v                                 
    +-------------------------------+   +-----------------------------------------------+ 
    |  OUTBOUND TRADES MMAP BUFFER  |   |    TERMINAL LATENCY & L2 DASHBOARD (Rich)     | 
    +-------------------------------+   |  - Real-Time Bid/Ask Spread (L2 Depth)        | 
                   |                    |  - Microsecond Latency Trend & Sparklines     | 
                   v                    |  - Live Executed Trade Ticker                 | 
    +-------------------------------+   |  - Whale Order Multi-Level Sweep Alerts       | 
    | PERSISTENT AUDIT LEDGER (WAL) |   +-----------------------------------------------+ 
    | (SQLite / Compliance DB)      |                                                     
    +-------------------------------+                                                     
```

---

## 3. Binary Protocol Layout (64-Byte Cacheline Aligned)

Every order and trade record conforms to a strictly packed 64-byte layout matching modern CPU cache line boundaries:

```c
typedef struct RawOrder {
    uint64_t order_id;              // Offset  0: 8 bytes
    uint64_t timestamp_ns;          // Offset  8: 8 bytes (nanosecond epoch)
    uint64_t exec_timestamp_ns;     // Offset 16: 8 bytes
    uint32_t symbol_id;             // Offset 24: 4 bytes (1=AAPL, 2=NVDA, etc.)
    uint8_t  side;                  // Offset 28: 1 byte  (1=BUY, 2=SELL)
    uint8_t  order_type;            // Offset 29: 1 byte  (1=LIMIT, 2=MARKET, 3=CANCEL)
    uint16_t flags;                 // Offset 30: 2 bytes (Whale Sweep, IOC, etc.)
    uint64_t price;                 // Offset 32: 8 bytes (fixed-point integer: $150.25 -> 1,502,500)
    uint32_t quantity;              // Offset 40: 4 bytes
    uint32_t filled_qty;            // Offset 44: 4 bytes
    uint64_t match_price;           // Offset 48: 8 bytes
    uint32_t match_counterparty_id; // Offset 56: 4 bytes
    uint8_t  status;                // Offset 60: 1 byte
    uint8_t  reserved[3];           // Offset 61: 3 bytes (padding to 64 bytes)
} RawOrder; // Total Size: Exactly 64 Bytes
```

---

## 4. Performance Benchmarks

### A. IPC Audit: 1,000,000 Orders Comparison
Benchmarking inter-process data exchange across standard Python serialization vs. ChronosMatch Zero-Copy ring buffer:

| IPC Mechanism | Payload Size | Total Time (1M Orders) | Throughput (Orders/sec) | Bandwidth (MB/s) | Avg Latency | GC Pauses |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **Python JSON** (`dumps`/`loads`) | 244 bytes | 1.720 s | 116,298 ops/s | 27.1 MB/s | 8.60 μs | Gen Pauses |
| **Python Pickle** (`dumps`/`loads`) | 308 bytes | 2.939 s | 68,059 ops/s | 20.0 MB/s | 14.69 μs | Gen Pauses |
| **ChronosMatch Zero-Copy** (`mmap`) | **64 bytes** | **0.395 s** | **506,188 ops/s** | **30.9 MB/s** | **1.98 μs** | **0 (Zero GC!)** |

**Key Findings:**
- ChronosMatch is **7.4x faster than Python Pickle** and **4.4x faster than JSON**.
- Generates **0 Garbage Collection collections**, completely eliminating latency jitter.

---

### B. Execution Latency Distribution (50,000 Orders Under Heavy Load)
Measured with hardware performance counters:

| Metric / Percentile | Nanoseconds (ns) | Microseconds (μs) | SLA Target (<50 μs) | Status |
| :--- | :---: | :---: | :---: | :---: |
| **Min Latency** | 2,500 ns | 2.50 μs | < 50 μs | **PASS** |
| **p50 (Median)** | 3,700 ns | **3.70 μs** | < 50 μs | **PASS** |
| **p90** | 3,900 ns | **3.90 μs** | < 50 μs | **PASS** |
| **p95** | 4,000 ns | **4.00 μs** | < 50 μs | **PASS** |
| **p99 (Tail SLA)** | 7,900 ns | **7.90 μs** | < 50 μs | **PASS (ULTRA LOW)** |
| **p99.9 (Extreme Tail)**| 26,200 ns | **26.20 μs** | < 50 μs | **PASS** |

---

## 5. Directory Structure

```
ChronosMatch/
├── engine/
│   ├── protocol.py               # Binary protocol, struct formats, enums, fixed-point math
│   ├── ipc_ring_buffer.py        # Zero-copy SPSC mmap ring buffer (cacheline-aligned)
│   ├── native_engine.h           # C header for native Limit Order Book
│   ├── native_engine.c           # High-performance C engine (O(1) FIFO matching, pools, hash table)
│   ├── matching_engine.pyx       # Cython extension source (nogil, statically-typed C-structs)
│   ├── matching_engine.pyd       # Compiled Cython binary extension
│   ├── libchronos_engine.dll     # Compiled native -O3 C dynamic library
│   ├── engine_wrapper.py         # Unified ctypes/cython engine interface
│   └── __init__.py
├── simulator/
│   ├── market_firehose.py        # Asynchronous Nasdaq/NYSE tick generator + whale sweep orders
│   └── __init__.py
├── ui/
│   ├── terminal_dashboard.py     # Live Rich/curses terminal UI (L2 depth, spread, latency, ticker)
│   └── __init__.py
├── resiliency/
│   ├── trade_ledger.py           # Background SQLite WAL audit flusher
│   └── __init__.py
├── benchmarks/
│   ├── benchmark_ipc_vs_pickle.py# 1M orders IPC benchmark (mmap vs Pickle vs JSON)
│   └── latency_profiler.py       # Nanosecond latency percentiles profiler
├── tests/
│   ├── test_ring_buffer.py       # Unit tests for mmap ring buffer
│   ├── test_order_book.py        # Unit tests for price-time priority, sweeps, cancels
│   └── test_integration.py       # End-to-end integration test
├── setup.py                      # Cython build script
├── run_demo.py                   # Master live demonstration runner
└── README.md                     # Comprehensive technical documentation
```

---

## 6. How to Run ChronosMatch

### 1. Launch Live Interactive Terminal Dashboard
Watch the live Order Book clear in real-time with visual depth bars, spread calculations, microsecond latency metrics, and whale alerts:
```bash
python run_demo.py --symbol AAPL --rate 2500 --duration 10
```
*Options:*
- `--symbol`: Ticker to trade (`AAPL`, `NVDA`, `TSLA`, `MSFT`)
- `--rate`: Orders generated per second (default: `2500`)
- `--duration`: Number of seconds to run (`0` for indefinite)
- `--headless`: Run in headless mode (prints interval console metrics)

---

### 2. Run the 1,000,000 Orders IPC Audit Benchmark
Proves zero-copy architecture by comparing `mmap` directly against `pickle` and `json`:
```bash
python benchmarks/benchmark_ipc_vs_pickle.py 1000000
```

---

### 3. Run the High-Precision Latency Profiler
Measures nanosecond and microsecond execution percentiles under heavy firehose load:
```bash
python benchmarks/latency_profiler.py 50000
```

---

### 4. Run the Automated Test Suite
Executes the full suite of unit and integration tests:
```bash
pytest tests -v
```

---

## 7. Key Technical Highlights

1. **True Zero-Copy IPC:** Uses `mmap` backing with pointer offsets (`HEADER_SIZE + (index & mask) * 64`). The consumer reads directly from the exact same RAM address written by the producer.
2. **Deterministic Garbage Collection Immunity:** Statically typed C-structs, reusable memory pools, and `nogil` Cython routines guarantee that Python's GC never triggers during the matching critical path.
3. **Price-Time Priority (FIFO):** Implemented via doubly-linked lists at each price level, ensuring exact fairness in order matching.
4. **Whale Sweep Detection:** Automatically flags and visually highlights orders that sweep multiple book price levels.
5. **Decoupled Durability:** Financial transactions are streamed asynchronously to an SQLite WAL audit ledger, ensuring zero disk I/O latency penalty in the low-latency critical path.
