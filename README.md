ChronosMatch is a Python-based high-throughput order-matching and
IPC simulation project. It explores order-book matching, a
shared-memory/ring-buffer approach, and trade-audit persistence, with
local benchmarks comparing the zero-copy path against JSON and Pickle
serialization.

> Scope: These results are from local simulation and benchmark runs.
> They do not prove live exchange connectivity, production readiness, or
> real-money trading.

## Key Features

-   Simulated order-book placement, matching, cancellation, and
    price/time priority.
-   Zero-copy IPC experiment using a memory-mapped ring buffer and
    C-style order structures.
-   Persistent trade audit ledger stored in `trades.db`.
-   Latency and throughput reporting.
-   Unit and integration tests for the order book, ring buffer, and full
    pipeline.
-   Benchmark comparison against JSON and Pickle serialization.

## Tech Stack

-   Python
-   `pytest` for automated tests
-   JSON and Pickle serialization baselines
-   Memory-mapped ring-buffer / C-structure IPC experiment
-   SQLite audit database

## Project Structure

``` text
ChronosMatch/
├── benchmarks/
│   ├── benchmark_ipc_vs_pickle.py
│   └── latency_profiler.py
├── engine/
├── simulator/
├── tests/
├── ui/
│   └── terminal_dashboard.py
├── scratch/
├── run_demo.py
├── README.md
└── trades.db
```

The exact files may differ depending on your checkout and local run.

## Setup

Open PowerShell in the repository root.

``` powershell
python --version
python -m pip install pytest
```

If the repository includes `requirements.txt`, install its dependencies
too:

``` powershell
python -m pip install -r requirements.txt
```

## Run the Demo

``` powershell
python run_demo.py --symbol AAPL --rate 2500 --duration 10
```

Options described in the project README: - `--symbol`: simulated ticker
such as `AAPL`, `NVDA`, `TSLA`, or `MSFT` - `--rate`: target generated
orders per second - `--duration`: run duration in seconds; `0` is
described as indefinite - `--headless`: print periodic console metrics
instead of the interactive terminal UI

Example:

``` powershell
python run_demo.py --symbol AAPL --rate 2500 --duration 10 --headless
```

## Run Automated Tests

``` powershell
python -m pytest -v
```

**Verified local result:** 11 tests passed in approximately 0.32 seconds
in one run.

The suite includes integration-pipeline, order-book, and ring-buffer
tests. Results may vary after code or environment changes.

## Benchmark

The IPC benchmark compares JSON serialization, Pickle serialization, and
the ChronosMatch zero-copy ring-buffer path over 1,000,000 simulated
orders.

``` powershell
python benchmarks/benchmark_ipc_vs_pickle.py 1000000
```

If the direct command does not start the script in your checkout, use
this invocation from the repository root:

``` powershell
python -c "import runpy; runpy.run_path('benchmarks/benchmark_ipc_vs_pickle.py', run_name='__main__')"
```

### Sample Local Results

These results were transcribed from one local run. They depend on
hardware, Python version, and environment.

  -----------------------------------------------------------------------------------
  IPC method        Payload Total time   Throughput   Bandwidth    Average  GC pauses
                       size                                        latency   reported
  -------------- ---------- ---------- ------------ ----------- ---------- ----------
  JSON            244 bytes    8.364 s      119,563   27.8 MB/s    8.36 µs          0
                                              ops/s                        

  Pickle          308 bytes   15.187 s 65,847 ops/s   19.3 MB/s   15.19 µs          0

  ChronosMatch     64 bytes    2.003 s      499,131   30.5 MB/s    2.00 µs          1
  zero-copy                                   ops/s                        
  -----------------------------------------------------------------------------------

In this run, ChronosMatch's reported throughput was approximately **7.6×
Pickle** and **4.2× JSON**. These are sample local results, not
universal performance guarantees.

Measurement note: The benchmark output showed one GC pause for
ChronosMatch, while the summary text also claimed zero GC collections.
Check the benchmark reporting code before making a definitive zero-GC
claim.

## Latency Profiler

The project README describes this command:

``` powershell
python benchmarks/latency_profiler.py 50000
```

Use it for a separate local latency profile if the script's command-line
interface supports the supplied count.

## Current Limitations

-   This is a local simulation/benchmark project; live exchange
    integration is not verified.
-   Benchmark numbers are machine- and environment-dependent.
-   Before presenting results as production-grade, validate the
    measurement methodology, repeat runs, and reconcile the GC-count
    reporting discrepancy.
-   Do not use this project for real-money trading without extensive
    engineering, security, reliability, and compliance review.

## Verification Summary

-   Automated tests: **11 passed** in one local run.
-   IPC benchmark: **1,000,000 simulated orders** completed in one local
    run.
-   Repository:
    [lamatif76/chronosmatch](https://github.com/lamatif76/chronosmatch).

## Author

Add your name, contact details, and links here if you want this README
to serve as a public portfolio page.


Performance Benchmark Results

ChronosMatch was tested locally to evaluate order-matching performance and inter-process communication (IPC) efficiency.

IPC Benchmark

- Test workload: 1,000,000 simulated orders
- Zero-copy throughput: Approximately 567,647 operations/second
- Zero-copy average latency: Approximately 1.76 microseconds
- Payload size: 64 bytes

Matching Engine Latency

The local latency profiler reported the following results during a 100,000-order load test:

- Median latency (p50): 3.50 microseconds
- 99th percentile latency (p99): 5.00 microseconds
- Throughput: Approximately 250,358 orders/second

Test Verification

- Automated tests: 11 passed
- Order book tests: 5 passed
- Ring buffer tests: 5 passed
- Integration tests: 1 passed

Note: These are local benchmark results. Performance may vary depending on hardware, runtime configuration, and test conditions. They do not represent verified production or live-exchange performance.