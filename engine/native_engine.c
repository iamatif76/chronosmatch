#include "native_engine.h"
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
static LARGE_INTEGER g_perf_freq;
static bool g_freq_init = false;

uint64_t get_current_time_ns(void) {
    if (!g_freq_init) {
        QueryPerformanceFrequency(&g_perf_freq);
        g_freq_init = true;
    }
    LARGE_INTEGER counter;
    QueryPerformanceCounter(&counter);
    // Convert to nanoseconds: (counter * 1,000,000,000) / freq
    return (uint64_t)((counter.QuadPart * 1000000000ULL) / g_perf_freq.QuadPart);
}
#else
#include <time.h>
uint64_t get_current_time_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}
#endif

// Fast 64-bit Fibonacci hashing for hash table index
static inline uint32_t hash_order_id(uint64_t id) {
    return (uint32_t)((id * 11400714819323198485ULL) >> (64 - 19));
}

// Memory pool allocators
static InternalOrder* alloc_order_node(OrderBook* book) {
    if (book->order_pool_next >= MAX_ORDERS_POOL) {
        book->order_pool_next = 0; // Wrap pool (circular reuse of completed orders)
    }
    InternalOrder* node = &book->order_pool[book->order_pool_next++];
    memset(node, 0, sizeof(InternalOrder));
    node->is_active = true;
    return node;
}

static PriceLevelNode* alloc_level_node(OrderBook* book) {
    if (book->level_pool_next >= MAX_PRICE_LEVELS) {
        book->level_pool_next = 0;
    }
    PriceLevelNode* node = &book->level_pool[book->level_pool_next++];
    memset(node, 0, sizeof(PriceLevelNode));
    return node;
}

// Hash table helpers
static void hash_insert(OrderBook* book, InternalOrder* order) {
    uint32_t bucket = hash_order_id(order->raw.order_id);
    book->order_hash[bucket] = order;
}

static InternalOrder* hash_lookup(OrderBook* book, uint64_t order_id) {
    uint32_t bucket = hash_order_id(order_id);
    InternalOrder* order = book->order_hash[bucket];
    if (order && order->is_active && order->raw.order_id == order_id) {
        return order;
    }
    return NULL;
}

static void hash_remove(OrderBook* book, uint64_t order_id) {
    uint32_t bucket = hash_order_id(order_id);
    if (book->order_hash[bucket] && book->order_hash[bucket]->raw.order_id == order_id) {
        book->order_hash[bucket]->is_active = false;
        book->order_hash[bucket] = NULL;
    }
}

// Price Level management
static PriceLevelNode* find_or_create_level(OrderBook* book, uint8_t side, uint64_t price) {
    PriceLevelNode** head_level = (side == SIDE_BUY) ? &book->best_bid_level : &book->best_ask_level;

    if (*head_level == NULL) {
        PriceLevelNode* new_lvl = alloc_level_node(book);
        new_lvl->price = price;
        *head_level = new_lvl;
        return new_lvl;
    }

    PriceLevelNode* curr = *head_level;
    PriceLevelNode* prev = NULL;

    while (curr != NULL) {
        if (curr->price == price) {
            return curr;
        }

        // Bids sorted DESCENDING (highest first)
        // Asks sorted ASCENDING (lowest first)
        bool should_insert_before = (side == SIDE_BUY) ? (price > curr->price) : (price < curr->price);

        if (should_insert_before) {
            PriceLevelNode* new_lvl = alloc_level_node(book);
            new_lvl->price = price;
            new_lvl->next = curr;
            new_lvl->prev = curr->prev;

            if (curr->prev) {
                curr->prev->next = new_lvl;
            } else {
                *head_level = new_lvl;
            }
            curr->prev = new_lvl;
            return new_lvl;
        }

        prev = curr;
        curr = curr->next;
    }

    // Insert at end of list
    PriceLevelNode* new_lvl = alloc_level_node(book);
    new_lvl->price = price;
    new_lvl->prev = prev;
    new_lvl->next = NULL;
    if (prev) {
        prev->next = new_lvl;
    }
    return new_lvl;
}

static void remove_price_level_if_empty(OrderBook* book, PriceLevelNode* lvl, uint8_t side) {
    if (lvl->total_qty == 0 || lvl->order_count == 0) {
        PriceLevelNode** head_level = (side == SIDE_BUY) ? &book->best_bid_level : &book->best_ask_level;

        if (lvl->prev) {
            lvl->prev->next = lvl->next;
        } else {
            *head_level = lvl->next;
        }

        if (lvl->next) {
            lvl->next->prev = lvl->prev;
        }

        lvl->head = NULL;
        lvl->tail = NULL;
        lvl->prev = NULL;
        lvl->next = NULL;
    }
}

