#include "admission.hpp"

#include <fstream>
#include <iostream>
#include <stdexcept>
#include <sys/stat.h>
#include <unistd.h>

using namespace rescue;
namespace {
unsigned checks = 0;
void require(bool condition, const char* message) {
    ++checks;
    if (!condition) throw std::runtime_error(message);
}
template<class F> void rejects(F operation, const char* message) {
    bool rejected = false;
    try { operation(); } catch (const std::exception&) { rejected = true; }
    require(rejected, message);
}
}
int main() {
    try {
        struct stat info{};
        info.st_mode = S_IFREG | 0644; info.st_uid = 0; info.st_nlink = 1;
        require(storage_metadata_trusted(info, false, 0), "root-owned regular input accepted");
        info.st_uid = 1000;
        require(!storage_metadata_trusted(info, false, 0), "wrong owner refused");
        info.st_uid = 0;
        for (auto permission : {0020, 0002}) {
            info.st_mode = S_IFREG | 0644 | permission;
            require(!storage_metadata_trusted(info, false, 0), "writable trusted input refused");
        }
        info.st_mode = S_IFREG | 0644; info.st_nlink = 2;
        require(!storage_metadata_trusted(info, false, 0), "hard-linked input refused");
        info.st_nlink = 1;
        for (auto kind : {S_IFLNK, S_IFCHR, S_IFBLK, S_IFIFO, S_IFSOCK}) {
            info.st_mode = kind | 0600;
            require(!storage_metadata_trusted(info, false, 0), "non-regular input refused");
        }
        info.st_mode = S_IFDIR | 0755; info.st_nlink = 2;
        require(storage_metadata_trusted(info, true, 0), "ordinary root directory accepted");
        info.st_mode |= 0020;
        require(!storage_metadata_trusted(info, true, 0), "group-writable parent refused");
        require(bool(trusted_directory("/")), "actual root directory held");
        rejects([] { trusted_directory("relative"); }, "relative trust root refused");
        rejects([] { trusted_directory("/tmp"); }, "world-writable ancestor refused even sticky");
        rejects([] { trusted_directory("/proc/self"); }, "symlink ancestor refused");
        rejects([] { trusted_directory("/run/../etc"); }, "parent traversal refused");
        rejects([] { trusted_directory(fs::path(std::string("/run\0evil", 9))); }, "NUL path refused");
        require(parse_json_input("{\"identity\":{\"a\":1},\"other\":{\"a\":2}}")
                .at("other").at("a") == 2, "separate JSON key scopes accepted");
        rejects([] { parse_json_input("{\"a\":1,\"a\":2}"); }, "duplicate JSON fields refused");
        require(parse_json_input(std::string(64, '[') + std::string(64, ']')).is_array(), "64 nesting levels accepted");
        rejects([] { parse_json_input(std::string(65, '[') + std::string(65, ']')); }, "65 empty containers refused before hashing");
        rejects([] { parse_json_input(std::string(66, '[') + "0" + std::string(66, ']')); }, "deep nesting refused before hashing");
        rejects([] { parse_json_input(std::string(log_limit + 1, ' ')); }, "input limit enforced");
        rejects([] { parse_blkid_output("UUID=good\nUUID=other\n"); }, "duplicate helper identity refused");
        rejects([] { parse_blkid_output(std::string("UUID=good\0other", 15)); }, "NUL helper identity refused");
        rejects([] { parse_lvm_rows("{\"report\":[{\"pv\":{}}]}", "pv"); }, "non-array helper rows refused");
        std::string pattern = "/tmp/rescue-security-XXXXXX";
        const auto name = ::mkdtemp(pattern.data());
        if (!name) throw std::runtime_error("mkdtemp");
        const fs::path folder(name);
        struct Cleanup { fs::path path; ~Cleanup() { fs::remove_all(path); } } cleanup{folder};
        atomic_json(folder / "state", {{"value", 1}});
        require(load_json(folder / "state").at("value") == 1, "safe atomic state publication");
        fs::create_symlink(folder / "state", folder / "alias");
        rejects([&] { atomic_json(folder / "alias", {{"value", 2}}); }, "symlink state destination refused");
        fs::create_hard_link(folder / "state", folder / "hardlink");
        rejects([&] { atomic_json(folder / "state", {{"value", 2}}); }, "aliased state destination refused");
        fs::remove(folder / "hardlink");
        fs::permissions(folder / "state", fs::perms::group_write, fs::perm_options::add);
        rejects([&] { atomic_json(folder / "state", {{"value", 2}}); }, "writable state destination refused");
        require(load_json(folder / "state").at("value") == 1, "rejection preserves existing state");
        fs::permissions(folder, fs::perms::others_write, fs::perm_options::add);
        rejects([&] { atomic_json(folder / "other", {{"value", 2}}); }, "writable publication directory refused");
    } catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
    std::cout << checks << " native storage/input security checks passed\n";
}
