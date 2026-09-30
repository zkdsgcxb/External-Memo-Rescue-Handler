// Experimental read-only observer. It deliberately cannot admit devices,
// change DM tables, acquire the Guard owner, or reset a recovery deadline.
#include "health.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <cerrno>
#include <cstring>
#include <dlfcn.h>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <memory>
#include <poll.h>
#include <stdexcept>
#include <string>
#include <system_error>
#include <tuple>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <linux/netlink.h>
#include <unistd.h>

namespace {
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;

struct Options {
    std::string map, uuid, node, sys_path;
    std::uint64_t diskseq = 0;
    double seconds = 30;
};

Options options(int argc, char** argv) {
    Options value;
    for (int i = 1; i < argc; i += 2) {
        const std::string key = argv[i];
        if (i + 1 >= argc) throw std::runtime_error("Every option requires a value");
        const std::string text = argv[i + 1];
        if (key == "--map") value.map = text;
        else if (key == "--uuid") value.uuid = text;
        else if (key == "--node") value.node = text;
        else if (key == "--sys-path") value.sys_path = text;
        else if (key == "--diskseq") {
            if (!rescue::decimal(text, value.diskseq) || !value.diskseq)
                throw std::runtime_error("Invalid diskseq");
        } else if (key == "--seconds") {
            std::size_t end = 0;
            value.seconds = std::stod(text, &end);
            if (end != text.size() || !(value.seconds > 0 && value.seconds <= 3600))
                throw std::runtime_error("Duration must be in (0, 3600] seconds");
        } else throw std::runtime_error("Unknown option: " + key);
    }
    if (value.map.empty() || value.uuid.empty() || value.node.empty() ||
        value.sys_path.empty() || !value.diskseq)
        throw std::runtime_error("Required: --map --uuid --node --sys-path --diskseq; optional: --seconds");
    if (!std::filesystem::path(value.node).is_absolute() ||
        !std::filesystem::path(value.sys_path).is_absolute())
        throw std::runtime_error("Device and sysfs paths must be absolute");
    return value;
}

std::string json_string(const std::string& value) {
    std::string result = "\"";
    const char* hex = "0123456789abcdef";
    for (unsigned char c : value) {
        if (c == '"' || c == '\\') { result += '\\'; result += c; }
        else if (c < 32) {
            result += "\\u00"; result += hex[c >> 4]; result += hex[c & 15];
        } else result += c;
    }
    return result + '"';
}

// libdevmapper's public ABI. No private symbols or kernel structure layouts
// are used. These declarations match runtime/linux_abi.py; dlsym keeps this
// optional prototype buildable without installing development headers.
struct DMInfo {
    int exists, suspended, live_table, inactive_table;
    std::int32_t open_count;
    std::uint32_t event_nr, major, minor;
    int read_only;
    std::int32_t target_count;
    int deferred_remove, internal_suspend;
};
struct DMVersion { std::uint32_t next, version[3]; };
static_assert(sizeof(DMInfo) == 48);
static_assert(sizeof(DMVersion) == 16);

class Mapper {
    void* library_ = nullptr;
    template<class T> T symbol(const char* name) {
        void* value = dlsym(library_, name);
        if (!value) throw std::runtime_error(std::string("Missing libdevmapper symbol: ") + name);
        // POSIX specifies the dlsym/function-pointer conversion.
        return reinterpret_cast<T>(value);
    }
    void* (*create_)(int) = nullptr;
    void (*destroy_)(void*) = nullptr;
    int (*name_)(void*, const char*) = nullptr;
    int (*run_)(void*) = nullptr;
    const char* (*uuid_)(void*) = nullptr;
    int (*info_)(void*, DMInfo*) = nullptr;
    void* (*next_)(void*, void*, std::uint64_t*, std::uint64_t*, char**, char**) = nullptr;
    void* (*versions_)(void*) = nullptr;
    using Task = std::unique_ptr<void, void (*)(void*)>;
    Task task(int operation) {
        void* raw = create_(operation);
        if (!raw) throw std::runtime_error("Cannot allocate DM task");
        return Task(raw, destroy_);
    }
public:
    Mapper() {
        library_ = dlopen("libdevmapper.so.1.02.1", RTLD_NOW | RTLD_LOCAL);
        if (!library_) throw std::runtime_error(dlerror());
        try {
            create_ = symbol<decltype(create_)>("dm_task_create");
            destroy_ = symbol<decltype(destroy_)>("dm_task_destroy");
            name_ = symbol<decltype(name_)>("dm_task_set_name");
            run_ = symbol<decltype(run_)>("dm_task_run");
            uuid_ = symbol<decltype(uuid_)>("dm_task_get_uuid");
            info_ = symbol<decltype(info_)>("dm_task_get_info");
            next_ = symbol<decltype(next_)>("dm_get_next_target");
            versions_ = symbol<decltype(versions_)>("dm_task_get_versions");
        } catch (...) { dlclose(library_); throw; }
    }
    ~Mapper() { dlclose(library_); }
    Mapper(const Mapper&) = delete;
    Mapper& operator=(const Mapper&) = delete;

