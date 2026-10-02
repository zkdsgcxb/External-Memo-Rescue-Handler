#include "controller.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <regex>
#include <set>
#include <sstream>
#include <stdexcept>
#include <system_error>
#include <thread>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <sys/un.h>
#include <sys/utsname.h>
#include <unistd.h>

namespace rescue {
namespace {
using CandidatePtr = std::shared_ptr<Candidate>;
const std::set<std::string> terminal{"expired", "failed", "interrupted", "blocked"};

bool has_word(const std::string& text, const std::string& word) {
    const auto words = split_words(text);
    return std::find(words.begin(), words.end(), word) != words.end();
}
bool failed_path(const std::string& status) {
    static const std::regex failed(R"(\b\d+:\d+ F \d+\b)");
    return std::regex_search(status, failed);
}
std::string dev_number(dev_t dev) {
    return std::to_string(major(dev)) + ":" + std::to_string(minor(dev));
}
dev_t node_device(const std::string& node) {
    struct stat st{};
    if (::stat(node.c_str(), &st)) throw std::system_error(errno, std::generic_category(), "stat " + node);
    return st.st_rdev;
}
bool unsafe_path(const fs::path& path) {
    if (!path.is_absolute() || path.native().find('\0') != std::string::npos) return true;
    return std::find(path.begin(), path.end(), "..") != path.end();
}
void notify_ready() {
    const char* name = std::getenv("NOTIFY_SOCKET");
    if (!name) return;
    sockaddr_un address{};
    address.sun_family = AF_UNIX;
    const std::size_t length = std::strlen(name);
    if (!length || length >= sizeof(address.sun_path)) throw std::runtime_error("Invalid notification socket");
    std::memcpy(address.sun_path, name, length);
    if (address.sun_path[0] == '@') address.sun_path[0] = '\0';
    Fd socket(::socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0));
    if (!socket || ::sendto(socket.get(), "READY=1", 7, 0,
            reinterpret_cast<const sockaddr*>(&address),
            static_cast<socklen_t>(offsetof(sockaddr_un, sun_path) + length + (name[0] != '@'))) != 7)
        throw std::system_error(errno, std::generic_category(), "systemd READY notification");
}

class Guard : public std::enable_shared_from_this<Guard> {
    Config config_;
    std::string epoch_;
    Journal journal_;
    Evidence evidence_;
    Observations observations_;
    std::string current_, current_sys_;
    dev_t current_dev_;
    std::uint64_t current_diskseq_;
    unsigned recoveries_ = 0;
    std::string last_rejection_, identity_state_ = "enrolled";
    std::array<unsigned, 3> version_;
    std::shared_ptr<Admission> admission_;
    std::string pending_kind_, candidate_table_;
    std::uint64_t pending_token_ = 0, generation_ = 0;
    std::atomic<bool> stopping_{false};
    Json kernel_probe_;
    CandidatePtr candidate_;

