#pragma once

#include <nlohmann/json.hpp>

#include <any>
#include <array>
#include <cstdint>
#include <filesystem>
#include <functional>
#include <memory>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>
#include <sys/stat.h>

namespace rescue {
using Json = nlohmann::json;
namespace fs = std::filesystem;
constexpr std::size_t log_limit = 64 * 1024;

// Own descriptors, but never explicitly unlock flock: inherited references
// must keep the same open-file description locked until their work finishes.
class Fd {
    int value_ = -1;
public:
    explicit Fd(int value = -1) noexcept : value_(value) {}
    ~Fd();
    Fd(const Fd&) = delete;
    Fd& operator=(const Fd&) = delete;
    Fd(Fd&& other) noexcept : value_(other.release()) {}
    Fd& operator=(Fd&& other) noexcept;
    int get() const noexcept { return value_; }
    explicit operator bool() const noexcept { return value_ >= 0; }
    int release() noexcept;
    void reset(int value = -1) noexcept;
};

double mono();
std::string read_text(const fs::path&, std::size_t limit = log_limit);
std::string trim(std::string);
std::vector<std::string> split_words(std::string_view);
std::string canonical_json(const Json&);
std::string digest(const Json&);
Json parse_json_input(std::string_view, std::size_t limit = log_limit);
Json load_json(const fs::path&);
// Privileged inputs are opened through held, root-owned directory descriptors.
// No symlink component or non-root writable parent is accepted.
bool storage_metadata_trusted(const struct stat&, bool directory, uid_t owner);
Fd trusted_directory(const fs::path&, bool create = false);
Json load_trusted_json(const fs::path&);
void atomic_json(const fs::path&, const Json&);
std::string table_digest(const Json& targets);
Json describe(Json snapshot);

struct Owner {
    fs::path run;
    Fd fd;
    std::string epoch, boot_id;
    explicit Owner(fs::path run_dir = "/run");
    void close() noexcept { fd.reset(); }
};

struct Journal {
    fs::path path;
    Json record;
    Journal(const Owner&, const std::string& name, const std::string& map_uuid);
    void write(const std::string& phase, const Json& details = Json::object());
};

struct Evidence {
    fs::path run;
    explicit Evidence(fs::path run_dir = "/run") : run(std::move(run_dir)) {}
    void event(const Json& entry);
};

struct Observations {
    std::optional<std::uint64_t> previous;
    double last_progress = mono();
    Json transport = {{"state", "unknown"}};
    Json errors = {{"state", "incomplete"},
        {"reason", "application and filesystem history not fully observed"}};
    Json sample(const Json& info, bool failed_path);
};

void fault_hook(const std::string& stage, const Journal&, bool enabled = true);

class DeviceMapper {
    struct Impl;
    std::unique_ptr<Impl> impl_;
    Json read(const std::string&, int operation, bool inactive = false);
public:
    Json last_info;
    DeviceMapper();
    ~DeviceMapper();
    DeviceMapper(const DeviceMapper&) = delete;
    DeviceMapper& operator=(const DeviceMapper&) = delete;
    std::array<unsigned, 3> target_version(const std::string&);
    std::pair<std::string, Json> query(const std::string& name);
    Json snapshot(const std::string& name);
};

Json probe_paths(const std::string& device, std::uint64_t token);

class Events {
    Fd socket_;
    std::vector<std::string> paths_;
public:
    bool operation_ready = false, control_ready = false;
    Events();
    void watch(const std::vector<std::string>& paths = {});
    bool relevant(std::string_view data) const;
    bool wait(double seconds, int completion_fd = -1, bool defer_events = false, int control_fd = -1);
    void close() noexcept { socket_.reset(); }
};

struct Schedule {
    double next_check, event_after, delay = 0.1;
    explicit Schedule(double now) : next_check(now), event_after(now) {}
    bool due(double now, bool event = false) const;
    void completed(double now, bool recovering);
};

struct Outcome {
    std::string kind;
    std::uint64_t token = 0;
    std::any value;
    Json error;
    double elapsed = 0;
};

// One detached worker shares state independently of the controller's lifetime.
// A deadline closes admission, never cancels a kernel task in D state. The
// worker holds its flock reference until poll transfers, or cleanup frees, the
// result. Candidate values use shared_ptr in std::any so late results have one
// explicit cleanup path even after their controller exits.
class OwnedOperation {
    struct State;
    std::shared_ptr<State> state_;
public:
    using Cleanup = std::function<void(const Outcome&)>;
    explicit OwnedOperation(int owner_fd);
    ~OwnedOperation();
    OwnedOperation(const OwnedOperation&) = delete;
    OwnedOperation& operator=(const OwnedOperation&) = delete;
    int fileno() const;
    int fence_fd() const;
    bool busy() const;
    Json cleanup_error() const;
    std::uint64_t start(const std::string& kind, std::function<std::any()> fn);
    std::optional<Outcome> poll();
    void abandon(Cleanup cleanup);
    void close(Cleanup cleanup = {});
};

// Helpers run without a shell, inherit only their standard streams and the
// owner's flock descriptor, and have bounded output. After timeout SIGKILL is
// requested, but a D-state helper is still waited for with its fence intact.
std::string command(const std::vector<std::string>& args, double timeout,
                    int owner_fd = -1);
}  // namespace rescue
