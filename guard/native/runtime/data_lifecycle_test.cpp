#include "data_lifecycle.hpp"
#include <iostream>
#include <fstream>
#include <stdexcept>
#include <sys/file.h>
#include <unistd.h>

using namespace rescue;
namespace {
unsigned checks = 0;
struct InjectedCrash : std::runtime_error { InjectedCrash() : std::runtime_error("injected crash") {} };
void require(bool ok, const char* why) { ++checks; if (!ok) throw std::runtime_error(why); }
template<class F> void refuses(F fn) { bool no = false; try { fn(); } catch (const std::exception&) { no = true; } require(no, "Expected refusal"); }
Config config() { return Config({{"profile", "host-data"}, {"map_name", "rr-data-test"},
    {"map_uuid", "RAMRESCUE-DATA-test"}, {"run_dir", "/run/ram-rescue-data/rr-data-test/state"},
    {"identity_path", "/run/ram-rescue-data/rr-data-test/identity.json"}, {"kernel_release", "7.0.0-test"},
    {"partition_start", 2048}, {"partition_sectors", 4096}, {"queue_seconds", 8}}); }
Json instance() { return {{"node", "/dev/sdb1"}, {"sys_path", "/sys/devices/test/block/sdb/sdb1"}, {"dev", "8:17"}, {"diskseq", 41}}; }
Json snapshot(bool queue) {
    auto result = Json{{"uuid", "RAMRESCUE-DATA-test"}, {"inactive", Json::array()},
        {"active", table_targets(queue ? table(4096, "8:17") : "0 4096 multipath 2 queue_mode bio 0 1 1 round-robin 0 1 1 8:17 1")},
        {"info", {{"exists", 1}, {"live_table", 1}, {"target_count", 1}, {"suspended", 0}, {"internal_suspend", 0},
            {"deferred_remove", 0}, {"read_only", 0}, {"open_count", 0}}}};
    return describe(result);
}
}
int main() {
    try {
        std::string pattern = "/tmp/rescue-lifecycle-XXXXXX";
        const auto directory = ::mkdtemp(pattern.data());
        if (!directory) throw std::runtime_error("mkdtemp");
        const fs::path folder(directory);
        struct Cleanup { fs::path path; ~Cleanup() { fs::remove_all(path); } } cleanup{folder};
        Owner owner(folder);
        const auto proc = folder / "proc";
        fs::create_directories(proc / "10"); fs::create_directories(proc / "20");
        std::ofstream(proc / "10/cgroup") << "0::/system.slice/owner.service\n";
        std::ofstream(proc / "20/cgroup") << "0::/other.service\n";
        check_proc_cgroup("/system.slice/owner.service", 10, proc); ++checks;
        refuses([&] { check_proc_cgroup("/system.slice/owner.service", 0, proc); });
        std::ofstream(proc / "20/cgroup") << "0::/system.slice/owner.service/child\n";
        refuses([&] { check_proc_cgroup("/system.slice/owner.service", 10, proc); });
        std::ofstream(proc / "20/cgroup") << "bad-data\n";
        refuses([&] { check_proc_cgroup("/system.slice/owner.service", 10, proc); });
        fs::remove(proc / "20/cgroup"); // vanished process is not a member
        check_proc_cgroup("/system.slice/owner.service", 10, proc); ++checks;
        refuses([&] { check_proc_cgroup("/system.slice/owner.service", 99, proc); });
        const auto cfg = config(); const Json id{{"fs_uuid", "enrolled"}};
        Journal old(owner, cfg.name, cfg.uuid);
        old.write("safe_stopped", {{"config_digest", digest(cfg.value)}, {"snapshot", snapshot(false)},
            {"stopped_instance", instance()}, {"owner_cgroup", "/system.slice/test.service"}, {"queue_disable", "verified_safe_stop"}});
        const auto receipt = stopped_receipt(old.record, id);
        validate_stopped(cfg, id, owner.boot_id, old.record, receipt); ++checks;
        for (const auto* phase : {"interrupted", "expired", "failed", "blocked", "idle", "rearm_intent", "safe_stop_intent"}) {
            auto changed = old.record; changed["phase"] = phase;
            refuses([&] { validate_stopped(cfg, id, owner.boot_id, changed, stopped_receipt(changed, id)); });
        }
        refuses([&] { validate_stopped(cfg, id, "different-boot", old.record, receipt); });
        refuses([&] { validate_stopped(cfg, {{"fs_uuid", "other"}}, owner.boot_id, old.record, receipt); });
        for (const auto* field : {"boot_id", "owner_epoch", "config_digest", "map_name", "map_uuid", "queue_disable"}) {
            auto changed = old.record; changed[field] = "changed";
            refuses([&] { validate_stopped(cfg, id, owner.boot_id, changed, receipt); });
        }
        auto extra = receipt; extra["allow"] = true;
        refuses([&] { validate_stopped(cfg, id, owner.boot_id, old.record, extra); });
        for (const auto* field : {"diskseq", "node", "dev", "sys_path"}) {
            auto changed = old.record; changed["stopped_instance"][field] = "changed";
            refuses([&] { validate_stopped(cfg, id, owner.boot_id, changed, receipt); });
        }
        auto geometry = cfg.value; geometry["partition_sectors"] = 4097;
        refuses([&] { validate_stopped(Config(geometry), id, owner.boot_id, old.record, receipt); });
        require(queue_enabled(snapshot(true)) && !queue_enabled(snapshot(false)), "Queue bit must not use normalized digest");
        require(snapshot(true)["active_digest"] == snapshot(false)["active_digest"], "Fixture must expose mutable queue bit");
        for (const auto* field : {"open_count", "suspended", "internal_suspend", "deferred_remove", "read_only"}) {
            auto busy = snapshot(true); busy["info"][field] = 1;
            refuses([&] { validate_idle_table(cfg, instance(), busy, true); });
        }
        for (const auto* field : {"exists", "live_table", "target_count"}) {
            auto changed = snapshot(true); changed["info"][field] = 0;
            refuses([&] { validate_idle_table(cfg, instance(), changed, true); });
        }
        auto inactive = snapshot(true); inactive["inactive"] = inactive["active"];
        refuses([&] { validate_idle_table(cfg, instance(), inactive, true); });
        auto unknown = snapshot(true); unknown["active"] = table_targets(table(4096, "8:33"));
        refuses([&] { validate_idle_table(cfg, instance(), unknown, true); });
        refuses([&] { validate_idle_table(cfg, instance(), snapshot(false), true); });
        refuses([&] { validate_idle_table(cfg, instance(), snapshot(true), false); });
        auto malformed = snapshot(true); malformed["active"][0][3] = "999 queue_if_no_path";
        refuses([&] { queue_enabled(malformed); });
        auto root = cfg.value; root["profile"] = "host";
        refuses([&] { validate_idle_table(Config(root), instance(), snapshot(true), true); });
        // Real file replacement failures and every callback crash prefix. The
        // journal is never deleted; queue ON is forbidden until its new epoch
        // and invocation receipt are both durably published.
        const Json stopped = old.record;
        Json old_invocation{{"owner_epoch", owner.epoch}, {"invocation_id", "old"}};
        struct stat lock_before{}; ::fstat(owner.fd.get(), &lock_before);
        refuses([&] { Owner competing(folder); });
        for (int fail = 1; fail <= 7; ++fail) {
            atomic_json(old.path, stopped);
            atomic_json(folder / "manager-invocation.json", old_invocation);
            Journal next(owner, cfg.name, cfg.uuid); next.record["owner_epoch"] = "next";
            bool queued = false; int step = 0;
            auto crash = [&] { if (++step == fail) throw InjectedCrash(); };
            try {
                begin_rearm(next, stopped, receipt, old_invocation, {{"owner_epoch", "next"}, {"invocation_id", "new"}},
                    [&](const auto& name, const auto& value) { crash(); atomic_json(folder / name, value); },
                    [&](bool on) { crash();
                        require(load_json(old.path).at("owner_epoch") == "next" &&
                            load_json(folder / "manager-invocation.json").at("owner_epoch") == "next", "Orphan queue window");
                        queued = on; },
                    [&](bool on) { crash(); require(queued == on, "Queue observation differs"); return snapshot(on); });
            } catch (const InjectedCrash&) {}
            const auto saved = load_json(old.path);
            require(saved.at("phase") == "safe_stopped" || saved.at("phase") == "rearm_intent", "Journal hole after archive");
            if (queued) require(saved.at("owner_epoch") == "next" &&
                load_json(folder / "manager-invocation.json").at("owner_epoch") == "next", "Queue lost supervisor binding");
            require(!queued || saved.at("snapshot").at("active_digest") == snapshot(true).at("active_digest"), "Takeover does not know table");
        }
        for (int fail = 1; fail <= 6; ++fail) {
            Journal live(owner, cfg.name, cfg.uuid);
            live.write("idle", {{"config_digest", digest(cfg.value)}, {"snapshot", snapshot(true)}});
            fs::remove(folder / "safe-stop.json");
            bool queued = true; int step = 0;
            auto crash = [&] { if (++step == fail) throw InjectedCrash(); };
            try {
                finish_safe_stop(live, {{"stopped_instance", instance()}, {"owner_cgroup", "/test"}}, id,
                    [&](const auto& name, const auto& value) { crash(); atomic_json(folder / name, value); },
                    [&](bool on) { crash(); queued = on; },
                    [&](bool on) { crash(); require(queued == on, "Stop queue observation differs"); return snapshot(on); });
            } catch (const InjectedCrash&) {}
            catch (const StopRefused&) { require(fail <= 2, "Unexpected stop refusal masked a test failure"); }
            if (fs::exists(folder / "safe-stop.json")) {
                require(!queued, "Receipt before queue OFF");
                validate_stopped(cfg, id, owner.boot_id, load_json(old.path), load_json(folder / "safe-stop.json")); ++checks;
            }
            require(fs::exists(old.path), "Stop deleted journal");
            if (fail <= 2) require(queued && load_json(old.path).at("phase") == "idle", "Pre-mutation refusal ended the owner transaction");
        }
        // A malicious alias cannot make archival replace an unrelated file.
        fs::remove(folder / "safe-stop-archive.json");
        fs::create_symlink(folder / "manager-invocation.json", folder / "safe-stop-archive.json");
        bool queued = false;
        Journal next(owner, cfg.name, cfg.uuid);
        refuses([&] { begin_rearm(next, stopped, receipt, old_invocation, Json::object(),
            [&](const auto& name, const auto& value) { atomic_json(folder / name, value); },
            [&](bool on) { queued = on; }, [&](bool on) { return snapshot(on); }); });
        require(!queued, "Archive rejection enabled queue");
        struct stat lock_after{}; ::stat((folder / "path-owner.lock").c_str(), &lock_after);
        require(lock_before.st_ino == lock_after.st_ino && lock_before.st_dev == lock_after.st_dev, "Lifecycle replaced lock inode");
        refuses([&] { Owner competing(folder); });
    } catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
    std::cout << checks << " data lifecycle safety checks passed\n";
}
