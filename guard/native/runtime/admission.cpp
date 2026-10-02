#include "admission.hpp"

#include <algorithm>
#include <cerrno>
#include <cmath>
#include <fcntl.h>
#include <linux/fs.h>
#include <regex>
#include <set>
#include <sstream>
#include <stdexcept>
#include <system_error>
#include <sys/ioctl.h>
#include <sys/sysmacros.h>
#include <sys/utsname.h>
#include <unistd.h>

namespace rescue {
namespace {
[[noreturn]] void refuse(const std::string& reason) { throw std::runtime_error(reason); }
[[noreturn]] void system_failure(const std::string& operation) {
    throw std::system_error(errno, std::generic_category(), operation);
}

bool integer(const Json& value) {
    return value.is_number_integer(); // JSON booleans and floats are not integers.
}
uint64_t nonnegative(const Json& value, const std::string& label) {
    if (!integer(value) || (!value.is_number_unsigned() && value.get<int64_t>() < 0))
        refuse(label + " must be a nonnegative integer");
    return value.get<uint64_t>();
}
uint64_t positive(const Json& value, const std::string& label) {
    auto result = nonnegative(value, label);
    if (!result) refuse(label + " must be a positive integer");
    return result;
}
uint64_t number_file(const fs::path& path) {
    auto value = trim(read_text(path));
    if (value.empty() || !std::all_of(value.begin(), value.end(), [](unsigned char c) {
            return c >= '0' && c <= '9';
        })) refuse("Invalid nonnegative integer in " + path.string());
    return std::stoull(value);
}
std::string string_field(const Json& value, const std::string& key) {
    if (!value.contains(key) || !value.at(key).is_string()) refuse("Expected string " + key);
    return value.at(key).get<std::string>();
}
std::string nonempty_field(const Json& value, const std::string& key) {
    auto result = string_field(value, key);
    if (trim(result).empty()) refuse("Enrollment needs nonempty " + key);
    return result;
}
bool matches(const std::string& value, const char* pattern) {
    return std::regex_match(value, std::regex(pattern));
}
std::string device_number(dev_t device) {
    return std::to_string(major(device)) + ":" + std::to_string(minor(device));
}
Json properties(const std::string& output) {
    Json result = Json::object();
    std::istringstream input(output);
    for (std::string line; std::getline(input, line);) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        auto separator = line.find('=');
        if (separator != std::string::npos)
            result[line.substr(0, separator)] = line.substr(separator + 1);
    }
    return result;
}
Json rows(const std::string& output, const std::string& key) {
    auto document = Json::parse(output);
    Json result = Json::array();
    for (const auto& report : document.at("report")) {
        if (report.contains(key)) {
            if (!report.at(key).is_array()) refuse("Invalid LVM report rows");
            for (const auto& row : report.at(key)) result.push_back(row);
        }
    }
    return result;
}
void require_properties(const Json& actual, const Json& expected) {
    for (const auto& [key, value] : expected.items())
        if (!actual.contains(key) || actual.at(key) != value)
            refuse(key + " does not match the enrolled disk. No changes made.");
}
void validate_filesystem_identity(const Json& identity) {
    if (identity.value("kind", "") != "filesystem")
        refuse("Filesystem identity must explicitly select kind=filesystem");
    static const std::set<std::string> types{"ext4", "exfat", "vfat"};
    if (!types.count(string_field(identity, "fs_type")))
        refuse("Unsupported filesystem type; expected ext4, exfat, or vfat");
    for (const auto* key : {"usb_serial", "fs_uuid", "partuuid"}) nonempty_field(identity, key);
    for (const auto* key : {"vid", "pid"})
        if (!matches(string_field(identity, key), "[0-9a-f]{4}"))
            refuse(std::string("Filesystem enrollment needs a lowercase USB ") + key);
    for (const auto* key : {"sectors", "partition_number"}) positive(identity.at(key), key);
}
Json filesystem_layout(const Json& identity) {
    Json result;
    for (const auto* key : {"kind", "fs_type", "fs_uuid", "partuuid"}) result[key] = identity.at(key);
    return result;
}
} // namespace

std::string readonly(const std::vector<std::string>& args, double timeout, int owner_fd) {
    if (args.empty()) refuse("Empty command");
    auto selected = args;
    if (args.front() == "/sbin/lvm") {
        auto devices = std::find(args.begin(), args.end(), "--devices");
        if (std::find(args.begin(), args.end(), "--readonly") == args.end() ||
                devices == args.end() || std::next(devices) == args.end() || std::next(devices)->empty())
            refuse("Guard may only read explicitly selected LVM devices");
        selected.insert(selected.end(), {"--config", "devices { multipath_component_detection=0 }"});
    }
    return command(selected, std::min(timeout, 3.0), owner_fd);
}

