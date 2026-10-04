#include "controller.hpp"
#include <iostream>
#include <stdexcept>

int main(int argc, char** argv) {
    using namespace rescue;
    try {
        if (argc == 2 && std::string(argv[1]) == "--version") {
            std::cout << "guard-runtime 1 (native C++17, DM multipath >=1.15, LP64 Linux)\n";
            return 0;
        }
        if (argc < 2) throw std::invalid_argument("Usage: guard-runtime run|takeover|activate --config PATH; maintain --record PATH [--takeover]");
        const std::string mode = argv[1];
        bool taking_over = mode == "takeover";
        fs::path config, record;
        for (int i = 2; i < argc; ++i) {
            const std::string arg = argv[i];
            if (arg == "--takeover" && mode == "maintain") taking_over = true;
            else if ((arg == "--config" || arg == "--record") && i + 1 < argc) {
                auto& path = arg == "--config" ? config : record;
                if (!path.empty()) throw std::invalid_argument("Duplicate input path");
                path = argv[++i];
            } else throw std::invalid_argument("Unknown or incomplete argument: " + arg);
        }
        if (mode == "maintain" && !record.empty() && config.empty()) maintain(load_trusted_json(record), taking_over);
        else if (!config.empty() && record.empty() && (mode == "run" || mode == "takeover")) run(load_trusted_json(config), taking_over);
        else if (mode == "activate" && !config.empty() && record.empty()) {
            activate(load_trusted_json(config));
            std::cout << "RAM rescue stable root mapping prepared\n";
        } else throw std::invalid_argument("Mode and input path do not match");
        return 0;
    } catch (const std::exception& error) {
        std::cerr << Json{{"state", "blocked"}, {"reason", error.what()}, {"outcome", "native_entry_refused"}}.dump() << '\n';
        return 1;
    }
}