// Queue order into PriceLevel (FIFO time priority)
static void add_resting_order(OrderBook* book, PriceLevelNode* lvl, InternalOrder* order) {
    order->level = lvl;
    order->next = NULL;
    order->prev = lvl->tail;

    if (lvl->tail) {
        lvl->tail->next = order;
    } else {
        lvl->head = order;
    }
    lvl->tail = order;

    lvl->total_qty += (order->raw.quantity - order->raw.filled_qty);
    lvl->order_count++;

    hash_insert(book, order);
}

// Remove order from PriceLevel
static void remove_order_from_level(OrderBook* book, InternalOrder* order) {
    PriceLevelNode* lvl = order->level;
    if (!lvl) return;

    if (order->prev) {
        order->prev->next = order->next;
    } else {
        lvl->head = order->next;
    }

    if (order->next) {
        order->next->prev = order->prev;
    } else {
        lvl->tail = order->prev;
    }

    uint32_t rem = order->raw.quantity - order->raw.filled_qty;
    if (lvl->total_qty >= rem) {
        lvl->total_qty -= rem;
    } else {
        lvl->total_qty = 0;
    }

    if (lvl->order_count > 0) {
        lvl->order_count--;
    }

    hash_remove(book, order->raw.order_id);
    remove_price_level_if_empty(book, lvl, order->raw.side);
}

// Public API Implementations
OrderBook* orderbook_create(uint32_t symbol_id) {
    OrderBook* book = (OrderBook*)calloc(1, sizeof(OrderBook));
    if (!book) return NULL;
    book->symbol_id = symbol_id;
    book->trade_seq = 1;
    return book;
}

void orderbook_destroy(OrderBook* book) {
    if (book) {
        free(book);
    }
}

void orderbook_reset(OrderBook* book) {
    if (!book) return;
    uint32_t sym = book->symbol_id;
    memset(book, 0, sizeof(OrderBook));
    book->symbol_id = sym;
    book->trade_seq = 1;
}

bool orderbook_cancel_order(OrderBook* book, uint64_t order_id) {
    if (!book) return false;
    InternalOrder* order = hash_lookup(book, order_id);
    if (!order) return false;

    remove_order_from_level(book, order);
    order->raw.status = STATUS_CANCELLED;
    return true;
}