Recovery::Recovery(Json value, Runner runner, fs::path sysroot, fs::path devroot)
    : identity(std::move(value)), sys(std::move(sysroot)), dev(std::move(devroot)),
      run(runner ? std::move(runner) : Runner([](const auto& args, double timeout) {
          return readonly(args, timeout);
      })) {
    const auto kind = identity.value("kind", "lvm");
    if (kind == "filesystem") validate_filesystem_identity(identity);
    else if (kind != "lvm") refuse("Unsupported enrolled device kind: " + kind);
}
bool Recovery::filesystem() const { return identity.value("kind", "lvm") == "filesystem"; }

std::vector<fs::path> Recovery::candidates() const {
    std::vector<fs::path> result;
    for (const auto& entry : fs::directory_iterator(sys / "class/block")) {
        const auto& block = entry.path();
        if (fs::exists(block / "partition")) continue;
        auto parent = fs::canonical(block).parent_path();
        while (!parent.empty()) {
            if (fs::exists(parent / "idVendor")) {
                try {
                    auto vendor = trim(read_text(parent / "idVendor"));
                    auto product = trim(read_text(parent / "idProduct"));
                    std::transform(vendor.begin(), vendor.end(), vendor.begin(), ::tolower);
                    std::transform(product.begin(), product.end(), product.begin(), ::tolower);
                    if (vendor == string_field(identity, "vid") && product == string_field(identity, "pid") &&
                            trim(read_text(parent / "serial")) == string_field(identity, "usb_serial"))
                        result.push_back(block);
                } catch (const std::system_error&) {
                    // USB metadata can disappear while enumerating. A missing
                    // serial never falls back to capacity or a Linux name.
                }
                break;
            }
            auto next = parent.parent_path();
            if (next == parent) break;
            parent = next;
        }
    }
    return result;
}

std::string Recovery::candidate_node() const {
    auto disks = candidates();
    if (disks.size() != 1)
        refuse("Expected ONE matching USB disk; found " + std::to_string(disks.size()) + ". No changes made.");
    const auto& disk = disks.front();
    if (number_file(disk / "size") != positive(identity.at("sectors"), "sectors"))
        refuse("Capacity differs. No changes made.");
    std::vector<fs::path> partitions;
    for (const auto& entry : fs::directory_iterator(disk))
        if (fs::is_regular_file(entry.path() / "partition") &&
                trim(read_text(entry.path() / "partition")) ==
                    std::to_string(positive(identity.at("partition_number"), "partition_number")))
            partitions.push_back(entry.path());
    if (partitions.size() != 1) refuse("Expected partition not found uniquely.");
    return (dev / partitions.front().filename()).string();
}

Json Recovery::admission_layout(const std::string& node) const {
    if (filesystem()) {
        auto props = properties(run({"/sbin/blkid", "-p", "-o", "export", node}, 3));
        require_properties(props, {{"TYPE", identity.at("fs_type")}, {"UUID", identity.at("fs_uuid")},
                                   {"PART_ENTRY_UUID", identity.at("partuuid")}});
        return {{"kind", "filesystem"}, {"fs_type", props.at("TYPE")},
                {"fs_uuid", props.at("UUID")}, {"partuuid", props.at("PART_ENTRY_UUID")}};
    }
    auto report = rows(run({"/sbin/lvm", "lvs", "--readonly", "--devices", node,
        "--segments", "--reportformat", "json", "--units", "s", "--nosuffix", "-o",
        "lv_name,lv_uuid,vg_uuid,segtype,seg_start,seg_size,seg_pe_ranges"}, 3), "seg");
    Json normalized = Json::array();
    for (const auto& row : report) {
        Json clean = Json::object();
        for (const auto& [key, value] : row.items()) clean[key] = trim(value.get<std::string>());
        std::string ranges;
        for (const auto& part : split_words(clean.at("seg_pe_ranges").get<std::string>())) {
            if (!ranges.empty()) ranges += ' ';
            const auto separator = part.rfind(':');
            ranges += separator == std::string::npos ? part : part.substr(separator + 1);
        }
        clean["seg_pe_ranges"] = ranges;
        normalized.push_back(std::move(clean));
    }
    std::sort(normalized.begin(), normalized.end(), [](const Json& left, const Json& right) {
        return std::make_pair(left.at("lv_name").get<std::string>(), left.at("seg_start").get<std::string>()) <
               std::make_pair(right.at("lv_name").get<std::string>(), right.at("seg_start").get<std::string>());
    });
    return normalized;
}

