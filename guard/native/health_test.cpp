#include "health.hpp"
#include <cassert>

int main() {
    using rescue::path_status;
    auto active = path_status("2 0 0 0 1 1 A 0 1 0 8:1 A 0", "8:1");
    assert(active.current_active && !active.failed_path);
    assert(!path_status("8:10 A 0", "8:1").current_active);
    assert(!path_status("x8:1 A 0", "8:1").current_active);
    assert(!path_status("8:1 A nope", "8:1").current_active);
    assert(!path_status("8:1 A 0tail", "8:1").current_active);
    assert(!path_status("8:1 A", "8:1").current_active);
    auto mixed = path_status("8:1 A 0 8:2 F 4", "8:1");
    assert(mixed.current_active && mixed.failed_path);
    assert(path_status("8:2 F 4", "8:1").failed_path);
    assert(!path_status("8:2 F -4", "8:1").failed_path);
    assert(!path_status("8:2 F 4junk", "8:1").failed_path);
    assert(!path_status("prefix8:2 F 4", "8:1").failed_path);
    const char event[] = "change@/devices/block/sda\0ACTION=change\0SUBSYSTEM=block\0";
    assert(rescue::block_event({event, sizeof(event) - 1}));
    assert(!rescue::block_event("OTHER=SUBSYSTEM=block"));
    assert(!rescue::block_event("SUBSYSTEM=blockish"));
    assert(!rescue::block_event("SUBSYSTEM=usb"));
    assert(rescue::block_event("SUBSYSTEM=block"));
    std::uint64_t number = 0;
    assert(!rescue::decimal("-1", number));
    assert(!rescue::decimal("18446744073709551616", number));
}
