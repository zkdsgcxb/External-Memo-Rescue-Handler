#include "controller.hpp"

#include <chrono>
#include <fstream>
#include <stdexcept>
#include <thread>
#include <sys/stat.h>
#include <sys/sysmacros.h>

namespace rescue {
namespace {
std::string escape_dm(const std::string& value) {
    std::string result;
    for (char c : value) { result += c; if (c == '-') result += c; }
    return result;
}
fs::path mapping(const std::string& uuid) {
    std::vector<fs::path> found;
    for (const auto& entry : fs::directory_iterator("/sys/class/block"))
        if (fs::exists(entry.path() / "dm/uuid") && trim(read_text(entry.path() / "dm/uuid")) == uuid)
            found.push_back(entry.path());
    if (found.size() != 1) throw std::runtime_error("Enrolled LV is not uniquely active");
    return found.front();
}
} // namespace

Json activate(const Json& enrollment) {
    if (enrollment.value("schema", 0) != 1) throw std::runtime_error("Unsupported enrollment schema");
    const auto identity = enrollment.at("identity");
    Config config(enrollment.at("guard"));
    if (config.profile != "host") throw std::runtime_error("Existing-disk boot requires an explicit host profile");
    config.validate_environment();
    const auto queue = config.value.at("queue_seconds");
    if (!queue.is_number_integer() || queue < Json(2) || queue > Json(60))
        throw std::runtime_error("Invalid queue budget");
    const auto root_lv = config.value.at("root_lv").get<std::string>();
    if (!identity.at("lvs").contains(root_lv)) throw std::runtime_error("Root LV is not enrolled");
    const auto expected_root = "/dev/mapper/" + escape_dm(identity.at("vg_name")) + "-" + escape_dm(root_lv);
    std::vector<std::string> roots;
    for (const auto& word : split_words(read_text("/proc/cmdline")))
        if (word.rfind("root=", 0) == 0) roots.push_back(word.substr(5));
    if (roots != std::vector<std::string>{expected_root}) throw std::runtime_error("Boot root argument does not match the enrolled root LV");
    DeviceMapper mapper;
    if (mapper.target_version("multipath") < std::array<unsigned, 3>{1, 15, 0})
        throw std::runtime_error("Current kernel path probe interface is required");
    for (const auto& row : config.value.at("layout"))
        if (row.at("segtype") != "linear") throw std::runtime_error("Only the enrolled linear layout can boot here");
    trusted_directory(config.run, true);
    const auto record = config.run / "boot.json";
    auto recovery = std::make_shared<Recovery>(identity);
    const double deadline = mono() + 30;
    for (;;) {
        try { recovery->candidate_node(); break; }
        catch (const std::exception&) {
            if (mono() >= deadline) throw;
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
    }
    Owner owner(config.run);
    if (fs::exists(record) || fs::exists(config.run / "path-transaction.json"))
        throw std::runtime_error("This boot already has a protection transaction");
    const auto vg_prefix = "LVM-" + identity.at("vg_uuid").get<std::string>();
    for (const auto& entry : fs::directory_iterator("/sys/class/block")) {
        if (!fs::exists(entry.path() / "dm/name")) continue;
        if (trim(read_text(entry.path() / "dm/name")) == config.name ||
            trim(read_text(entry.path() / "dm/uuid")).rfind(vg_prefix, 0) == 0)
            throw std::runtime_error("An enrolled mapping was activated before protection");
    }
    recovery->run = [&owner](const auto& args, double timeout) { return readonly(args, timeout, owner.fd.get()); };
    const auto record_phase = [&](const std::string& phase, const Json& extra = Json::object()) {
        Json value{{"phase", phase}, {"boot_id", owner.boot_id}, {"time", mono()}, {"map_name", config.name}};
        value.update(extra); atomic_json(record, value);
    };
    record_phase("verifying");
    auto admission = std::make_shared<Admission>(config.value, recovery);
    auto candidate = admission->verify(deadline, owner.epoch);
    candidate->revalidate(owner.epoch);
    {
        std::ofstream parameter("/sys/module/dm_multipath/parameters/queue_if_no_path_timeout_secs");
        parameter << queue.get<int>() + 2 << '\n';
        parameter.close();
        if (!parameter) throw std::runtime_error("Cannot configure multipath queue timeout");
    }
    record_phase("create_intent", {{"candidate", candidate->to_json()}});
    const auto device = std::to_string(major(candidate->dev())) + ":" + std::to_string(minor(candidate->dev()));
    const auto new_table = table(candidate->partition_sectors(), device);
    dm({"create", config.name, "--uuid", config.uuid, "--table", new_table}, owner.fd.get());
    dm({"mknodes", config.name}, owner.fd.get());
    auto snapshot = checked_snapshot(mapper, config);
    if (snapshot.at("active_digest") != table_digest(table_targets(new_table)) || !snapshot.at("inactive").empty() ||
        snapshot.at("info").at("suspended").get<int>() != 0) throw std::runtime_error("Stable map is not ready for initial activation");
    config.value.update({{"initial_node", candidate->node()}, {"initial_sys_path", candidate->sys_path()}, {"initial_diskseq", candidate->diskseq()}});
    candidate->revalidate(owner.epoch);
    record_phase("activate_intent", {{"snapshot", snapshot}});
    // Boot needs real udev rule/cookie completion before pivot. The runtime
    // then takes a fresh owner; no direct LV reload occurs during recovery.
    std::vector<std::string> args{"/sbin/lvm", "lvchange", "-ay", "--devices", config.device,
        "--config", "activation { udev_rules=1 udev_sync=1 }"};
    for (const auto& [name, value] : identity.at("lvs").items()) {
        (void)value;
        args.push_back(identity.at("vg_name").get<std::string>() + "/" + name);
    }
    command(args, 15, owner.fd.get());
    struct stat st{};
    if (::stat(config.device.c_str(), &st)) throw std::runtime_error("Cannot stat stable block device");
    const auto stable_sys = fs::canonical(fs::path("/sys/dev/block") /
        (std::to_string(major(st.st_rdev)) + ":" + std::to_string(minor(st.st_rdev))));
    for (const auto& [name, value] : identity.at("lvs").items()) {
        (void)name;
        std::vector<fs::path> slaves;
        for (const auto& entry : fs::directory_iterator(mapping(value.at("dm_uuid")) / "slaves"))
            slaves.push_back(fs::canonical(entry.path()));
        if (slaves != std::vector<fs::path>{stable_sys})
            throw std::runtime_error("Enrolled LV does not depend solely on the stable map");
    }
    candidate->revalidate(owner.epoch, false);
    candidate.reset();
    atomic_json("/run/ram-rescue-guard/config.json", config.value);
    record_phase("prepared", {{"snapshot", checked_snapshot(mapper, config)}});
    return config.value;
}
} // namespace rescue