std::string Recovery::verify() const {
    auto node = candidate_node();
    if (filesystem()) {
        admission_layout(node);
        return node;
    }
    auto props = properties(run({"/sbin/blkid", "-p", "-o", "export", node}, 3));
    require_properties(props, {{"TYPE", "LVM2_member"}, {"UUID", identity.at("pv_uuid")},
                               {"PART_ENTRY_UUID", identity.at("partuuid")}});
    auto pvs = rows(run({"/sbin/lvm", "pvs", "--readonly", "--devices", node,
                        "--reportformat", "json", "-o", "pv_uuid,vg_uuid,vg_name"}, 3), "pv");
    if (pvs.size() != 1) refuse("Unexpected PV count.");
    const auto& pv = pvs.front();
    auto vg_uuid = trim(pv.at("vg_uuid").get<std::string>());
    vg_uuid.erase(std::remove(vg_uuid.begin(), vg_uuid.end(), '-'), vg_uuid.end());
    if (trim(pv.at("pv_uuid").get<std::string>()) != string_field(identity, "pv_uuid") ||
            vg_uuid != string_field(identity, "vg_uuid") ||
            trim(pv.at("vg_name").get<std::string>()) != string_field(identity, "vg_name"))
        refuse("LVM identity differs. No changes made.");
    return node;
}

Fd BlockAccess::open_readonly(const std::string& node) const {
    int result = ::open(node.c_str(), O_RDONLY | O_NONBLOCK | O_CLOEXEC);
    if (result < 0) system_failure("open candidate");
    return Fd(result);
}
struct stat BlockAccess::held_stat(int fd) const {
    struct stat result{};
    if (::fstat(fd, &result)) system_failure("fstat candidate");
    return result;
}
struct stat BlockAccess::node_stat(const std::string& node) const {
    struct stat result{};
    if (::stat(node.c_str(), &result)) system_failure("stat candidate");
    return result;
}
uint64_t BlockAccess::number(int fd, unsigned long request) const {
    if (request == BLKSSZGET) {
        unsigned int value = 0;
        if (::ioctl(fd, request, &value)) system_failure("candidate logical block size ioctl");
        return value;
    }
    uint64_t value = 0;
    if (::ioctl(fd, request, &value)) system_failure("candidate identity ioctl");
    return value;
}

Candidate::Candidate(std::shared_ptr<Admission> admission, Fd fd, Json record)
    : admission_(std::move(admission)), fd_(std::move(fd)), record_(std::move(record)) {}
std::string Candidate::node() const { return record_.at("instance").at("node"); }
dev_t Candidate::dev() const { return record_.at("instance").at("dev").get<dev_t>(); }
std::string Candidate::sys_path() const { return record_.at("instance").at("sys_path"); }
uint64_t Candidate::diskseq() const { return record_.at("instance").at("diskseq"); }
uint64_t Candidate::partition_sectors() const { return record_.at("instance").at("partition_sectors"); }
uint64_t Candidate::logical_block_size() const { return record_.at("instance").at("logical_block_size"); }
std::string Candidate::layout_digest() const { return record_.at("layout_digest"); }
double Candidate::verified_at() const { return record_.at("verified_at"); }
double Candidate::deadline() const { return record_.at("deadline"); }
std::string Candidate::owner_epoch() const { return record_.at("owner_epoch"); }
Json Candidate::to_json() const { return record_; }
void Candidate::revalidate(const std::string& epoch, bool check_layout) {
    admission_->revalidate(*this, epoch, check_layout);
}

