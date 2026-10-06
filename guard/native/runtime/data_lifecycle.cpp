#include "data_lifecycle.hpp"

#include <algorithm>
#include <chrono>
#include <cstring>
#include <poll.h>
#include <regex>
#include <set>
#include <sstream>
#include <stdexcept>
#include <system_error>
#include <thread>
#include <sys/socket.h>
#include <sys/sysmacros.h>
#include <sys/un.h>
#include <unistd.h>

namespace rescue {
namespace {
void require(bool condition, const char* why) { if (!condition) throw std::runtime_error(why); }
std::string dev_number(dev_t dev) { return std::to_string(major(dev)) + ':' + std::to_string(minor(dev)); }
std::set<fs::path> entries(const fs::path& path) {
    std::set<fs::path> result;
    for (const auto& entry : fs::directory_iterator(path)) result.insert(fs::canonical(entry.path()));
    return result;
}
void zero_inflight(const fs::path& path) {
    const auto words = split_words(read_text(path / "inflight"));
    require(words.size() == 2 && words[0] == "0" && words[1] == "0", "Block requests still in flight or counters unknown");
}
sockaddr_un address(int directory_fd) {
    // A held trusted directory also avoids sockaddr_un truncation for the
    // longest valid enrolled map names. Both peers resolve the same inode.
    const auto path = "/proc/self/fd/" + std::to_string(directory_fd) + "/data-control.sock";
    sockaddr_un result{}; result.sun_family = AF_UNIX;
    require(path.size() < sizeof(result.sun_path), "Data control socket path too long");
    std::memcpy(result.sun_path, path.c_str(), path.size() + 1);
    return result;
}
ucred peer(int fd) {
    ucred result{}; socklen_t size = sizeof(result);
    require(!::getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &result, &size) && size == sizeof(result) && result.uid == 0,
            "Data control requires a root peer");
    return result;
}
Json receive(int fd, int timeout_ms) {
    pollfd item{fd, POLLIN, 0};
    require(::poll(&item, 1, timeout_ms) > 0 && (item.revents & POLLIN), "Data control response incomplete or timed out");
    std::array<char, 8192> data{};
    const auto size = ::recv(fd, data.data(), data.size(), MSG_TRUNC);
    require(size > 0 && size < static_cast<ssize_t>(data.size()), "Invalid data control packet");
    return parse_json_input(std::string_view(data.data(), size), data.size());
}
void send(int fd, const Json& value) {
    const auto text = canonical_json(value);
    require(text.size() < 8192 && ::send(fd, text.data(), text.size(), MSG_NOSIGNAL | MSG_DONTWAIT) == static_cast<ssize_t>(text.size()),
            "Data control reply not delivered");
}
Json stopped_now(const Config& config, const Json& identity, Owner& fence) {
    const auto journal = load_trusted_json(config.run / "path-transaction.json");
    const auto receipt = load_trusted_json(config.run / "safe-stop.json");
    validate_stopped(config, identity, fence.boot_id, journal, receipt);
    check_cgroup(journal.at("owner_cgroup"), 0);
    DeviceMapper mapper;
    idle_data_snapshot(config, journal.at("stopped_instance"), mapper, false);
    return {{"state", "stopped"}, {"outcome", "safe_data_owner_exit"},
        {"owner_epoch", journal.at("owner_epoch")}, {"map_preserved", true}, {"shared_ram_preserved", true},
        {"receipt_digest", digest(receipt)}, {"raw_open_descriptors", "not_observable"}};
}
} // namespace