    void stage(const std::string& phase, const Json& details = Json::object(),
               const std::string& hook = {}) {
        journal_.write(phase, details);
        if (!hook.empty()) fault_hook(hook, journal_, config_.lab());
    }
    void submit(const std::string& kind, std::function<std::any()> fn) {
        if (mono() >= *deadline) { expire(); return; }
        if (!pending_kind_.empty()) throw std::runtime_error("Only one recovery operation may run");
        pending_token_ = operation.start(kind, std::move(fn));
        pending_kind_ = kind;
    }
    void mutation_budget(double until) const {
        if (stopping_.load() || mono() >= until)
            throw std::runtime_error("Admission expired before kernel mutation");
    }
    void reject(const std::string& reason) {
        candidate_.reset();
        if (reason != last_rejection_) {
            event("rejected", {{"reason", reason}});
            last_rejection_ = reason;
        }
    }
    void consume(Outcome result) {
        const auto kind = pending_kind_;
        if (result.kind != kind || result.token != pending_token_)
            throw std::runtime_error("Operation belongs to a different transaction");
        pending_kind_.clear();
        if (kind == "verify" && result.error.is_null())
            candidate_ = std::any_cast<CandidatePtr>(result.value);
        if (mono() >= *deadline) { expire(); return; }
        if (!result.error.is_null()) {
            const auto reason = result.error.at("message").get<std::string>();
            if (kind == "verify") { reject(reason); return; }
            throw std::runtime_error(kind + ": " + reason);
        }
        // Workers retain both the controller and the credential. Abandoning a
        // late operation cannot destroy either while a syscall still uses it.
        const auto self = shared_from_this();
        const auto candidate = candidate_;
        const auto epoch = epoch_;
        const double until = *deadline;
        if (kind == "verify") {
            if (candidate->dev() == current_dev_ && candidate->diskseq() != current_diskseq_) {
                reject("New disk instance reuses active dev_t; binding validation required");
                return;
            }
            identity_state_ = "verified_candidate";
            const auto snapshot = checked_snapshot(mapper, config_);
            if (!snapshot.at("inactive").empty() || snapshot.at("info").at("suspended").get<int>() != 0)
                throw std::runtime_error("Unexpected pre-existing transaction state");
            candidate_table_ = table(candidate->partition_sectors(), dev_number(candidate->dev()));
            stage("load_intent", {{"previous", snapshot}, {"candidate", candidate->to_json()},
                {"candidate_table_digest", table_digest(table_targets(candidate_table_))},
                {"commit_started", false}}, "before_load");
            event("verified", {{"node", candidate->node()}});
            const auto new_table = candidate_table_;
            submit("load", [self, candidate, epoch, until, new_table]() -> std::any {
                candidate->revalidate(epoch, false);
                self->mutation_budget(until);
                dm({"load", self->config_.name, "--table", new_table}, self->operation.fence_fd());
                return checked_snapshot(self->mapper, self->config_);
            });
        } else if (kind == "load") {
            stage("loaded", {{"snapshot", std::any_cast<Json>(result.value)}}, "after_load");
            fault_hook("before_revalidate", journal_, config_.lab());
            submit("revalidate", [self, candidate, epoch]() -> std::any { candidate->revalidate(epoch); return Json(); });
        } else if (kind == "revalidate") {
            stage("commit_intent", {{"commit_started", true}}, "before_commit");
            submit("commit", [self, candidate, epoch, until]() -> std::any {
                candidate->revalidate(epoch, false);
                self->mutation_budget(until);
                dm({"resume", "--noflush", "--nolockfs", self->config_.name}, self->operation.fence_fd());
                return checked_snapshot(self->mapper, self->config_);
            });
        } else if (kind == "commit") {
            current_ = candidate->node(); current_sys_ = candidate->sys_path();
            current_dev_ = candidate->dev(); current_diskseq_ = candidate->diskseq();
            stage("committed", {{"snapshot", std::any_cast<Json>(result.value)}}, "after_commit");
            submit("preprobe", [self, candidate, epoch]() -> std::any { candidate->revalidate(epoch, false); return Json(); });
        } else if (kind == "preprobe") {
            stage("probe_intent", Json::object(), "before_probe");
            const auto generation = ++generation_;
            const auto device = config_.device;
            submit("probe", [device, generation]() -> std::any { return probe_paths(device, generation); });
            if (!terminal.count(state)) {
                stage("probing", {{"generation", generation}}, "probe_started");
                event("probing", {{"node", current_}, {"generation", generation}});
            }
        } else if (kind == "probe") {
            const auto value = std::any_cast<Json>(result.value);
            if (value.at("token") != generation_) throw std::runtime_error("Path probe belongs to a different table generation");
            const auto status = check_map();
            if (value.at("status") != "completed" || !current_present() || !current_active(status) || failed_path(status)) {
                candidate_.reset();
                event("rejected", {{"reason", "Post-swap path confirmation failed"}, {"kernel_probe", value}});
                return;
            }
            kernel_probe_ = value;
            stage("confirming", {{"kernel_probe", value}}, "before_ready");
            const auto expected = journal_.record.at("candidate_table_digest");
            submit("confirm", [self, candidate, epoch, expected]() -> std::any {
                candidate->revalidate(epoch, false);
                auto snapshot = checked_snapshot(self->mapper, self->config_);
                if (snapshot.at("active_digest") != expected || !snapshot.at("inactive").empty() ||
                        snapshot.at("info").at("suspended").get<int>() != 0)
                    throw std::runtime_error("Committed table no longer matches candidate");
                return snapshot;
            });
        } else if (kind == "confirm") {
            deadline.reset(); ++recoveries_; last_rejection_.clear(); identity_state_ = "verified";
            stage("ready", {{"snapshot", std::any_cast<Json>(result.value)}, {"deadline", nullptr}, {"recoveries", recoveries_}});
            candidate_.reset();
            event("ready", {{"node", current_}, {"kernel_probe", kernel_probe_},
                {"confirmation", "kernel-probe-and-state"}, {"outcome", "path_restored"}});
        } else throw std::runtime_error("Unknown operation result");
    }
public:
    DeviceMapper mapper;
    OwnedOperation operation;
    std::optional<double> deadline;
    std::string state = "ready";