Admission::Admission(Json config, std::shared_ptr<Recovery> recovery, Clock clock,
                     std::string boot_id, std::shared_ptr<BlockAccess> access)
    : config_(std::move(config)), recovery_(std::move(recovery)), clock_(std::move(clock)),
      boot_id_(boot_id.empty() ? trim(read_text("/proc/sys/kernel/random/boot_id")) : std::move(boot_id)),
      layout_digest_(digest(config_.at("layout"))),
      layout_version_(config_.value("layout_version", Json(layout_digest_))), access_(std::move(access)) {
    positive(config_.at("partition_sectors"), "partition_sectors");
    positive(config_.at("logical_block_size"), "logical_block_size");
    if (config_.contains("partition_start") && !config_.at("partition_start").is_null())
        nonnegative(config_.at("partition_start"), "Enrolled partition start");
    enrollment_digest_ = digest(enrollment());
}
Json Admission::enrollment() const {
    Json result = {{"identity", recovery_->identity}, {"partition_sectors", config_.at("partition_sectors")},
        {"logical_block_size", config_.at("logical_block_size")}, {"layout_digest", layout_digest_},
        {"layout_version", layout_version_}};
    if (config_.contains("partition_start") && !config_.at("partition_start").is_null())
        result["partition_start"] = config_.at("partition_start");
    return result;
}
void Admission::check_enrollment() const {
    if (digest(enrollment()) != enrollment_digest_)
        refuse("Enrollment changed; a new admission policy is required");
}
void Admission::budget(double deadline) const {
    if (!std::isfinite(deadline) || clock_() >= deadline)
        refuse("Candidate verification deadline expired");
}
Json Admission::snapshot(const std::string& node, int fd) const {
    auto held = access_->held_stat(fd), current = access_->node_stat(node);
    if (!S_ISBLK(held.st_mode) || !S_ISBLK(current.st_mode)) refuse("Candidate must be a block device");
    if (held.st_rdev != current.st_rdev) refuse("Candidate node no longer refers to the held device");
    auto path = fs::canonical(recovery_->sys / "class/block" / fs::path(node).filename());
    if (!fs::is_regular_file(path / "partition")) refuse("Candidate is not the enrolled partition");
    auto disk = path.parent_path();
    if (trim(read_text(path / "dev")) != device_number(held.st_rdev))
        refuse("Candidate sysfs device number differs");
    auto sequence = number_file(disk / "diskseq");
    if (!sequence || access_->number(fd, BLKGETDISKSEQ) != sequence)
        refuse("Candidate disk instance differs from held fd");
    auto sectors = number_file(path / "size");
    if (sectors != config_.at("partition_sectors") || sectors > UINT64_MAX / 512 ||
            access_->number(fd, BLKGETSIZE64) != sectors * 512) refuse("Partition size differs");
    auto start = number_file(path / "start");
    if (config_.contains("partition_start") && !config_.at("partition_start").is_null() &&
            start != config_.at("partition_start")) refuse("Partition start differs from enrollment");
    auto block_size = number_file(disk / "queue/logical_block_size");
    if (!block_size || block_size != config_.at("logical_block_size") ||
            access_->number(fd, BLKSSZGET) != block_size)
        refuse("Candidate logical block size differs");
    auto partition_number = number_file(path / "partition");
    if (partition_number != recovery_->identity.at("partition_number"))
        refuse("Candidate partition number differs");
    auto disk_sectors = number_file(disk / "size");
    if (disk_sectors != recovery_->identity.at("sectors")) refuse("Disk capacity differs");
    return {{"node", node}, {"dev", held.st_rdev}, {"sys_path", path.string()},
        {"disk_sys_path", disk.string()}, {"diskseq", sequence}, {"disk_sectors", disk_sectors},
        {"partition_sectors", sectors}, {"partition_number", partition_number},
        {"partition_start", start}, {"logical_block_size", block_size}};
}
void Admission::same_instance(const std::string& node, int fd, const Json& expected) const {
    // Uniqueness is rechecked too: a second disk with the same enrolled serial
    // must invalidate an otherwise unchanged held device instance.
    if (recovery_->candidate_node() != node || snapshot(node, fd) != expected)
        refuse("Candidate changed during verification");
}
std::string Admission::layout(const std::string& node) const {
    auto actual = digest(recovery_->admission_layout(node));
    if (actual != layout_digest_) refuse("Device layout differs from enrolled metadata");
    return actual;
}
std::shared_ptr<Candidate> Admission::verify(double deadline, const std::string& epoch) {
    std::unique_lock lock(mutex_, std::try_to_lock);
    if (!lock.owns_lock()) refuse("Another candidate verification is already running");
    check_enrollment();
    budget(deadline);
    auto node = recovery_->candidate_node();
    auto fd = access_->open_readonly(node);
    auto instance = snapshot(node, fd.get());
    if (recovery_->verify() != node) refuse("Candidate changed during identity verification");
    budget(deadline);
    same_instance(node, fd.get(), instance);
    auto verified_layout = layout(node);
    budget(deadline);
    if (recovery_->verify() != node) refuse("Candidate changed during identity verification");
    same_instance(node, fd.get(), instance);
    budget(deadline);
    check_enrollment();
    Json record = {{"schema", 1}, {"boot_id", boot_id_}, {"enrollment_digest", enrollment_digest_},
        {"layout_version", layout_version_}, {"layout_digest", verified_layout}, {"instance", instance},
        {"verified_at", clock_()}, {"deadline", deadline}, {"owner_epoch", epoch}};
    return std::shared_ptr<Candidate>(new Candidate(shared_from_this(), std::move(fd), std::move(record)));
}
void Admission::revalidate(Candidate& candidate, const std::string& epoch, bool check_layout) {
    if (candidate.admission_.get() != this || !candidate.fd_)
        refuse("Candidate has no live fd from this admission policy");
    if (candidate.owner_epoch() != epoch) refuse("Candidate belongs to a different owner epoch");
    std::unique_lock lock(mutex_, std::try_to_lock);
    if (!lock.owns_lock()) refuse("Another candidate verification is already running");
    check_enrollment();
    budget(candidate.deadline());
    same_instance(candidate.node(), candidate.fd(), candidate.record_.at("instance"));
    if (check_layout) {
        layout(candidate.node());
        same_instance(candidate.node(), candidate.fd(), candidate.record_.at("instance"));
    }
    budget(candidate.deadline());
    check_enrollment();
}