    void require_probe_interface() {
        auto current = task(16);  // DM_DEVICE_LIST_VERSIONS, public libdevmapper enum.
        if (!run_(current.get())) throw std::runtime_error("Cannot query DM target versions");
        auto* cursor = static_cast<char*>(versions_(current.get()));
        while (cursor) {
            const auto* entry = reinterpret_cast<const DMVersion*>(cursor);
            if (std::string_view(cursor + sizeof(DMVersion)) == "multipath") {
                if (std::make_tuple(entry->version[0], entry->version[1], entry->version[2]) <
                    std::make_tuple(1U, 15U, 0U))
                    throw std::runtime_error("Multipath target >= 1.15.0 is required");
                return;
            }
            if (!entry->next) break;
            cursor += entry->next;
        }
        throw std::runtime_error("Multipath target is not loaded");
    }

    rescue::PathStatus query(const Options& expected, const std::string& device) {
        auto current = task(10);  // DM_DEVICE_STATUS, never a table mutation.
        if (!name_(current.get(), expected.map.c_str()) || !run_(current.get()))
            throw std::runtime_error("Cannot query DM map");
        DMInfo info{};
        const char* uuid = uuid_(current.get());
        if (!info_(current.get(), &info) || !info.exists || !uuid || uuid != expected.uuid)
            throw std::runtime_error("Unexpected stable map identity");
        std::uint64_t start = 0, size = 0;
        char *kind = nullptr, *parameters = nullptr;
        void* remaining = next_(current.get(), nullptr, &start, &size, &kind, &parameters);
        if (remaining || !kind || std::string_view(kind) != "multipath" || info.target_count != 1)
            throw std::runtime_error("Expected one multipath target");
        return rescue::path_status(parameters ? parameters : "", device);
    }
};

class Device {
    const Options& expected_;
    std::filesystem::path class_path_;
    dev_t dev_;
public:
    explicit Device(const Options& expected) : expected_(expected),
        class_path_(std::filesystem::path("/sys/class/block") /
                    std::filesystem::path(expected.node).filename()) {
        struct stat info{};
        if (stat(expected.node.c_str(), &info) || !S_ISBLK(info.st_mode))
            throw std::runtime_error("Initial node is not a block device");
        dev_ = info.st_rdev;
    }
    std::string number() const {
        return std::to_string(major(dev_)) + ':' + std::to_string(minor(dev_));
    }
    bool present() const {
        std::error_code error;
        auto path = std::filesystem::canonical(class_path_, error);
        if (error || path.string() != expected_.sys_path) return false;
        std::ifstream input(path.parent_path() / "diskseq");
        std::uint64_t sequence = 0;
        if (!(input >> sequence) || sequence != expected_.diskseq) return false;
        struct stat info{};
        return stat(expected_.node.c_str(), &info) == 0 && S_ISBLK(info.st_mode) && info.st_rdev == dev_;
    }
};

class Events {
    int fd_ = -1;
public:
    Events() {
        fd_ = socket(AF_NETLINK, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, NETLINK_KOBJECT_UEVENT);
        if (fd_ < 0) throw std::system_error(errno, std::generic_category(), "netlink socket");
        int bytes = 256 * 1024;
        sockaddr_nl address{};
        address.nl_family = AF_NETLINK;
        address.nl_groups = 1;
        if (setsockopt(fd_, SOL_SOCKET, SO_RCVBUF, &bytes, sizeof(bytes)) ||
            bind(fd_, reinterpret_cast<sockaddr*>(&address), sizeof(address))) {
            const int error = errno; close(fd_); fd_ = -1;
            throw std::system_error(error, std::generic_category(), "netlink setup");
        }
    }
    ~Events() { close(fd_); }
    Events(const Events&) = delete;
    Events& operator=(const Events&) = delete;