    Guard(Config config, const std::shared_ptr<Recovery>& recovery, Owner& owner)
        : config_(std::move(config)), epoch_(owner.epoch), journal_(owner, config_.name, config_.uuid),
          evidence_(owner.run), current_(config_.value.at("initial_node")),
          current_sys_(config_.value.at("initial_sys_path")), current_dev_(node_device(current_)),
          current_diskseq_(config_.value.at("initial_diskseq")), operation(owner.fd.get()) {
        version_ = mapper.target_version("multipath");
        if (version_ < std::array<unsigned, 3>{1, 15, 0})
            throw std::runtime_error("DM_MPATH_PROBE_PATHS requires multipath target >= 1.15.0");
        recovery->run = [this](const auto& args, double timeout) { return readonly(args, timeout, operation.fence_fd()); };
        admission_ = std::make_shared<Admission>(config_.value, recovery);
        journal_.write("idle", {{"config_digest", digest(config_.value)}, {"snapshot", checked_snapshot(mapper, config_)}, {"recoveries", 0}});
    }
    const std::string& current() const { return current_; }
    const std::string& current_sys() const { return current_sys_; }
    void event(const std::string& next, const Json& details = Json::object()) {
        Json entry{{"time", mono()}, {"state", next}, {"recoveries", recoveries_},
            {"multipath_target_version", version_}, {"owner_epoch", epoch_},
            {"observations", {{"identity", identity_state_}, {"transport", observations_.transport},
                {"control", journal_.record.at("phase")}, {"upper_errors", observations_.errors}}}};
        entry.update(details); state = next; evidence_.event(entry);
    }
    std::string check_map() {
        auto [uuid, targets] = mapper.query(config_.name);
        if (uuid != config_.uuid || targets.size() != 1 || targets[0][0] != "multipath")
            throw std::runtime_error("Unexpected stable map identity or target");
        const auto status = targets[0][1].get<std::string>();
        observations_.sample(mapper.last_info, failed_path(status));
        return status;
    }
    bool current_present() const {
        try {
            const auto path = fs::canonical(fs::path("/sys/class/block") / fs::path(current_).filename());
            return path == current_sys_ && std::stoull(trim(read_text(path.parent_path() / "diskseq"))) == current_diskseq_
                && node_device(current_) == current_dev_;
        } catch (const std::exception&) { return false; }
    }
    bool current_active(const std::string& status) const {
        return std::regex_search(status, std::regex("\\b" + dev_number(current_dev_) + R"( A \d+\b)"));
    }
    void shutdown() {
        if (stopping_.exchange(true)) return;
        auto candidate = std::move(candidate_);
        operation.close([candidate = std::move(candidate)](const Outcome&) { /* retained through worker completion */ });
    }
    void expire() {
        Json pending = pending_kind_.empty() ? Json() : Json(pending_kind_);
        const Json details{{"operation_pending", pending}, {"probe_pending", pending_kind_ == "probe"},
            {"queue_disable", "deferred_to_takeover"}};
        auto journal_details = details;
        journal_details["phase_at_expiry"] = journal_.record.at("phase");
        journal_.write("expired", journal_details);
        auto event_details = details;
        event_details.update({{"outcome", "admission_stopped"}, {"reason", "admission deadline exceeded; in-flight I/O is not cancelled"}});
        event("expired", event_details); shutdown();
    }
    void step() {
        if (terminal.count(state)) return;
        const double now = mono();
        if (deadline && now >= *deadline) { expire(); return; }
        try {
            if (!pending_kind_.empty()) {
                if (auto result = operation.poll()) consume(std::move(*result));
                return;
            }
            if (!deadline) {
                const bool failed = failed_path(check_map());
                if (current_present() && !failed) return;
                deadline = now + config_.value.at("queue_seconds").get<double>();
                identity_state_ = "missing_or_failed";
                stage("waiting", {{"deadline", *deadline}, {"candidate", nullptr}, {"commit_started", false}});
                event("waiting", {{"old_node", current_}, {"deadline", *deadline}});
            }
            candidate_.reset(); stage("verifying", Json::object(), "before_verify");
            const auto self = shared_from_this();
            const auto until = *deadline;
            submit("verify", [self, until]() -> std::any { return self->admission_->verify(until, self->epoch_); });
        } catch (const std::exception& error) {
            event("failed", {{"reason", error.what()}, {"outcome", "control_uncertain"}});
            shutdown(); throw;
        }
    }
};

void reconcile(const Config& config, Owner& owner) {
    auto record = load_json(owner.run / "path-transaction.json");
    if (record.value("schema", 0) != 1 || record.value("boot_id", "") != owner.boot_id ||
        record.value("map_name", "") != config.name || record.value("map_uuid", "") != config.uuid ||
        record.value("config_digest", "") != digest(config.value))
        throw std::runtime_error("Untrusted transaction journal; manual diagnosis required");
    DeviceMapper mapper;
    auto snapshot = checked_snapshot(mapper, config);
    std::set<Json> known{record.value("previous", Json::object()).value("active_digest", Json()),
        record.value("snapshot", Json::object()).value("active_digest", Json())};
    const auto phase = record.at("phase") == "expired" ? record.value("phase_at_expiry", record.at("phase")) : record.at("phase");
    const std::set<std::string> commit_phases{"commit_intent", "committed", "probe_intent", "probing", "confirming", "ready"};
    if (commit_phases.count(phase.get<std::string>())) known.insert(record.value("candidate_table_digest", Json()));
    if (!known.count(snapshot.at("active_digest"))) throw std::runtime_error("Unknown active table; refusing automatic resume");
    if (!snapshot.at("inactive").empty()) {
        if (snapshot.at("inactive_digest") != record.value("candidate_table_digest", Json()))
            throw std::runtime_error("Unknown inactive table; refusing automatic clear");
        dm({"clear", config.name}, owner.fd.get());
    }
    if (snapshot.at("info").at("suspended").get<int>() != 0) dm({"resume", "--noflush", "--nolockfs", config.name}, owner.fd.get());
    dm({"message", config.name, "0", "fail_if_no_path"}, owner.fd.get());
    const auto final = checked_snapshot(mapper, config);
    if (!final.at("inactive").empty() || final.at("info").at("suspended").get<int>() != 0)
        throw std::runtime_error("Map still has an unfinished control transaction");
    const auto previous_epoch = record.at("owner_epoch");
    const auto end = record.at("phase") == "expired" ? "expired" : "interrupted";
    record.update({{"owner_epoch", owner.epoch}, {"previous_owner_epoch", previous_epoch}, {"phase", end},
        {"updated_at", mono()}, {"snapshot", final}, {"queue_disable", "completed_by_takeover"}});
    atomic_json(owner.run / "path-transaction.json", record);
    Json result{{"state", end}, {"outcome", "admission_stopped"}, {"owner_epoch", owner.epoch},
        {"recoveries", record.value("recoveries", 0)}, {"previous_owner_epoch", previous_epoch}, {"snapshot", final},
        {"reason", "owner ended; no new candidate admitted; in-flight I/O is not cancelled"}};
    atomic_json(owner.run / "path-supervisor.json", result);
    if (std::string(end) != "expired") { result["time"] = mono(); Evidence(owner.run).event(result); }
}
} // namespace

