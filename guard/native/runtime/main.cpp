#include "data_lifecycle.hpp"
#include <iostream>
#include <stdexcept>

int main(int argc, char** argv) {
    using namespace rescue;
    try {
        if (argc == 2 && std::string(argv[1]) == "--version") {
            std::cout << "guard-runtime 0.0.1-beta (native C++17, DM multipath >=1.15, LP64 Linux)\n";
            return 0;
        }
        if (argc < 2) throw std::invalid_argument("Usage: guard-runtime run|takeover|activate --config PATH; maintain --record PATH [--takeover|--rearm|--first-enable]; safe-stop --record PATH");
        const std::string mode = argv[1];
        bool taking_over = mode == "takeover", rearm = false, first_enable = false;
        fs::path config, record;
        for (int i = 2; i < argc; ++i) {
            const std::string arg = argv[i];
            if (arg == "--takeover" && mode == "maintain") taking_over = true;
            else if (arg == "--first-enable" && mode == "maintain") first_enable = true;
            else if (arg == "--rearm" && mode == "maintain") rearm = true;
            else if ((arg == "--config" || arg == "--record") && i + 1 < argc) {
                auto& path = arg == "--config" ? config : record;
                if (!path.empty()) throw std::invalid_argument("Duplicate input path");
                path = argv[++i];
            } else throw std::invalid_argument("Unknown or incomplete argument: " + arg);
        }
        if (rearm && taking_over) throw std::invalid_argument("Rearm and takeover are mutually exclusive");
        if (mode == "safe-stop" && (record.empty() != config.empty())) {
            const auto value = config.empty() ? load_trusted_json(record) : load_trusted_json(config);
            const auto selected = config.empty() ? value : record_from_profile({{"schema", 1},
                {"guard", value}, {"identity", load_trusted_json(Config(value).identity)}});
            const auto result = stop_data(selected);
            std::cout << result.dump() << '\n';
            return result.value("state", "") == "stopped" ? 0 : result.value("state", "") == "incomplete" ? 2 : 1;
        }
        if (mode == "maintain" && !record.empty() && config.empty()) maintain(load_trusted_json(record), taking_over, rearm, first_enable);
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