bool queue_enabled(const Json& snapshot) {
    const auto& active = snapshot.at("active");
    require(active.size() == 1 && active[0][2] == "multipath", "Not a single multipath table");
    const auto words = split_words(active[0][3].get<std::string>());
    require(!words.empty() && std::regex_match(words[0], std::regex("[0-9]{1,3}")), "Invalid multipath feature count");
    const auto count = std::stoul(words[0]);
    require(count < words.size(), "Truncated multipath features");
    return std::find(words.begin() + 1, words.begin() + 1 + count, "queue_if_no_path") != words.begin() + 1 + count;
}
Json path_instance(const std::string& node) {
    struct stat info{};
    require(!::stat(node.c_str(), &info) && S_ISBLK(info.st_mode), "Data path missing or not a block device");
    const auto sys = fs::canonical(fs::path("/sys/dev/block") / dev_number(info.st_rdev));
    require(fs::exists(sys / "partition"), "Data path is not a partition");
    return {{"node", node}, {"dev", dev_number(info.st_rdev)}, {"sys_path", sys.string()},
        {"diskseq", std::stoull(trim(read_text(sys.parent_path() / "diskseq")))}};
}
Json observed_config(const Config& config, const Json& instance) {
    auto value = config.value;
    value["initial_node"] = instance.at("node"); value["initial_sys_path"] = instance.at("sys_path");
    value["initial_diskseq"] = instance.at("diskseq");
    return value;
}
std::string own_cgroup() {
    std::istringstream lines(read_text("/proc/self/cgroup")); std::string line;
    while (std::getline(lines, line)) if (line.rfind("0::/", 0) == 0) return line.substr(3);
    throw std::runtime_error("Unified controller cgroup unavailable");
}
void check_proc_cgroup(const std::string& path, pid_t allowed, const fs::path& proc) {
    // The established RAM /sys bind does not necessarily include the cgroup2
    // submount. Membership remains observable without ptrace through /proc.
    // This bounded cold scan is never part of steady-state monitoring.
    unsigned count = 0; bool found_owner = allowed == 0;
    const double until = mono() + 2;
    for (const auto& entry : fs::directory_iterator(proc)) {
        const auto pid = entry.path().filename().string();
        if (pid.empty() || !std::all_of(pid.begin(), pid.end(), [](char c) { return c >= '0' && c <= '9'; })) continue;
        require(++count <= 8192 && mono() < until, "Controller membership scan exceeded bound");
        std::string text;
        try { text = read_text(entry.path() / "cgroup", 8192); }
        catch (const std::system_error& error) {
            if (error.code().value() == ENOENT || error.code().value() == ESRCH) continue;
            throw;
        }
        std::istringstream lines(text); std::string line; bool unified = false;
        while (std::getline(lines, line)) if (line.rfind("0::/", 0) == 0) {
            unified = true;
            const auto group = line.substr(3);
            if (group == path || group.rfind(path + '/', 0) == 0) {
                require(allowed > 0 && pid == std::to_string(allowed), "Controller cgroup contains a residual helper or another process");
                found_owner = true;
            }
        }
        require(unified, "Process cgroup membership is unknown");
    }
    require(found_owner, "Current controller membership was not observed");
}
void check_cgroup(const std::string& path, pid_t allowed) {
    const fs::path relative = fs::path(path).relative_path();
    require(path.size() > 1 && path[0] == '/' && path.find('\0') == std::string::npos &&
        std::find(relative.begin(), relative.end(), "..") == relative.end(), "Invalid controller cgroup");
    const auto root = fs::path("/sys/fs/cgroup") / relative;
    if (!fs::exists(root / "cgroup.procs")) { check_proc_cgroup(path, allowed); return; }
    unsigned count = 0;
    auto check = [&](const fs::path& directory) {
        require(++count <= 64, "Controller cgroup tree exceeds bound");
        for (const auto& pid : split_words(read_text(directory / "cgroup.procs")))
            require(allowed > 0 && pid == std::to_string(allowed), "Controller cgroup contains a residual helper or another process");
    };
    check(root);
    for (const auto& entry : fs::recursive_directory_iterator(root)) if (entry.is_directory()) check(entry.path());
}
void validate_idle_table(const Config& config, const Json& instance, const Json& snapshot, bool queue) {
    require(config.profile == "host-data", "Safe data lifecycle refuses root and lab profiles");
    require(snapshot.at("uuid") == config.uuid && snapshot.at("inactive").empty(), "Unexpected data map identity or inactive table");
    const auto& info = snapshot.at("info");
    for (const auto* key : {"suspended", "internal_suspend", "deferred_remove", "read_only", "open_count"})
        require(info.at(key) == 0, "Data map busy, suspended, read-only, or deferred");
    require(info.at("exists") == 1 && info.at("live_table") == 1 && info.at("target_count") == 1,
            "Data map is not a live single target");
    require(table_digest(snapshot.at("active")) == table_digest(table_targets(table(
        config.value.at("partition_sectors"), instance.at("dev")))), "Data map table or geometry changed");
    require(queue_enabled(snapshot) == queue, "Unexpected data map queue policy");
}
Json idle_data_snapshot(const Config& config, const Json& instance, DeviceMapper& mapper, bool queue) {
    require(path_instance(instance.at("node")) == instance, "Stopped/current device instance changed; identity must not be relearned");
    const auto before = checked_snapshot(mapper, config);
    validate_idle_table(config, instance, before, queue);
    const auto path = fs::path(instance.at("sys_path").get<std::string>());
    const auto mapdev = std::to_string(before.at("info").at("major").get<unsigned>()) + ':' +
        std::to_string(before.at("info").at("minor").get<unsigned>());
    const auto map = fs::canonical(fs::path("/sys/dev/block") / mapdev);
    require(entries(map / "slaves") == std::set<fs::path>{path} &&
        entries(path / "holders") == std::set<fs::path>{map} && fs::is_empty(map / "holders"), "Data map has unexpected holders or backing");
    std::istringstream mounts(read_text("/proc/1/mountinfo", 4 * 1024 * 1024)); std::string line;
    while (std::getline(mounts, line)) {
        auto fields = split_words(line); require(fields.size() >= 6, "Malformed host mountinfo");
        require(fields[2] != mapdev && fields[2] != instance.at("dev"), "Data map or raw partition remains mounted");
    }
    // open_count also detects mounts/opens outside PID 1's mount namespace.
    zero_inflight(map); zero_inflight(path); zero_inflight(path.parent_path());
    const auto [uuid, targets] = mapper.query(config.name);
    require(uuid == config.uuid && targets.size() == 1 && targets[0][0] == "multipath", "Data path status changed");
    const auto status = targets[0][1].get<std::string>();
    require(std::regex_search(status, std::regex("\\b" + instance.at("dev").get<std::string>() + R"( A \d+\b)")) &&
        !std::regex_search(status, std::regex(R"(\b\d+:\d+ F \d+\b)")), "Data path is missing, failed or recovering");
    const auto after = checked_snapshot(mapper, config);
    validate_idle_table(config, instance, after, queue);
    require(path_instance(instance.at("node")) == instance && before.at("active_digest") == after.at("active_digest"),
            "Data topology changed during idle checks");
    return after;
}
Json stopped_receipt(const Json& journal, const Json& identity) {
    return {{"schema", 1}, {"purpose", "safe_host_data_stop"}, {"boot_id", journal.at("boot_id")},
        {"map_name", journal.at("map_name")}, {"map_uuid", journal.at("map_uuid")},
        {"owner_epoch", journal.at("owner_epoch")}, {"config_digest", journal.at("config_digest")},
        {"identity_digest", digest(identity)}, {"journal_digest", digest(journal)}};
}
void validate_stopped(const Config& config, const Json& identity, const std::string& boot,
                      const Json& journal, const Json& receipt) {
    require(config.profile == "host-data" && journal.value("schema", 0) == 1 && journal.value("phase", "") == "safe_stopped" &&
        journal.value("queue_disable", "") == "verified_safe_stop" && journal.value("boot_id", "") == boot &&
        journal.value("config_digest", "") == digest(config.value) && journal.value("map_name", "") == config.name &&
        journal.value("map_uuid", "") == config.uuid, "No matching explicit safe-stop authorization");
    require(receipt == stopped_receipt(journal, identity), "Safe-stop receipt changed or mismatched");
    require(journal.at("stopped_instance").is_object() && journal.at("stopped_instance").size() == 4,
            "Invalid stopped device instance");
    validate_idle_table(config, journal.at("stopped_instance"), journal.at("snapshot"), false);
}
void finish_safe_stop(Journal& journal, const Json& details, const Json& identity,
                      const SaveState& save, const SetQueue& queue, const ReadIdle& check) {
    // All refusal checks precede intent. A later failure is incomplete, and the
    // existing supervisor still has a known table and the same owner fence.
    const auto previous = journal.record;
    Json before;
    try {
        before = check(true);
        journal.write("safe_stop_intent", details);
        check(true);
    } catch (const std::exception& error) {
        // No queue mutation has started. A consumer racing the preflight must
        // leave the owner ready, rather than trigger an unsolicited takeover.
        atomic_json(journal.path, previous);
        journal.record = previous;
        throw StopRefused(error.what());
    }
    queue(false);
    const auto final = check(false);
    require(before.at("active_digest") == final.at("active_digest"), "Map changed during safe stop");
    journal.write("safe_stopped", {{"snapshot", final}, {"queue_disable", "verified_safe_stop"}});
    save("safe-stop.json", stopped_receipt(journal.record, identity));
}
void begin_rearm(Journal& journal, const Json& old_journal, const Json& receipt,
                 const Json& old_invocation, const Json& new_invocation,
                 const SaveState& save, const SetQueue& queue, const ReadIdle& check) {
    const auto snapshot = check(false);
    // Copy, never unlink the current transaction. Archive failure is retryable
    // with the original explicit stop authorization and queue still disabled.
    save("safe-stop-archive.json", {{"schema", 1}, {"journal", old_journal},
        {"receipt", receipt}, {"invocation", old_invocation}});
    journal.write("rearm_intent", {{"config_digest", old_journal.at("config_digest")},
        {"snapshot", snapshot}, {"safe_stop_digest", digest(receipt)}});
    save("manager-invocation.json", new_invocation);
    check(false);
    // Both the new journal and its systemd invocation are durable before ON.
    queue(true);
    check(true);
}
DataControl::DataControl(const Config& config) {
    require(config.profile == "host-data", "Data control refuses other profiles");
    const auto directory = trusted_directory(config.run);
    const auto location = config.run / "data-control.sock";
    struct stat info{};
    if (!::lstat(location.c_str(), &info)) {
        require(S_ISSOCK(info.st_mode) && info.st_uid == 0 && (info.st_mode & 0077) == 0, "Untrusted old data control socket");
        require(!::unlink(location.c_str()), "Cannot retire old control socket");
    } else require(errno == ENOENT, "Cannot inspect old control socket");
    socket_.reset(::socket(AF_UNIX, SOCK_SEQPACKET | SOCK_NONBLOCK | SOCK_CLOEXEC, 0));
    require(bool(socket_), "Cannot create data control socket");
    const auto addr = address(directory.get());
    const auto mask = ::umask(0077);
    const auto result = ::bind(socket_.get(), reinterpret_cast<const sockaddr*>(&addr), sizeof(addr));
    ::umask(mask);
    require(!result && !::listen(socket_.get(), 4), "Cannot bind data control socket");
}
void DataControl::serve(const std::function<Json(const Json&)>& handler) {
    Fd client(::accept4(socket_.get(), nullptr, nullptr, SOCK_NONBLOCK | SOCK_CLOEXEC));
    if (!client) return;
    Json response;
    try { peer(client.get()); response = handler(receive(client.get(), 200)); }
    catch (const std::exception& error) { response = {{"state", "blocked"}, {"reason", error.what()}}; }
    try { send(client.get(), response); } catch (const std::exception&) { /* stop outcome is kept in the journal */ }
}
Json stop_data(const Json& record) {
    validate_record(record); Config requested(record.at("guard"));
    require(requested.profile == "host-data", "Safe stop is restricted to existing host-data owners");
    requested.validate_environment(); trusted_directory(requested.run);
    Config config(load_trusted_json(requested.identity.parent_path() / "config.json"));
    const auto identity = load_trusted_json(config.identity);
    require(record_from_profile({{"schema", 1}, {"guard", config.value}, {"identity", identity}}) == record,
        "Data runtime differs from the selected registration");
    try { Owner fence(config.run); return stopped_now(config, identity, fence); }
    catch (const std::system_error& error) {
        if (error.code().value() != EWOULDBLOCK && error.code().value() != EAGAIN) throw;
    }
    const auto journal = load_trusted_json(config.run / "path-transaction.json");
    Fd client(::socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC | SOCK_NONBLOCK, 0));
    const auto directory = trusted_directory(config.run);
    const auto addr = address(directory.get());
    if (!client || ::connect(client.get(), reinterpret_cast<const sockaddr*>(&addr), sizeof(addr)))
        return {{"state", "incomplete"}, {"reason", "Owner control unavailable; resources retained"}};
    require(peer(client.get()).pid == journal.at("owner_pid").get<pid_t>(), "Control socket is not the journal owner");
    send(client.get(), {{"action", "safe-stop"}, {"boot_id", journal.at("boot_id")},
        {"owner_epoch", journal.at("owner_epoch")}, {"config_digest", digest(config.value)}, {"record_digest", digest(record)}});
    Json result;
    try { result = receive(client.get(), 15000); }
    catch (const std::exception& error) { return {{"state", "incomplete"}, {"reason", error.what()}, {"resources_retained", true}}; }
    if (result.value("state", "") != "stopping") return result;
    const double until = mono() + 15;
    while (mono() < until) {
        try { Owner fence(config.run); return stopped_now(config, identity, fence); }
        catch (const std::exception&) { /* fence/cgroup may still be held by ExecStopPost or D-state work */ }
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    return {{"state", "incomplete"}, {"reason", "Owner or helper exit not confirmed; resources retained"}};
}
} // namespace rescue
