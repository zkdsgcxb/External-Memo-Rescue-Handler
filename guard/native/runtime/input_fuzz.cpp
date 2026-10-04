// Deterministic mutation corpus for CI/sanitizers. No device access, commands,
// trusted-input filesystem writes, or recovery operations are performed.
#include "controller.hpp"

#include <cstdlib>
#include <iostream>
#include <random>
#include <stdexcept>

using namespace rescue;
int main(int argc, char** argv) {
    try {
        const auto count = argc == 2 ? std::stoul(argv[1]) : 10000;
        if (!count || count > 1000000) throw std::invalid_argument("invalid case count");
        std::mt19937 random(0x52524731);
        const std::vector<std::string> seeds = {
            "{}", R"({"profile":"lab","run_dir":"/run","map_name":"lab-path"})",
            R"({"schema":1,"identity":{},"guard":{}})",
            R"({"report":[{"pv":[{"pv_uuid":"abc","vg_uuid":"def"}]}]})",
            "SUBSYSTEM=block\nDEVPATH=/devices/usb/block/sda",
            "0 2048 multipath 3 queue_if_no_path queue_mode bio 0 1 1 round-robin 0 1 1 8:17 1",
            std::string(70, '[') + "0" + std::string(70, ']'),
            "{\"profile\":\"host\",\"profile\":\"lab\"}"
        };
        Events events;
        events.watch({"/sys/devices/usb/block/sda", "/sys/devices/virtual/block/dm-0"});
        for (unsigned long index = 0; index < count; ++index) {
            std::string input = seeds[index % seeds.size()];
            for (unsigned mutation = 0; mutation < random() % 12; ++mutation) {
                const auto at = input.empty() ? 0U : random() % (input.size() + 1);
                switch (random() % 3) {
                case 0: input.insert(at, 1, static_cast<char>(random() % 256)); break;
                case 1: if (at < input.size()) input.erase(at, 1); break;
                default: if (at < input.size()) input[at] = static_cast<char>(random() % 256);
                }
            }
            (void)events.relevant(std::string_view(input.data(), input.size()));
            try {
                const auto json = parse_json_input(input);
                (void)digest(json);
                try { Config config(json); } catch (const std::exception&) {}
                try { validate_record(json); } catch (const std::exception&) {}
            } catch (const std::exception&) {}
            try { (void)table_digest(table_targets(input)); } catch (const std::exception&) {}
            try { (void)parse_blkid_output(input); } catch (const std::exception&) {}
            try { (void)parse_lvm_rows(input, "pv"); } catch (const std::exception&) {}
        }
        std::cout << count << " deterministic hostile input mutations passed\n";
    } catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
}