Config::Config(Json input) : value(std::move(input)), profile(value.value("profile", "lab")),
    name(value.value("map_name", "lab-path")), uuid(value.value("map_uuid", "mpath-RAMRESCUE-LAB")),
    device("/dev/mapper/" + name), run(value.value("run_dir", "/run")), identity(value.value("identity_path", "/etc/rescue/identity.json")) {
    if (profile != "lab" && profile != "host" && profile != "host-data") throw std::invalid_argument("Unsupported guard profile");
    if (!lab()) for (const auto* key : {"map_name", "map_uuid", "run_dir", "kernel_release"})
        if (!value.contains(key) || !value.at(key).is_string() || value.at(key).get<std::string>().empty())
            throw std::invalid_argument(std::string("Host profile requires ") + key);
    if (!std::regex_match(name, std::regex("[A-Za-z0-9+_.-]{1,127}")) || name == "." || name == "..")
        throw std::invalid_argument("Invalid multipath map name");
    if (uuid.empty() || uuid.size() > 127 || std::any_of(uuid.begin(), uuid.end(), [](unsigned char c) { return !c || std::isspace(c); }))
        throw std::invalid_argument("Invalid multipath map UUID");
    if (unsafe_path(run) || run == "/") throw std::invalid_argument("run_dir must be an absolute dedicated state directory");
    if (unsafe_path(identity)) throw std::invalid_argument("identity_path must be absolute");
    if (profile == "host-data") validate_data_config(value);
}
void Config::validate_environment() const {
    const auto cmdline = read_text("/proc/cmdline");
    if (lab()) {
        if (!has_word(cmdline, "ram_rescue_lab=1") || trim(read_text("/sys/class/dmi/id/product_name")) != "RAMRescueLab")
            throw std::runtime_error("Refusing: not booted as disposable RAM rescue lab");
    } else {
        if (profile == "host" && !has_word(cmdline, "ram_rescue_guard=1"))
            throw std::runtime_error("Host guard requires explicit ram_rescue_guard=1 boot flag");
        utsname info{};
        if (::uname(&info) || value.at("kernel_release") != info.release)
            throw std::runtime_error("Host guard kernel differs from enrolled kernel_release");
    }
}
std::string table(std::uint64_t sectors, const std::string& node) {
    if (!sectors) throw std::invalid_argument("invalid partition size");
    return "0 " + std::to_string(sectors) + " multipath 3 queue_if_no_path queue_mode bio 0 1 1 round-robin 0 1 1 " + node + " 1";
}
Json table_targets(const std::string& text) {
    std::istringstream input(text); std::uint64_t start, size; std::string kind, params;
    if (!(input >> start >> size >> kind)) throw std::invalid_argument("Malformed DM table");
    std::getline(input, params);
    if (trim(params).empty()) throw std::invalid_argument("Malformed DM table parameters");
    return Json::array({Json::array({start, size, kind, trim(params)})});
}
std::string dm(const std::vector<std::string>& args, int fence) {
    std::vector<std::string> command_args{"/sbin/dmsetup", "--noudevsync"};
    command_args.insert(command_args.end(), args.begin(), args.end());
    return command(command_args, 5, fence);
}
Json checked_snapshot(DeviceMapper& mapper, const Config& config) {
    auto snapshot = describe(mapper.snapshot(config.name));
    if (snapshot.at("uuid") != config.uuid || snapshot.at("active").size() != 1 || snapshot.at("active")[0][2] != "multipath")
        throw std::runtime_error("Unexpected stable map identity or target");
    return snapshot;
}
std::unique_ptr<Owner> acquire_owner(const Config& config, bool taking_over) {
    bool announced = false;
    for (;;) {
        try { return std::make_unique<Owner>(config.run); }
        catch (const std::system_error& error) {
            if (!taking_over || (error.code().value() != EWOULDBLOCK && error.code().value() != EAGAIN)) throw;
            if (!announced) {
                atomic_json(config.run / "path-supervisor.json", {{"state", "waiting_for_owner"}, {"time", mono()},
                    {"pid", ::getpid()}, {"outcome", "old_operation_not_finished"}});
                announced = true;
            }
            std::this_thread::sleep_for(std::chrono::seconds(1));
        }
    }
}
void run_owned(const Config& config, Owner& owner, bool taking_over) {
    if (taking_over) { reconcile(config, owner); return; }
    if (fs::exists(owner.run / "path-transaction.json"))
        throw std::runtime_error("Existing transaction requires takeover, not owner restart");
    auto recovery = std::make_shared<Recovery>(load_json(config.identity), [&owner](const auto& args, double timeout) {
        return readonly(args, timeout, owner.fd.get()); });
    if (config.profile == "host-data") validate_data_runtime(config.value, recovery);
    auto manager = std::make_shared<Guard>(config, recovery, owner);
    try {
        const auto status = manager->check_map();
        if (!manager->current_present() || !manager->current_active(status))
            throw std::runtime_error("Initial enrolled path is absent or not active");
        std::ofstream(owner.run / "path-guard.pid") << ::getpid();
        manager->event("ready", {{"node", manager->current()}, {"outcome", "initial_mapping"}});
        try { notify_ready(); }
        catch (const std::exception& error) {
            manager->event("failed", {{"reason", error.what()}, {"outcome", "startup_notification_failed"}}); throw;
        }
        Events events;
        const auto info = manager->mapper.last_info;
        const auto map_sys = fs::canonical(fs::path("/sys/dev/block") /
            (std::to_string(info.at("major").get<unsigned>()) + ":" + std::to_string(info.at("minor").get<unsigned>()))).string();
        Schedule schedule(mono()); bool pending = false, completed = false;
        while (!terminal.count(manager->state)) {
            const double now = mono();
            if (completed || schedule.due(now, pending) || (manager->deadline && now >= *manager->deadline)) {
                manager->step();
                events.watch(manager->deadline ? std::vector<std::string>{} :
                    std::vector<std::string>{map_sys, fs::path(manager->current_sys()).parent_path().string()});
                schedule.completed(mono(), manager->deadline.has_value()); pending = completed = false;
            }
            if (terminal.count(manager->state)) break;
            double wake = schedule.next_check;
            if (pending) wake = std::min(wake, schedule.event_after);
            if (manager->deadline) wake = std::min(wake, *manager->deadline);
            pending = events.wait(wake - mono(), manager->operation.fileno(), pending && mono() < schedule.event_after) || pending;
            completed = events.operation_ready;
        }
    } catch (...) { manager->shutdown(); throw; }
    manager->shutdown();
}
void run(const Json& value, bool taking_over) {
    Config config(value); config.validate_environment();
    try {
        auto owner = acquire_owner(config, taking_over);
        run_owned(config, *owner, taking_over);
    } catch (const std::exception& error) {
        if (taking_over) {
            Json failure{{"state", "blocked"}, {"reason", error.what()}, {"outcome", "manual_diagnosis_required"}, {"time", mono()}};
            atomic_json(config.run / "path-supervisor.json", failure);
            const auto* system = dynamic_cast<const std::system_error*>(&error);
            if (!system || (system->code().value() != EWOULDBLOCK && system->code().value() != EAGAIN)) Evidence(config.run).event(failure);
        }
        throw;
    }
}
void maintain(const Json& record, bool taking_over) {
    validate_record(record);
    if (record.at("guard").at("profile") != "host-data")
        throw std::runtime_error("The enrolled root map is already maintained by its boot service");
    const char* invocation_env = std::getenv("INVOCATION_ID");
    const std::string invocation = invocation_env ? invocation_env : "";
    if (!std::regex_match(invocation, std::regex("[0-9a-f]{32}")))
        throw std::runtime_error("Maintenance must run as its systemd service invocation");
    Config config(record.at("guard")); config.validate_environment();
    const auto directory = config.identity.parent_path();
    const auto receipt_path = config.run / "manager-invocation.json";
    const auto journal_path = config.run / "path-transaction.json";
    if (!taking_over) {
        for (const auto& location : {directory.parent_path(), directory, config.run}) {
            if (fs::is_symlink(location)) throw std::runtime_error("Maintenance state directory must not be a symlink");
            fs::create_directories(location); fs::permissions(location, fs::perms::owner_all);
        }
        auto owner = acquire_owner(config, false);
        if (fs::exists(journal_path)) throw std::runtime_error("Existing transaction requires takeover, not owner restart");
        const auto profile = current_profile(record, [&owner](const auto& args, double timeout) {
            return readonly(args, timeout, owner->fd.get()); });
        if (record_from_profile(profile) != record) throw std::runtime_error("Fresh device observations changed the registered policy");
        config = Config(profile.at("guard"));
        for (const auto& location : {directory / "identity.json", directory / "config.json", receipt_path})
            if (fs::is_symlink(location)) throw std::runtime_error("Maintenance state file must not be a symlink");
        atomic_json(directory / "identity.json", profile.at("identity"));
        atomic_json(directory / "config.json", config.value);
        atomic_json(receipt_path, {{"schema", 1}, {"invocation_id", invocation}, {"owner_epoch", owner->epoch}, {"config_digest", digest(config.value)}});
        run_owned(config, *owner);
    } else {
        if (!fs::exists(receipt_path)) return;
        const auto receipt = load_json(receipt_path);
        if (receipt.value("invocation_id", "") != invocation || !fs::exists(journal_path)) return;
        if (receipt.value("schema", 0) != 1) throw std::runtime_error("Untrusted maintenance invocation receipt");
        if (load_json(journal_path).at("owner_epoch") != receipt.at("owner_epoch"))
            throw std::runtime_error("Transaction belongs to another maintenance owner");
        auto owner = acquire_owner(config, true);
        if (load_json(receipt_path) != receipt) throw std::runtime_error("Maintenance invocation changed while waiting for its fence");
        if (load_json(journal_path).at("owner_epoch") != receipt.at("owner_epoch"))
            throw std::runtime_error("Transaction owner changed while waiting for its fence");
        config = Config(load_json(directory / "config.json"));
        Json profile{{"schema", 1}, {"identity", load_json(directory / "identity.json")}, {"guard", config.value}};
        if (record_from_profile(profile) != record || digest(config.value) != receipt.at("config_digest"))
            throw std::runtime_error("Maintenance runtime differs from its registered invocation");
        run_owned(config, *owner, true);
    }
}
} // namespace rescue
