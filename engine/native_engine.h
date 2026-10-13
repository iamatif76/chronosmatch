#ifndef CHRONOS_NATIVE_ENGINE_H
#define CHRONOS_NATIVE_ENGINE_H

#include <stdint.h>
#include <stdbool.h>

#ifdef _WIN32
#define EXPORT_API __declspec(dllexport)
#else
#define EXPORT_API __attribute__((visibility("default")))
#endif

#define MAX_ORDERS_POOL 262144
#define MAX_PRICE_LEVELS 4096
#define MAX_TRADES_PER_ORDER 128
#define HASH_TABLE_SIZE 524288  // 2^19 for O(1) order lookup by order_id

// Side definitions
#define SIDE_BUY 1
#define SIDE_SELL 2

// Order types
#define TYPE_LIMIT 1
#define TYPE_MARKET 2
#define TYPE_CANCEL 3

// Status
#define STATUS_NEW 0
#define STATUS_PARTIAL 1
#define STATUS_FILLED 2
#define STATUS_CANCELLED 3
#define STATUS_REJECTED 4

// Flags
#define FLAG_NONE 0
#define FLAG_WHALE_SWEEP (1 << 0)
#define FLAG_IOC (1 << 1)
#define FLAG_POST_ONLY (1 << 2)

#pragma pack(push, 1)
// 64-byte raw Order struct (matches Python binary protocol exactly)
typedef struct RawOrder {
    uint64_t order_id;
    uint64_t timestamp_ns;
    uint64_t exec_timestamp_ns;
    uint32_t symbol_id;
    uint8_t  side;
    uint8_t  order_type;
    uint16_t flags;
    uint64_t price;
    uint32_t quantity;
    uint32_t filled_qty;
    uint64_t match_price;
    uint32_t match_counterparty_id;
    uint8_t  status;
    uint8_t  reserved[3];
} RawOrder;

// 64-byte raw Trade execution report struct
typedef struct RawTrade {
    uint64_t trade_id;
    uint64_t maker_order_id;
    uint64_t taker_order_id;
    uint32_t symbol_id;
    uint8_t  side;
    uint8_t  flags;
    uint16_t reserved_h;
    uint64_t price;
    uint32_t quantity;
    uint32_t latency_ns;
    uint64_t match_timestamp_ns;
    uint8_t  padding[8];
} RawTrade;

// Level 2 Market Depth Entry
typedef struct L2Entry {
    uint64_t price;
    uint32_t quantity;
    uint32_t order_count;
} L2Entry;

// Level 2 Book Snapshot (Top 10 Bids, Top 10 Asks)
typedef struct L2DepthSnapshot {
    uint32_t bid_count;
    uint32_t ask_count;
    L2Entry bids[10];
    L2Entry asks[10];
    uint64_t best_bid;
    uint64_t best_ask;
    uint64_t spread;
    uint64_t total_trades;
    uint64_t total_volume;
    uint64_t last_match_latency_ns;
} L2DepthSnapshot;
#pragma pack(pop)

// Forward declarations
struct PriceLevelNode;

typedef struct InternalOrder {
    RawOrder raw;
    struct InternalOrder* prev;
    struct InternalOrder* next;
    struct PriceLevelNode* level;
    bool is_active;
} InternalOrder;

typedef struct PriceLevelNode {
    uint64_t price;
    uint32_t total_qty;
    uint32_t order_count;
    InternalOrder* head;
    InternalOrder* tail;
    struct PriceLevelNode* prev;
    struct PriceLevelNode* next;
} PriceLevelNode;

typedef struct OrderBook {
    uint32_t symbol_id;
    PriceLevelNode* best_bid_level;
    PriceLevelNode* best_ask_level;
    
    // Hash table for O(1) order lookup by order_id
    InternalOrder* order_hash[HASH_TABLE_SIZE];

    // Preallocated pools for zero-allocation matching loop
    InternalOrder order_pool[MAX_ORDERS_POOL];
    uint32_t order_pool_next;

    PriceLevelNode level_pool[MAX_PRICE_LEVELS];
    uint32_t level_pool_next;

    // Statistics
    uint64_t trade_seq;
    uint64_t total_trades;
    uint64_t total_volume;
    uint64_t last_match_latency_ns;
} OrderBook;

#ifdef __cplusplus
extern "C" {
#endif

EXPORT_API OrderBook* orderbook_create(uint32_t symbol_id);
EXPORT_API void orderbook_destroy(OrderBook* book);
EXPORT_API void orderbook_reset(OrderBook* book);

EXPORT_API int orderbook_process_order(
    OrderBook* book,
    const RawOrder* in_order,
    RawTrade* out_trades,
    int max_trades
);

EXPORT_API bool orderbook_cancel_order(OrderBook* book, uint64_t order_id);

EXPORT_API void orderbook_get_l2_depth(
    const OrderBook* book,
    L2DepthSnapshot* out_depth
);

EXPORT_API uint64_t get_current_time_ns(void);

#ifdef __cplusplus
}
#endif

#endif // CHRONOS_NATIVE_ENGINE_H