void validate_data_config(const Json& config) {
    auto name = string_field(config, "map_name");
    if (!matches(name, "rr-data-[A-Za-z0-9_-]{1,64}"))
        refuse("Data map names must start with rr-data-");
    if (!matches(string_field(config, "map_uuid"), "RAMRESCUE-DATA-[A-Za-z0-9_-]{1,64}"))
        refuse("Data map UUID must use the RAMRESCUE-DATA- namespace");
    const auto base = fs::path("/run/ram-rescue-data") / name;
    if (string_field(config, "run_dir") != (base / "state").string() ||
            string_field(config, "identity_path") != (base / "identity.json").string())
        refuse("Data profile requires its canonical RAM owner and identity paths");
    auto queue = positive(config.at("queue_seconds"), "queue_seconds");
    if (queue < 2 || queue > 8) refuse("Data admission budget must be 2..8 seconds");
    nonnegative(config.at("partition_start"), "partition_start");
}

void validate_record(const Json& record) {
    if (!record.is_object() || record.size() != 3 || !record.contains("schema") ||
            !integer(record.at("schema")) || record.at("schema") != 1 ||
            !record.contains("identity") || !record.contains("guard"))
        refuse("Unsupported registry record");
    const auto& identity = record.at("identity");
    const auto& config = record.at("guard");
    if (!identity.is_object() || !config.is_object()) refuse("Registry record requires identity and guard objects");
    if (!config.contains("schema") || !integer(config.at("schema")) || config.at("schema") != 1)
        refuse("Unsupported guard configuration schema");
    auto kind = identity.value("kind", "lvm");
    std::set<std::string> allowed{"schema", "profile", "map_name", "map_uuid", "run_dir", "identity_path",
        "kernel_release", "queue_seconds", "partition_sectors", "partition_start", "logical_block_size",
        "layout", "layout_version"};
    if (kind == "lvm") allowed.insert({"root_lv", "root_fs_uuid"});
    for (const auto& [key, value] : config.items()) {
        (void)value;
        if (key == "initial_node" || key == "initial_sys_path" || key == "initial_diskseq")
            refuse("Persistent registration cannot replay a device instance");
        if (!allowed.count(key)) refuse("Unexpected runtime fields in persistent registration");
    }
    if (!matches(string_field(config, "kernel_release"), "[A-Za-z0-9._+-]{1,128}"))
        refuse("Invalid enrolled kernel release");
    auto sectors = positive(config.at("partition_sectors"), "partition_sectors");
    auto size = positive(config.at("logical_block_size"), "logical_block_size");
    auto disk_sectors = positive(identity.at("sectors"), "sectors");
    positive(identity.at("partition_number"), "partition_number");
    if (size < 512 || (size & (size - 1))) refuse("Logical block size must be a power of two at least 512");
    auto start = nonnegative(config.value("partition_start", Json(0)), "partition_start");
    if (start > disk_sectors || sectors > disk_sectors - start)
        refuse("Enrolled partition geometry exceeds the disk");
    if (kind == "filesystem") {
        if (config.value("profile", "") != "host-data")
            refuse("Filesystem identity requires a filesystem guard backend");
        validate_filesystem_identity(identity);
        validate_data_config(config);
        if (config.at("layout") != filesystem_layout(identity))
            refuse("Filesystem layout differs from registered identity");
    } else if (kind == "lvm") {
        if (config.value("profile", "") != "host" || config.value("map_name", "") != "ram-rescue-path" ||
                !matches(string_field(config, "map_uuid"), "RAMRESCUE-HOST-[A-Za-z0-9_-]{1,64}") ||
                config.value("run_dir", "") != "/run/ram-rescue-guard/state" ||
                config.value("identity_path", "") != "/etc/rescue/identity.json")
            refuse("Root registration must identify the existing boot owner");
        for (const auto* key : {"pv_uuid", "vg_uuid", "vg_name", "partuuid", "usb_serial"})
            nonempty_field(identity, key);
        if (!identity.contains("lvs") || !identity.at("lvs").is_object() ||
                !identity.at("lvs").contains(string_field(config, "root_lv")))
            refuse("Root LV must belong to the registered LVM identity");
        const auto& layout = config.at("layout");
        if (!layout.is_array() || layout.empty()) refuse("Root registration requires its enrolled linear LVM layout");
        for (const auto& row : layout)
            if (!row.is_object() || row.value("segtype", "") != "linear")
                refuse("Root registration requires its enrolled linear LVM layout");
        nonempty_field(config, "root_fs_uuid");
        auto queue = positive(config.at("queue_seconds"), "queue_seconds");
        if (queue < 2 || queue > 60) refuse("Invalid root admission budget");
    } else refuse("Unsupported registered identity kind");
    if (config.contains("layout_version") && string_field(config, "layout_version") != digest(config.at("layout")))
        refuse("Registered layout version differs from its content");
}

