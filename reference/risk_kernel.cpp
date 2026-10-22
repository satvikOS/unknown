// C++20 offline pretrade example; no network/broker capability.
#include <cassert>
#include <cstdint>
#include <iostream>
#include <limits>

enum class Reason { approved, stopped, stale, invalid, overflow, cash, limit };
struct Intent { std::int64_t quantity, limit_cents, fee_cents, quote_age_ns; };
struct Policy { bool stopped; std::int64_t free_cash, max_notional, max_age_ns; };

Reason check_buy(const Intent& i, const Policy& p) noexcept {
    if (p.stopped) return Reason::stopped;
    if (i.quantity <= 0 || i.limit_cents <= 0 || i.fee_cents < 0 ||
        i.quote_age_ns < 0 || p.free_cash < 0 || p.max_notional < 0 || p.max_age_ns < 0)
        return Reason::invalid;
    if (i.quote_age_ns > p.max_age_ns) return Reason::stale;
    constexpr auto max = std::numeric_limits<std::int64_t>::max();
    if (i.quantity > (max - i.fee_cents) / i.limit_cents) return Reason::overflow;
    const auto notional = i.quantity * i.limit_cents;
    if (notional > p.max_notional) return Reason::limit;
    if (notional + i.fee_cents > p.free_cash) return Reason::cash;
    return Reason::approved;
}

int main() {
    Policy p{false, 10000, 9500, 1000000};
    assert(check_buy({10,900,10,0},p)==Reason::approved);
    assert(check_buy({10,900,1001,0},p)==Reason::cash);
    assert(check_buy({11,900,0,0},p)==Reason::limit);
    assert(check_buy({0,900,0,0},p)==Reason::invalid);
    assert(check_buy({1,0,0,0},p)==Reason::invalid);
    assert(check_buy({1,1,-1,0},p)==Reason::invalid);
    assert(check_buy({1,1,0,-1},p)==Reason::invalid);
    assert(check_buy({1,1,0,1000000},p)==Reason::approved);
    assert(check_buy({1,1,0,1000001},p)==Reason::stale);
    assert(check_buy({std::numeric_limits<std::int64_t>::max(),2,0,0},p)==Reason::overflow);
    p.stopped=true;
    assert(check_buy({1,1,0,0},p)==Reason::stopped);
    std::cout << "11 native boundary checks passed\n";
}