int orderbook_process_order(
    OrderBook* book,
    const RawOrder* in_order,
    RawTrade* out_trades,
    int max_trades
) {
    if (!book || !in_order) return 0;

    uint64_t start_ns = get_current_time_ns();
    int trade_idx = 0;

    // Handle cancellation orders directly
    if (in_order->order_type == TYPE_CANCEL) {
        orderbook_cancel_order(book, in_order->order_id);
        return 0;
    }

    uint32_t remaining_qty = in_order->quantity - in_order->filled_qty;
    uint8_t taker_side = in_order->side;
    uint64_t taker_price = in_order->price;
    bool is_market = (in_order->order_type == TYPE_MARKET);

    // Matching against opposite book side:
    // If BUY taker -> match against Asks (lowest price first)
    // If SELL taker -> match against Bids (highest price first)
    PriceLevelNode** opp_head = (taker_side == SIDE_BUY) ? &book->best_ask_level : &book->best_bid_level;
    uint32_t levels_swept = 0;

    while (*opp_head != NULL && remaining_qty > 0 && trade_idx < max_trades) {
        PriceLevelNode* curr_lvl = *opp_head;
        uint64_t maker_price = curr_lvl->price;

        // Check price cross condition
        if (!is_market) {
            if (taker_side == SIDE_BUY && taker_price < maker_price) {
                break; // Buy price lower than best ask -> no cross
            }
            if (taker_side == SIDE_SELL && taker_price > maker_price) {
                break; // Sell price higher than best bid -> no cross
            }
        }

        levels_swept++;

        // FIFO match orders at this price level
        while (curr_lvl->head != NULL && remaining_qty > 0 && trade_idx < max_trades) {
            InternalOrder* maker_order = curr_lvl->head;
            uint32_t maker_rem = maker_order->raw.quantity - maker_order->raw.filled_qty;
            uint32_t fill_qty = (remaining_qty < maker_rem) ? remaining_qty : maker_rem;

            remaining_qty -= fill_qty;
            maker_order->raw.filled_qty += fill_qty;
            curr_lvl->total_qty -= fill_qty;

            uint64_t end_ns = get_current_time_ns();
            uint32_t latency_ns = (uint32_t)(end_ns - start_ns);

            // Record Trade Execution Report
            RawTrade* trade = &out_trades[trade_idx++];
            trade->trade_id = book->trade_seq++;
            trade->maker_order_id = maker_order->raw.order_id;
            trade->taker_order_id = in_order->order_id;
            trade->symbol_id = book->symbol_id;
            trade->side = taker_side;
            
            // Mark Whale sweep flag if order sweeps multiple levels or is large
            uint8_t flags = (uint8_t)(in_order->flags);
            if (levels_swept > 1 || in_order->quantity >= 5000) {
                flags |= FLAG_WHALE_SWEEP;
            }
            trade->flags = flags;
            trade->reserved_h = 0;
            trade->price = maker_price; // Execution happens at resting maker price
            trade->quantity = fill_qty;
            trade->latency_ns = latency_ns;
            trade->match_timestamp_ns = end_ns;

            book->total_trades++;
            book->total_volume += fill_qty;
            book->last_match_latency_ns = latency_ns;

            // If maker order fully filled, remove it from queue
            if (maker_order->raw.filled_qty >= maker_order->raw.quantity) {
                maker_order->raw.status = STATUS_FILLED;
                curr_lvl->head = maker_order->next;
                if (curr_lvl->head) {
                    curr_lvl->head->prev = NULL;
                } else {
                    curr_lvl->tail = NULL;
                }
                curr_lvl->order_count--;
                hash_remove(book, maker_order->raw.order_id);
            } else {
                maker_order->raw.status = STATUS_PARTIAL;
            }
        }

        // If level exhausted, clean it up and advance to next level
        if (curr_lvl->total_qty == 0 || curr_lvl->head == NULL) {
            remove_price_level_if_empty(book, curr_lvl, (taker_side == SIDE_BUY) ? SIDE_SELL : SIDE_BUY);
        }
    }

    // If remaining quantity exists and this is a LIMIT order, insert into book as resting order
    if (remaining_qty > 0 && !is_market && (in_order->flags & FLAG_IOC) == 0) {
        PriceLevelNode* lvl = find_or_create_level(book, taker_side, taker_price);
        InternalOrder* resting = alloc_order_node(book);
        memcpy(&resting->raw, in_order, sizeof(RawOrder));
        resting->raw.filled_qty = in_order->quantity - remaining_qty;
        resting->raw.status = (resting->raw.filled_qty > 0) ? STATUS_PARTIAL : STATUS_NEW;
        resting->raw.exec_timestamp_ns = start_ns;
        add_resting_order(book, lvl, resting);
    }

    return trade_idx;
}

void orderbook_get_l2_depth(const OrderBook* book, L2DepthSnapshot* out_depth) {
    if (!book || !out_depth) return;

    memset(out_depth, 0, sizeof(L2DepthSnapshot));
    out_depth->total_trades = book->total_trades;
    out_depth->total_volume = book->total_volume;
    out_depth->last_match_latency_ns = book->last_match_latency_ns;

    // Top 10 Bids
    PriceLevelNode* bid_lvl = book->best_bid_level;
    uint32_t b_count = 0;
    while (bid_lvl != NULL && b_count < 10) {
        if (bid_lvl->total_qty > 0) {
            out_depth->bids[b_count].price = bid_lvl->price;
            out_depth->bids[b_count].quantity = bid_lvl->total_qty;
            out_depth->bids[b_count].order_count = bid_lvl->order_count;
            if (b_count == 0) {
                out_depth->best_bid = bid_lvl->price;
            }
            b_count++;
        }
        bid_lvl = bid_lvl->next;
    }
    out_depth->bid_count = b_count;

    // Top 10 Asks
    PriceLevelNode* ask_lvl = book->best_ask_level;
    uint32_t a_count = 0;
    while (ask_lvl != NULL && a_count < 10) {
        if (ask_lvl->total_qty > 0) {
            out_depth->asks[a_count].price = ask_lvl->price;
            out_depth->asks[a_count].quantity = ask_lvl->total_qty;
            out_depth->asks[a_count].order_count = ask_lvl->order_count;
            if (a_count == 0) {
                out_depth->best_ask = ask_lvl->price;
            }
            a_count++;
        }
        ask_lvl = ask_lvl->next;
    }
    out_depth->ask_count = a_count;

    if (out_depth->best_bid > 0 && out_depth->best_ask > 0 && out_depth->best_ask >= out_depth->best_bid) {
        out_depth->spread = out_depth->best_ask - out_depth->best_bid;
    } else {
        out_depth->spread = 0;
    }
}