Json record_from_profile(const Json& profile) {
    if (!profile.is_object() || !profile.contains("schema") || !integer(profile.at("schema")) ||
            profile.at("schema") != 1 || !profile.contains("identity") || !profile.at("identity").is_object() ||
            !profile.contains("guard") || !profile.at("guard").is_object())
        refuse("Unsupported enrollment profile");
    Json record = {{"schema", profile.at("schema")}, {"identity", profile.at("identity")}, {"guard", profile.at("guard")}};
    for (const auto* key : {"initial_node", "initial_sys_path", "initial_diskseq"}) record["guard"].erase(key);
    validate_record(record);
    return record;
}

namespace {
using Mounts = std::vector<std::vector<std::string>>;
std::string unescape(const std::string& value) {
    std::string result;
    for (std::size_t i = 0; i < value.size(); ++i) {
        if (value[i] == '\\' && i + 3 < value.size() &&
                value[i + 1] >= '0' && value[i + 1] <= '7' &&
                value[i + 2] >= '0' && value[i + 2] <= '7' &&
                value[i + 3] >= '0' && value[i + 3] <= '7') {
            result += static_cast<char>((value[i + 1] - '0') * 64 +
                (value[i + 2] - '0') * 8 + value[i + 3] - '0');
            i += 3;
        } else result += value[i];
    }
    return result;
}
Mounts mounts() {
    Mounts result;
    std::istringstream input(read_text("/proc/1/mountinfo", 4 * 1024 * 1024));
    for (std::string line; std::getline(input, line);) {
        auto fields = split_words(line);
        if (fields.size() < 7) refuse("Invalid host mountinfo");
        fields[4] = unescape(fields[4]);
        result.push_back(std::move(fields));
    }
    return result;
}
std::set<fs::path> physical_disks(const std::string& device, std::set<fs::path> seen = {}) {
    auto path = fs::canonical(fs::path("/sys/dev/block") / device);
    if (!seen.insert(path).second) refuse("Cyclic block-device dependency");
    if (fs::exists(path / "partition")) return {path.parent_path()};
    std::set<fs::path> result;
    for (const auto& entry : fs::directory_iterator(path / "slaves")) {
        auto disks = physical_disks(trim(read_text(entry.path() / "dev")), seen);
        result.insert(disks.begin(), disks.end());
    }
    return result.empty() ? std::set<fs::path>{path} : result;
}
std::set<fs::path> resolved_entries(const fs::path& directory) {
    std::set<fs::path> result;
    for (const auto& entry : fs::directory_iterator(directory)) result.insert(fs::canonical(entry.path()));
    return result;
}
void check_environment(const Json& config) {
    struct utsname kernel{};
    if (uname(&kernel)) system_failure("uname");
    if (kernel.release != string_field(config, "kernel_release"))
        refuse("Data guard kernel differs from enrolled kernel_release");
    if (number_file("/sys/module/dm_multipath/parameters/queue_if_no_path_timeout_secs") <
            positive(config.at("queue_seconds"), "queue_seconds") + 2)
        refuse("Existing kernel no-path timeout is shorter than the data budget plus margin");
    for (const auto& process : fs::directory_iterator("/proc")) {
        auto name = process.path().filename().string();
        if (name.empty() || !std::all_of(name.begin(), name.end(), [](char c) { return c >= '0' && c <= '9'; }))
            continue;
        try {
            if (trim(read_text(process.path() / "comm")) == "multipathd")
                refuse("multipathd is running; competing map controllers are unsupported");
        } catch (const std::system_error& error) {
            if (error.code().value() != ENOENT) throw;
        }
    }
}
std::pair<fs::path, fs::path> check_map(const Json& config, const std::string& node, DeviceMapper& mapper) {
    auto snapshot = mapper.snapshot(config.at("map_name"));
    const auto& info = snapshot.at("info");
    struct stat held{};
    if (::stat(node.c_str(), &held)) system_failure("stat data candidate");
    if (!S_ISBLK(held.st_mode)) refuse("Data candidate must be a block partition");
    const auto expected = Json::array({Json::array({0, config.at("partition_sectors"), "multipath",
        "3 queue_if_no_path queue_mode bio 0 1 1 round-robin 0 1 1 " + device_number(held.st_rdev) + " 1"})});
    bool refused = snapshot.at("uuid") != config.at("map_uuid") || !snapshot.at("inactive").empty();
    for (const auto* key : {"suspended", "internal_suspend", "deferred_remove", "read_only"})
        refused = refused || info.at(key).get<int>() != 0;
    if (refused || table_digest(snapshot.at("active")) != table_digest(expected))
        refuse("Existing map is not the exclusive ready single-path data table");
    const auto words = split_words(snapshot.at("active").at(0).at(3).get<std::string>());
    const auto count = std::stoull(words.at(0));
    if (count >= words.size() || std::find(words.begin() + 1, words.begin() + 1 + count,
            "queue_if_no_path") == words.begin() + 1 + count)
        refuse("Data map must already enable queue_if_no_path");
    const auto map = fs::canonical(fs::path("/sys/dev/block") /
        (std::to_string(info.at("major").get<unsigned>()) + ":" + std::to_string(info.at("minor").get<unsigned>())));
    const auto path = fs::canonical(fs::path("/sys/class/block") / fs::path(node).filename());
    if (resolved_entries(map / "slaves") != std::set<fs::path>{path})
        refuse("Data map backing partition differs");
    return {path, map};
}
void check_isolation(const std::string& node, const fs::path& path, const fs::path& map,
                     const Json& identity, const Runner& runner) {
    static const std::set<std::string> critical{"/", "/usr", "/var", "/home", "/workspace", "/boot", "/boot/efi"};
    const auto dev = trim(read_text(path / "dev"));
    const auto entries = mounts();
    for (const auto& entry : entries) {
        if (entry[2] == dev) refuse("Raw partition is mounted; enroll before mounting through DM");
        if (critical.count(entry[4]) && fs::exists(fs::path("/sys/dev/block") / entry[2]) &&
                physical_disks(entry[2]).count(path.parent_path()))
            refuse("Data mode refuses a disk backing a system mount");
    }
    if (resolved_entries(path / "holders") != std::set<fs::path>{map})
        refuse("Partition must be held exclusively by the selected data map");
    if (!fs::is_empty(map / "holders"))
        refuse("Data mode accepts a filesystem directly on the map, without upper DM layers");
    const auto map_dev = trim(read_text(map / "dev"));
    std::istringstream swaps(read_text("/proc/swaps"));
    std::string line;
    std::getline(swaps, line); // Header.
    while (std::getline(swaps, line)) {
        auto fields = split_words(line);
        if (fields.size() < 2) refuse("Invalid swaps entry");
        auto swap = unescape(fields[0]);
        auto resolved = fs::weakly_canonical(swap);
        if (resolved == fs::path(node) || resolved == fs::path("/dev") / map.filename())
            refuse("Data mode does not accept swap devices");
        if (fields[1] == "file") {
            const std::vector<std::string>* enclosing = nullptr;
            for (const auto& entry : entries) {
                auto prefix = entry[4];
                while (!prefix.empty() && prefix.back() == '/') prefix.pop_back();
                prefix += '/';
                if (swap.rfind(prefix, 0) == 0 && (!enclosing || entry[4].size() > (*enclosing)[4].size()))
                    enclosing = &entry;
            }
            if (enclosing && (*enclosing)[2] == map_dev)
                refuse("Data mode does not accept a filesystem containing active swap");
        }
    }
    const auto uuid = string_field(identity, "fs_uuid"), partuuid = string_field(identity, "partuuid");
    const std::set<std::string> raw{node, "UUID=" + uuid, "PARTUUID=" + partuuid,
        "/dev/disk/by-uuid/" + uuid, "/dev/disk/by-partuuid/" + partuuid};
    std::optional<Json> labels;
    std::istringstream fstab(read_text("/proc/1/root/etc/fstab", 4 * 1024 * 1024));
    while (std::getline(fstab, line)) {
        auto fields = split_words(line);
        if (fields.empty() || fields[0][0] == '#') continue;
        auto source = unescape(fields[0]);
        auto separator = source.find('=');
        auto tag = source.substr(0, separator);
        if (separator != std::string::npos && (tag == "UUID" || tag == "PARTUUID" || tag == "LABEL" || tag == "PARTLABEL")) {
            auto value = source.substr(separator + 1);
            const auto first = value.find_first_not_of("\"'");
            value = first == std::string::npos ? "" : value.substr(first, value.find_last_not_of("\"'") - first + 1);
            source = tag + '=' + value;
            if (tag == "UUID" || tag == "PARTUUID") {
                auto wanted = tag == "UUID" ? uuid : partuuid;
                std::transform(value.begin(), value.end(), value.begin(), ::tolower);
                std::transform(wanted.begin(), wanted.end(), wanted.begin(), ::tolower);
                if (value == wanted) refuse("fstab selects the filesystem through its ambiguous UUID");
            } else {
                if (!labels) labels = properties(runner({"/sbin/blkid", "-p", "-o", "export", node}, 3));
                const auto key = tag == "LABEL" ? "LABEL" : "PART_ENTRY_NAME";
                if (labels->contains(key) && string_field(*labels, key) == value)
                    refuse("fstab selects the filesystem through its ambiguous label");
            }
        }
        if (raw.count(source) || (source.rfind("/dev/", 0) == 0 && fs::weakly_canonical(source) == fs::path(node)))
            refuse("fstab still selects the raw partition or its ambiguous UUID");
    }
}
} // namespace