    bool wait(Clock::time_point deadline, bool defer) {
        const auto left = std::chrono::ceil<std::chrono::milliseconds>(deadline - Clock::now()).count();
        pollfd descriptor{fd_, POLLIN, 0};
        const int ready = poll(defer ? nullptr : &descriptor, defer ? 0 : 1,
                               static_cast<int>(std::max<std::int64_t>(0, left)));
        if (ready < 0) {
            if (errno == EINTR) return false;
            throw std::system_error(errno, std::generic_category(), "netlink poll");
        }
        if (!ready) return false;
        bool relevant = false;
        for (int i = 0; i < 64; ++i) {
            std::array<char, 16384> data{};
            sockaddr_nl peer{};
            socklen_t length = sizeof(peer);
            const auto count = recvfrom(fd_, data.data(), data.size(), 0,
                                       reinterpret_cast<sockaddr*>(&peer), &length);
            if (count < 0) {
                if (errno == EAGAIN || errno == EWOULDBLOCK) break;
                if (errno == ENOBUFS) return true;
                throw std::system_error(errno, std::generic_category(), "netlink receive");
            }
            if (peer.nl_pid == 0 && rescue::block_event(std::string_view(data.data(), count)))
                relevant = true;
        }
        // Unrelated event storms are also bounded; no busy receive loop.
        if (!relevant) poll(nullptr, 0, 50);
        return relevant;
    }
};

int observe(const Options& expected) {
    Mapper mapper;
    mapper.require_probe_interface();
    Device device(expected);
    Events events;
    const auto started = Clock::now();
    const auto end = started + std::chrono::duration<double>(expected.seconds);
    auto next = started, event_after = started;
    bool pending = false, fault = false;
    while (Clock::now() < end) {
        const auto now = Clock::now();
        if (now >= next || (pending && now >= event_after)) {
            const auto status = mapper.query(expected, device.number());
            const bool present = device.present();
            const bool healthy = present && !status.failed_path && status.current_active;
            fault |= !healthy;
            std::cout << "{\"state\":\"" << (healthy ? "ready" : "path-unavailable")
                      << "\",\"time\":" << std::chrono::duration<double>(now - started).count()
                      << ",\"present\":" << (present ? "true" : "false")
                      << ",\"current_active\":" << (status.current_active ? "true" : "false")
                      << ",\"failed_path\":" << (status.failed_path ? "true" : "false") << "}\n" << std::flush;
            next = Clock::now() + 1s;
            event_after = Clock::now() + 100ms;
            pending = false;
        }
        auto wake = pending ? std::min(next, event_after) : next;
        wake = std::min(wake, std::chrono::time_point_cast<Clock::duration>(end));
        pending = events.wait(wake, pending && Clock::now() < event_after) || pending;
    }
    return fault ? 2 : 0;
}
}  // namespace

int main(int argc, char** argv) {
    try { return observe(options(argc, argv)); }
    catch (const std::exception& error) {
        std::cout << "{\"state\":\"control-uncertain\",\"reason\":" << json_string(error.what()) << "}\n";
        return 1;
    }
}
