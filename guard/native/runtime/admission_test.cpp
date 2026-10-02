#include "admission.hpp"

#include <cerrno>
#include <fcntl.h>
#include <fstream>
#include <iostream>
#include <limits>
#include <linux/fs.h>
#include <map>
#include <stdexcept>
#include <sys/sysmacros.h>
#include <thread>
#include <unistd.h>

using namespace rescue;

namespace {
void require(bool condition, const std::string& message) {
    if (!condition) throw std::runtime_error(message);
}
template<class Function> void rejects(Function operation, const std::string& expected) {
    try { operation(); }
    catch (const std::exception& error) {
        require(std::string(error.what()).find(expected) != std::string::npos,
                "Expected '" + expected + "', got: " + error.what());
        return;
    }
    throw std::runtime_error("Expected refusal: " + expected);
}
void write(const fs::path& path, const std::string& value) {
    fs::create_directories(path.parent_path());
    std::ofstream output(path);
    output << value;
    if (!output) throw std::runtime_error("Cannot write test fixture");
}

struct FakeAccess : BlockAccess {
    dev_t node_device = makedev(8, 17);
    uint64_t sequence = 45, size = 2048 * 512, block_size = 512;
    bool unsupported_ioctl = false;
    mutable std::map<int, std::pair<uint64_t, dev_t>> held;
    mutable std::vector<int> opened;
    Fd open_readonly(const std::string&) const override {
        auto descriptor = BlockAccess::open_readonly("/dev/null");
        require((fcntl(descriptor.get(), F_GETFL) & (O_ACCMODE | O_NONBLOCK)) == (O_RDONLY | O_NONBLOCK),
                "Candidate must hold a read-only nonblocking descriptor");
        require(fcntl(descriptor.get(), F_GETFD) & FD_CLOEXEC, "Candidate descriptor must be close-on-exec");
        held[descriptor.get()] = {sequence, node_device};
        opened.push_back(descriptor.get());
        return descriptor;
    }
    struct stat held_stat(int fd) const override {
        require(fcntl(fd, F_GETFD) >= 0, "Held descriptor unexpectedly closed");
        struct stat result{};
        result.st_mode = S_IFBLK | 0600;
        result.st_rdev = held.at(fd).second;
        return result;
    }
    struct stat node_stat(const std::string&) const override {
        struct stat result{};
        result.st_mode = S_IFBLK | 0600;
        result.st_rdev = node_device;
        return result;
    }
    uint64_t number(int fd, unsigned long request) const override {
        if (unsupported_ioctl) throw std::system_error(ENOTTY, std::generic_category(), "unsupported ioctl");
        if (request == BLKGETDISKSEQ) return held.at(fd).first;
        if (request == BLKGETSIZE64) return size;
        if (request == BLKSSZGET) return block_size;
        throw std::runtime_error("Unexpected ioctl");
    }
    void all_closed() const {
        for (int fd : opened) require(fcntl(fd, F_GETFD) == -1 && errno == EBADF, "Leaked candidate fd");
    }
};

struct Fixture {
    fs::path root, sys, dev, block, disk_path;
    Json identity, row, config, props;
    std::shared_ptr<FakeAccess> access = std::make_shared<FakeAccess>();
    std::shared_ptr<Recovery> recovery;
    std::shared_ptr<Admission> policy;
    double now = 100;
    std::string node;
    std::vector<std::vector<std::string>> calls;
    std::function<void(const std::vector<std::string>&)> hook;
    Fixture() {
        fs::create_directories("lab/work");
        std::string pattern = fs::absolute("lab/work/cpp-admission-XXXXXX").string();
        auto location = mkdtemp(pattern.data());
        require(location != nullptr, "mkdtemp failed");
        root = location;
        sys = root / "sys";
        dev = root / "dev";
        fs::create_directories(sys / "class/block");
        fs::create_directories(dev);
        identity = {{"vid", "1234"}, {"pid", "abcd"}, {"usb_serial", "enrolled"}, {"sectors", 10000},
            {"partition_number", 1}, {"partuuid", "partition-id"}, {"pv_uuid", "pv-id"}, {"vg_uuid", "vgid"},
            {"vg_name", "labrescue"}, {"lvs", {{"ubuntu", {{"dm_uuid", "LVM-vgidlv-id"}}}}}};
        row = {{"lv_name", "ubuntu"}, {"lv_uuid", "lv-id"}, {"vg_uuid", "vg-id"}, {"segtype", "linear"},
            {"seg_start", "0"}, {"seg_size", "1024"}, {"seg_pe_ranges", "/dev/old1:0-127"}};
        auto normalized = row;
        normalized["seg_pe_ranges"] = "0-127";
        config = {{"partition_sectors", 2048}, {"logical_block_size", 512}, {"layout", Json::array({normalized})}};
        props = {{"TYPE", "LVM2_member"}, {"UUID", "pv-id"}, {"PART_ENTRY_UUID", "partition-id"}};
        disk("sdb");
        node = (dev / "sdb1").string();
        block = fs::canonical(sys / "class/block/sdb1");
        disk_path = block.parent_path();
        reset_policy();
    }
    ~Fixture() {
        policy.reset();
        recovery.reset();
        std::error_code ignored;
        fs::remove_all(root, ignored);
    }
    void disk(const std::string& name) {
        auto usb = sys / "devices" / name / "usb";
        write(usb / "idVendor", "1234");
        write(usb / "idProduct", "abcd");
        write(usb / "serial", "enrolled");
        auto disk = usb / "block" / name;
        write(disk / "size", "10000");
        write(disk / "diskseq", "45");
        write(disk / "queue/logical_block_size", "512");
        auto partition = disk / (name + "1");
        write(partition / "partition", "1");
        write(partition / "size", "2048");
        write(partition / "start", "2048");
        write(partition / "dev", "8:17");
        fs::create_symlink(disk, sys / "class/block" / name);
        fs::create_symlink(partition, sys / "class/block" / (name + "1"));
        write(dev / (name + "1"), "");
    }
    void reset_policy() {
        recovery = std::make_shared<Recovery>(identity,
            [this](const auto& args, double timeout) { return run(args, timeout); }, sys, dev);
        policy = std::make_shared<Admission>(config, recovery, [this] { return now; }, "this-boot", access);
    }
    std::string run(const std::vector<std::string>& args, double timeout) {
        require(timeout == 3, "Content probe timeout changed");
        calls.push_back(args);
        if (hook) hook(args);
        if (args.at(0) == "/sbin/blkid") {
            require(args.back() == node, "Probed an unrelated device");
            std::string output;
            for (const auto& [key, value] : props.items()) output += key + '=' + value.get<std::string>() + '\n';
            return output;
        }
        require(args.at(0) == "/sbin/lvm", "Unexpected command");
        auto devices = std::find(args.begin(), args.end(), "--devices");
        require(devices != args.end() && std::next(devices) != args.end() && *std::next(devices) == node,
                "LVM must be explicitly restricted to enrolled partition");
        require(std::find(args.begin(), args.end(), "--readonly") != args.end(), "LVM must be read-only");
        if (args.at(1) == "pvs") return Json{{"report", Json::array({{{"pv", Json::array({{
            {"pv_uuid", "pv-id"}, {"vg_uuid", "vgid"}, {"vg_name", "labrescue"}}})}}})}}.dump();
        if (args.at(1) == "lvs") return Json{{"report", Json::array({{{"seg", Json::array({row})}}})}}.dump();
        throw std::runtime_error("Unexpected LVM command");
    }
    std::shared_ptr<Candidate> verify() { return policy->verify(106, "owner-1"); }
    std::vector<std::string> operations() const {
        std::vector<std::string> result;
        for (const auto& call : calls) result.push_back(call.at(1));
        return result;
    }
    void filesystem(const std::string& type = "ext4") {
        identity = {{"kind", "filesystem"}, {"vid", "1234"}, {"pid", "abcd"},
            {"usb_serial", "enrolled"}, {"sectors", 10000}, {"partition_number", 1},
            {"partuuid", "partition-id"}, {"fs_type", type}, {"fs_uuid", "filesystem-id"}};
        props = {{"TYPE", type}, {"UUID", "filesystem-id"}, {"PART_ENTRY_UUID", "partition-id"}};
        config["layout"] = {{"kind", "filesystem"}, {"fs_type", type},
            {"fs_uuid", "filesystem-id"}, {"partuuid", "partition-id"}};
        config["partition_start"] = 2048;
        reset_policy();
    }
};

Json data_profile() {
    Json identity = {{"kind", "filesystem"}, {"vid", "1234"}, {"pid", "5678"},
        {"usb_serial", "test-serial"}, {"sectors", 32768}, {"partition_number", 1},
        {"partuuid", "test-partition"}, {"fs_type", "ext4"}, {"fs_uuid", "test-filesystem"}};
    return {{"schema", 1}, {"identity", identity}, {"guard", {
        {"schema", 1}, {"profile", "host-data"}, {"map_name", "rr-data-test"},
        {"map_uuid", "RAMRESCUE-DATA-test"}, {"kernel_release", "7.0.0-test"},
        {"run_dir", "/run/ram-rescue-data/rr-data-test/state"},
        {"identity_path", "/run/ram-rescue-data/rr-data-test/identity.json"},
        {"queue_seconds", 8}, {"partition_sectors", 16384}, {"partition_start", 2048},
        {"logical_block_size", 512}, {"layout", {{"kind", "filesystem"}, {"fs_type", "ext4"},
            {"fs_uuid", "test-filesystem"}, {"partuuid", "test-partition"}}},
        {"initial_node", "/dev/sdb1"}, {"initial_sys_path", "/sys/devices/old/sdb/sdb1"},
        {"initial_diskseq", 12}}}};
}
} // namespace

