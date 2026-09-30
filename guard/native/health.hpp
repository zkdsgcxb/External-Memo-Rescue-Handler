#pragma once

#include <charconv>
#include <cstdint>
#include <string_view>
#include <vector>

namespace rescue {

inline bool decimal(std::string_view text, std::uint64_t& value) {
    if (text.empty()) return false;
    const auto result = std::from_chars(text.data(), text.data() + text.size(), value);
    return result.ec == std::errc{} && result.ptr == text.data() + text.size();
}

inline bool device_number(std::string_view text) {
    const auto colon = text.find(':');
    std::uint64_t major = 0, minor = 0;
    return colon != text.npos && decimal(text.substr(0, colon), major) &&
           decimal(text.substr(colon + 1), minor);
}

struct PathStatus {
    bool current_active = false;
    bool failed_path = false;
};

// Multipath emits whitespace-delimited dev_t, state and failure-count fields.
// Match complete tokens, so e.g. 8:10 cannot be mistaken for enrolled 8:1.
inline PathStatus path_status(std::string_view status, std::string_view expected) {
    std::vector<std::string_view> fields;
    for (std::size_t start = 0; start < status.size();) {
        start = status.find_first_not_of(" \t\n", start);
        if (start == status.npos) break;
        const auto end = status.find_first_of(" \t\n", start);
        fields.push_back(status.substr(start, end == status.npos ? end : end - start));
        if (end == status.npos) break;
        start = end;
    }
    PathStatus result;
    for (std::size_t i = 0; i + 2 < fields.size(); ++i) {
        std::uint64_t failures = 0;
        if (!device_number(fields[i]) || !decimal(fields[i + 2], failures)) continue;
        result.failed_path |= fields[i + 1] == "F";
        result.current_active |= fields[i] == expected && fields[i + 1] == "A";
    }
    return result;
}

// recvmsg includes NUL-separated environment fields; substring matches could
// incorrectly accept an unrelated attribute containing SUBSYSTEM=block.
inline bool block_event(std::string_view message) {
    for (std::size_t start = 0; start < message.size();) {
        const auto end = message.find('\0', start);
        if (message.substr(start, end == message.npos ? end : end - start) == "SUBSYSTEM=block")
            return true;
        if (end == message.npos) break;
        start = end + 1;
    }
    return false;
}

}  // namespace rescue