Json current_profile(const Json& record, Runner runner) {
    validate_record(record);
    if (record.at("identity").value("kind", "lvm") != "filesystem")
        refuse("Root protection is already owned by the protected boot");
    Json profile = record;
    auto& config = profile["guard"];
    auto recovery = std::make_shared<Recovery>(profile.at("identity"), std::move(runner));
    check_environment(config);
    DeviceMapper mapper;
    if (mapper.target_version("multipath") < std::array<unsigned, 3>{1, 15, 0})
        refuse("DM_MPATH_PROBE_PATHS requires multipath target >= 1.15.0");
    auto node = recovery->candidate_node();
    auto [path, map] = check_map(config, node, mapper);
    check_isolation(node, path, map, recovery->identity, recovery->run);
    auto policy = std::make_shared<Admission>(config, recovery);
    auto candidate = policy->verify(mono() + 15, "registered-start");
    candidate->revalidate("registered-start");
    check_map(config, candidate->node(), mapper);
    config["initial_node"] = candidate->node();
    config["initial_sys_path"] = candidate->sys_path();
    config["initial_diskseq"] = candidate->diskseq();
    return profile;
}

void validate_data_runtime(const Json& config, const std::shared_ptr<Recovery>& recovery) {
    validate_data_config(config);
    if (!recovery->filesystem()) refuse("Data mode requires an enrolled filesystem identity");
    check_environment(config);
    auto node = recovery->candidate_node();
    DeviceMapper mapper;
    auto [path, map] = check_map(config, node, mapper);
    if (node != string_field(config, "initial_node") || path.string() != string_field(config, "initial_sys_path") ||
            number_file(path.parent_path() / "diskseq") != config.at("initial_diskseq"))
        refuse("Initial data path changed since enrollment; enroll again before starting");
    check_isolation(node, path, map, recovery->identity, recovery->run);
    auto policy = std::make_shared<Admission>(config, recovery);
    auto candidate = policy->verify(mono() + 15, "data-startup");
    candidate->revalidate("data-startup");
}

} // namespace rescue