int main() {
    unsigned passed = 0;
    auto test = [&passed](const char* name, auto operation) {
        try { operation(); ++passed; }
        catch (const std::exception& error) {
            std::cerr << name << ": " << error.what() << '\n';
            throw;
        }
    };
    try {
        test("successful LVM admission holds fd and checks identity twice", [] {
            Fixture f;
            {
                auto candidate = f.verify();
                require(f.operations() == std::vector<std::string>{"-p", "pvs", "lvs", "-p", "pvs"},
                        "Identity/layout probe sequence changed");
                require(candidate->dev() == makedev(8, 17) && candidate->diskseq() == 45 &&
                        candidate->partition_sectors() == 2048 && candidate->logical_block_size() == 512 &&
                        candidate->sys_path() == f.block.string() && candidate->verified_at() == 100 &&
                        candidate->deadline() == 106, "Credential fields differ");
                require(f.access->opened.size() == 1 && fcntl(candidate->fd(), F_GETFD) >= 0, "Missing held fd");
            }
            f.access->all_closed();
        });
        test("duplicate serial rejects before opening or probing media", [] {
            Fixture f; f.disk("sdc");
            rejects([&] { f.verify(); }, "ONE");
            require(f.calls.empty() && f.access->opened.empty(), "Duplicate serial touched media");
        });
        test("missing partition rejects before opening or probing media", [] {
            Fixture f; fs::remove(f.sys / "class/block/sdb1"); fs::remove(f.block / "partition");
            rejects([&] { f.verify(); }, "partition");
            require(f.calls.empty() && f.access->opened.empty(), "Missing partition touched media");
        });
        test("wrong identity rejects and releases fd", [] {
            Fixture f; f.props["UUID"] = "wrong-pv";
            rejects([&] { f.verify(); }, "UUID");
            require(f.calls.size() == 1, "Unexpected further probes");
            f.access->all_closed();
        });
        test("wrong extent layout rejects before second identity check", [] {
            Fixture f; f.row["seg_pe_ranges"] = "/dev/sdb1:128-255";
            rejects([&] { f.verify(); }, "layout");
            require(f.operations() == std::vector<std::string>{"-p", "pvs", "lvs"}, "Unexpected probe sequence");
            f.access->all_closed();
        });
        test("same dev_t reenumeration cannot override held fd identity", [] {
            Fixture f;
            f.hook = [&](const auto& args) { if (args[1] == "pvs") write(f.disk_path / "diskseq", "46"); };
            rejects([&] { f.verify(); }, "disk instance");
            f.access->all_closed();
        });
        test("node reassignment cannot override held fd", [] {
            Fixture f;
            f.hook = [&](const auto&) { f.access->node_device = makedev(8, 33); };
            rejects([&] { f.verify(); }, "held device");
            f.access->all_closed();
        });
        test("unsupported diskseq ioctl fails closed before probing", [] {
            Fixture f; f.access->unsupported_ioctl = true;
            rejects([&] { f.verify(); }, "unsupported");
            require(f.calls.empty(), "Read media with unsupported instance attestation");
            f.access->all_closed();
        });
        test("capacity ioctl must agree with enrolled sysfs size", [] {
            Fixture f; f.access->size -= 512;
            rejects([&] { f.verify(); }, "size");
            require(f.calls.empty(), "Read media with different geometry");
            f.access->all_closed();
        });
        test("block size ioctl must agree with sysfs", [] {
            Fixture f; f.access->block_size = 4096;
            rejects([&] { f.verify(); }, "block size");
            require(f.calls.empty(), "Read media with different block size");
            f.access->all_closed();
        });
        test("changed block size cannot override original enrollment", [] {
            Fixture f; f.access->block_size = 4096; write(f.disk_path / "queue/logical_block_size", "4096");
            rejects([&] { f.verify(); }, "block size");
            f.access->all_closed();
        });
        test("enrollment mutation invalidates outstanding credential", [] {
            Fixture f; auto candidate = f.verify();
            auto before = f.calls.size();
            f.recovery->identity["partuuid"] = "uncoordinated-change";
            rejects([&] { candidate->revalidate("owner-1"); }, "Enrollment changed");
            require(f.calls.size() == before, "Changed policy still read media");
        });
        test("expired or nonfinite budget never reads media", [] {
            Fixture f; f.now = 106;
            rejects([&] { f.verify(); }, "deadline");
            rejects([&] { f.policy->verify(std::numeric_limits<double>::infinity(), "owner"); }, "deadline");
            rejects([&] { f.policy->verify(std::numeric_limits<double>::quiet_NaN(), "owner"); }, "deadline");
            require(f.calls.empty() && f.access->opened.empty(), "Expired budget touched media");
        });
        test("slow identity cannot extend deadline", [] {
            Fixture f; f.hook = [&](const auto&) { f.now = 106; };
            rejects([&] { f.verify(); }, "deadline");
            require(f.operations() == std::vector<std::string>{"-p", "pvs"}, "Unexpected postdeadline probe");
            f.access->all_closed();
        });
        test("overlapping verification cannot create second descriptor", [] {
            Fixture f; bool checked = false;
            f.hook = [&](const auto&) {
                if (checked) return;
                checked = true;
                std::exception_ptr failure;
                std::thread overlap([&] {
                    try { rejects([&] { f.policy->verify(106, "owner-2"); }, "already running"); }
                    catch (...) { failure = std::current_exception(); }
                });
                overlap.join();
                if (failure) std::rethrow_exception(failure);
            };
            auto candidate = f.verify();
            require(checked && f.access->opened.size() == 1, "Overlapping admission opened another fd");
        });
        test("final revalidation checks layout without reopening fd", [] {
            Fixture f; auto candidate = f.verify(); candidate->revalidate("owner-1");
            require(f.operations() == std::vector<std::string>{"-p", "pvs", "lvs", "-p", "pvs", "lvs"},
                    "Final layout check missing");
            require(f.access->opened.size() == 1, "Revalidation reopened candidate");
        });
        test("final layout mutation rejects credential", [] {
            Fixture f; auto candidate = f.verify(); f.row["seg_size"] = "1025";
            rejects([&] { candidate->revalidate("owner-1"); }, "layout");
        });
        test("partition start mutation invalidates credential", [] {
            Fixture f; auto candidate = f.verify(); write(f.block / "start", "4096");
            rejects([&] { candidate->revalidate("owner-1"); }, "changed");
        });
        test("diskseq change during final layout read rejects credential", [] {
            Fixture f; auto candidate = f.verify();
            f.hook = [&](const auto&) { write(f.disk_path / "diskseq", "46"); };
            rejects([&] { candidate->revalidate("owner-1"); }, "disk instance");
        });
        test("new duplicate serial invalidates live unchanged fd", [] {
            Fixture f; auto candidate = f.verify(); f.disk("sdc");
            rejects([&] { candidate->revalidate("owner-1"); }, "ONE");
        });
        test("wrong owner is rejected before media reads", [] {
            Fixture f; auto candidate = f.verify(); auto before = f.calls.size();
            rejects([&] { candidate->revalidate("owner-2"); }, "owner epoch");
            require(f.calls.size() == before, "Wrong owner read media");
        });
        test("deadline checked after final layout probe", [] {
            Fixture f; auto candidate = f.verify();
            f.hook = [&](const auto&) { f.now = 106; };
            rejects([&] { candidate->revalidate("owner-1"); }, "deadline");
        });
        test("pre-mutation revalidation skips layout only", [] {
            Fixture f; auto candidate = f.verify(); auto before = f.calls.size();
            candidate->revalidate("owner-1", false);
            require(f.calls.size() == before, "Fast revalidation unexpectedly probed media");
            write(f.disk_path / "diskseq", "46");
            rejects([&] { candidate->revalidate("owner-1", false); }, "disk instance");
        });
        test("serialized credential cannot mutate authority or carry fd", [] {
            Fixture f; auto candidate = f.verify(); auto record = candidate->to_json();
            require(!record.contains("fd") && record.at("boot_id") == "this-boot", "Invalid credential serialization");
            record["instance"]["diskseq"] = 999; record["owner_epoch"] = "wrong";
            require(candidate->diskseq() == 45 && candidate->owner_epoch() == "owner-1", "Credential was mutable");
        });
        test("candidate keeps policy and fd alive until last owner releases", [] {
            Fixture f; auto candidate = f.verify(); auto worker = candidate; int fd = candidate->fd();
            f.policy.reset(); candidate.reset();
            worker->revalidate("owner-1"); require(fcntl(fd, F_GETFD) >= 0, "Worker lost held descriptor");
            worker.reset(); f.access->all_closed();
        });
        test("filesystem admission probes only selected partition", [] {
            for (const auto* type : {"ext4", "vfat", "exfat"}) {
                Fixture f; f.filesystem(type);
                { auto candidate = f.verify(); candidate->revalidate("owner-1");
                  require(f.operations() == std::vector<std::string>{"-p", "-p", "-p", "-p"},
                          "Filesystem admission invoked an unrelated backend"); }
                f.access->all_closed();
            }
        });
        test("filesystem wrong UUID and partition start reject", [] {
            Fixture f; f.filesystem(); f.props["UUID"] = "wrong-filesystem";
            rejects([&] { f.verify(); }, "UUID");
            f.props["UUID"] = "filesystem-id"; write(f.block / "start", "4096"); f.calls.clear();
            rejects([&] { f.verify(); }, "Partition start");
            require(f.calls.empty(), "Geometry mismatch read media");
            f.access->all_closed();
        });
        test("unscoped or mutating LVM invocation rejected before execution", [] {
            rejects([] { readonly({"/sbin/lvm", "pvs", "--readonly"}); }, "explicitly selected");
            rejects([] { readonly({"/sbin/lvm", "pvs", "--devices", "/dev/not-real"}); }, "explicitly selected");
            rejects([] { readonly({"/sbin/lvm", "pvs", "--readonly", "--devices"}); }, "explicitly selected");
        });
        test("persistent record discards instances without changing source", [] {
            auto profile = data_profile(), original = profile;
            auto record = record_from_profile(profile);
            for (const auto* key : {"initial_node", "initial_diskseq", "initial_sys_path"})
                require(!record.at("guard").contains(key), "Persistent record retained stale instance");
            record["identity"]["usb_serial"] = "caller mutation";
            require(profile == original, "Record mutation changed profile");
        });
        test("persistent record rejects stale instances and runtime state", [] {
            for (const auto* key : {"initial_node", "initial_diskseq", "initial_sys_path", "owner_epoch"}) {
                auto record = record_from_profile(data_profile()); record["guard"][key] = "stale";
                rejects([&] { validate_record(record); }, key == std::string("owner_epoch") ? "runtime fields" : "replay");
            }
        });
        test("record rejects changed filesystem identity with old layout", [] {
            for (const auto* key : {"fs_uuid", "partuuid", "fs_type"}) {
                auto record = record_from_profile(data_profile());
                record["identity"][key] = key == std::string("fs_type") ? "vfat" : "changed";
                rejects([&] { validate_record(record); }, "layout differs");
            }
        });
        test("record geometry overflow bool float invalid sizes rejected", [] {
            auto record = record_from_profile(data_profile());
            for (const Json& value : {Json(true), Json(512.0), Json(0), Json(-512), Json(513)}) {
                auto invalid = record; invalid["guard"]["logical_block_size"] = value;
                rejects([&] { validate_record(invalid); }, value == 513 ? "power of two" : "integer");
            }
            record["guard"]["partition_start"] = UINT64_MAX;
            rejects([&] { validate_record(record); }, "geometry");
        });
        test("record enforces canonical namespace and RAM paths", [] {
            auto original = record_from_profile(data_profile());
            for (const auto& change : std::vector<std::pair<std::string, Json>>{
                    {"map_name", "foreign"}, {"map_uuid", "mpath-foreign"}, {"run_dir", "/run/other"},
                    {"identity_path", "/tmp/identity"}, {"queue_seconds", 9}, {"partition_start", -1}}) {
                auto record = original; record["guard"][change.first] = change.second;
                bool rejected = false;
                try { validate_record(record); } catch (const std::exception&) { rejected = true; }
                require(rejected, "Invalid persistent config admitted: " + change.first);
            }
        });
        test("layout version must match exact enrolled content", [] {
            auto record = record_from_profile(data_profile());
            record["guard"]["layout_version"] = digest(record.at("guard").at("layout"));
            validate_record(record);
            record["guard"]["layout_version"] = "different";
            rejects([&] { validate_record(record); }, "layout version");
        });
        test("unsupported filesystem and foreign profile rejected", [] {
            auto record = record_from_profile(data_profile()); record["identity"]["fs_type"] = "ntfs";
            rejects([&] { validate_record(record); }, "Unsupported filesystem");
            record = record_from_profile(data_profile()); record["guard"]["profile"] = "host";
            rejects([&] { validate_record(record); }, "filesystem guard backend");
        });
        test("root registration recognizes existing owner but cannot start it", [] {
            auto record = record_from_profile(data_profile());
            auto& identity = record["identity"]; identity.erase("kind");
            identity.update({{"pv_uuid", "pv"}, {"vg_uuid", "vg"}, {"vg_name", "portable"},
                {"lvs", {{"ubuntu", {{"dm_uuid", "LVM-test-root"}}}}}});
            record["guard"].update({{"profile", "host"}, {"map_name", "ram-rescue-path"},
                {"map_uuid", "RAMRESCUE-HOST-test"}, {"run_dir", "/run/ram-rescue-guard/state"},
                {"identity_path", "/etc/rescue/identity.json"}, {"root_lv", "ubuntu"},
                {"root_fs_uuid", "root-fs"}, {"layout", Json::array({{{"segtype", "linear"}, {"lv_name", "ubuntu"}}})}});
            validate_record(record);
            rejects([&] { current_profile(record); }, "already owned");
        });
        test("invalid cold record fails before discovery or subprocess", [] {
            auto record = record_from_profile(data_profile()); record["guard"]["initial_node"] = "/dev/old";
            unsigned calls = 0;
            rejects([&] { current_profile(record, [&](const auto&, double) { ++calls; return ""; }); }, "replay");
            require(calls == 0, "Invalid record started probes");
        });
    } catch (...) { return 1; }
    std::cout << passed << " admission checks passed\n";
    return 0;
}
