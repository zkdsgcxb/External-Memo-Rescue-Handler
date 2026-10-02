#include "controller.hpp"

#include <iostream>
#include <limits>
#include <stdexcept>

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
Json host_config() {
    return {{"profile", "host"}, {"map_name", "ram-rescue-path"}, {"map_uuid", "RAMRESCUE-HOST-test"},
        {"run_dir", "/run/ram-rescue-guard/state"}, {"identity_path", "/etc/rescue/identity.json"},
        {"kernel_release", "7.0.0-test"}};
}
Json data_config() {
    return {{"profile", "host-data"}, {"map_name", "rr-data-test"}, {"map_uuid", "RAMRESCUE-DATA-test"},
        {"run_dir", "/run/ram-rescue-data/rr-data-test/state"},
        {"identity_path", "/run/ram-rescue-data/rr-data-test/identity.json"},
        {"kernel_release", "7.0.0-test"}, {"queue_seconds", 8}, {"partition_start", 2048}};
}
} // namespace

int main() {
    unsigned passed = 0;
    auto test = [&passed](const char* name, auto operation) {
        try { operation(); ++passed; }
        catch (const std::exception& error) {
            std::cerr << name << ": " << error.what() << '\n'; throw;
        }
    };
    try {
        test("lab defaults remain explicit and isolated", [] {
            Config config(Json::object());
            require(config.lab() && config.name == "lab-path" && config.uuid == "mpath-RAMRESCUE-LAB" &&
                    config.run == "/run" && config.identity == "/etc/rescue/identity.json",
                    "Lab defaults drifted");
        });
        test("host config has no implicit map ownership", [] {
            Config config(host_config());
            require(!config.lab() && config.device == "/dev/mapper/ram-rescue-path", "Wrong host map");
            for (const auto* key : {"map_name", "map_uuid", "run_dir", "kernel_release"}) {
                auto invalid = host_config(); invalid.erase(key);
                rejects([&] { Config ignored(invalid); }, "Host profile requires");
            }
        });
        test("unrecognized profile fails before map access", [] {
            rejects([] { Config ignored({{"profile", "automatic"}}); }, "Unsupported guard profile");
        });
        test("map names cannot be paths or special directory entries", [] {
            for (const auto* name : {".", "..", "../other", "/dev/dm-0", "has space", ""})
                rejects([&] { Config ignored({{"map_name", name}}); }, "map name");
            rejects([] { Config ignored({{"map_name", std::string(128, 'a')}}); }, "map name");
        });
        test("map UUID rejects empty whitespace NUL and oversize", [] {
            for (const std::string& uuid : {std::string(), std::string("has space"), std::string("has\ttab"),
                    std::string("has\0nul", 7), std::string(128, 'a')})
                rejects([&] { Config ignored({{"map_uuid", uuid}}); }, "map UUID");
        });
        test("state directory must be dedicated absolute without parent traversal", [] {
            for (const auto* path : {"relative", "/", "/run/one/../two"})
                rejects([&] { Config ignored({{"run_dir", path}}); }, "run_dir");
            rejects([] { Config ignored({{"run_dir", std::string("/run/state\0other", 16)}}); }, "run_dir");
        });
        test("identity path must be absolute without parent traversal", [] {
            for (const auto* path : {"relative", "/etc/../identity.json"})
                rejects([&] { Config ignored({{"identity_path", path}}); }, "identity_path");
            rejects([] { Config ignored({{"identity_path", std::string("/etc/id\0other", 13)}}); }, "identity_path");
        });
        test("ordinary data uses identical controller with canonical namespace", [] {
            Config config(data_config());
            require(config.profile == "host-data" && config.name == "rr-data-test" &&
                    config.device == "/dev/mapper/rr-data-test", "Wrong data mapping");
            auto invalid = data_config(); invalid["run_dir"] = "/run/another-owner";
            rejects([&] { Config ignored(invalid); }, "canonical RAM owner");
        });
        test("ordinary data budget cannot narrow overflow to acceptable int", [] {
            for (const Json& queue : {Json(true), Json(8.0), Json(1), Json(9), Json(uint64_t(4294967304)),
                    Json(std::numeric_limits<uint64_t>::max())}) {
                auto invalid = data_config(); invalid["queue_seconds"] = queue;
                bool rejected = false;
                try { Config ignored(invalid); } catch (const std::exception&) { rejected = true; }
                require(rejected, "Invalid admission budget accepted");
            }
        });
        test("single-path table preserves full enrolled sector range", [] {
            const auto text = table(2048, "8:17");
            require(text == "0 2048 multipath 3 queue_if_no_path queue_mode bio 0 1 1 round-robin 0 1 1 8:17 1",
                    "Recovery table topology changed");
            const auto targets = table_targets(text);
            require(targets.size() == 1 && targets[0][0] == 0 && targets[0][1] == 2048 &&
                    targets[0][2] == "multipath", "DM target geometry changed");
        });
        test("empty geometry refuses before creating a DM table", [] {
            rejects([] { table(0, "8:17"); }, "partition size");
        });
        test("incomplete DM table syntax is rejected", [] {
            for (const auto* text : {"", "0", "0 2048", "0 2048 multipath", "invalid 2048 multipath params"})
                rejects([&] { table_targets(text); }, "DM table");
        });
        test("only kernel mutable policy bits are normalized", [] {
            auto expected = table_targets(table(2048, "8:17"));
            auto dequeued = table_targets("0 2048 multipath 2 queue_mode bio 0 1 0 round-robin 0 1 1 8:17 1");
            require(table_digest(expected) == table_digest(dequeued), "Kernel queue policy normalization changed");
            require(table_digest(expected) != table_digest(table_targets(table(2049, "8:17"))),
                    "Changed geometry lost from map digest");
            require(table_digest(expected) != table_digest(table_targets(table(2048, "8:33"))),
                    "Changed underlying device lost from map digest");
        });
    } catch (...) { return 1; }
    std::cout << passed << " controller checks passed\n";
    return 0;
}
