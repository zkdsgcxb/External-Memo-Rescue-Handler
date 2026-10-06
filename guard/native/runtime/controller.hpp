#pragma once

#include "admission.hpp"

namespace rescue {

// One process owns one map. Root and ordinary enrolled devices share this
// configuration and the exact same restoration state machine.
struct Config {
    Json value;
    std::string profile, name, uuid, device;
    fs::path run, identity;
    explicit Config(Json value);
    void validate_environment() const;
    bool lab() const { return profile == "lab"; }
};

std::string table(std::uint64_t sectors, const std::string& node);
Json table_targets(const std::string& text);
Json checked_snapshot(DeviceMapper&, const Config&);
std::string dm(const std::vector<std::string>& args, int fence);
std::unique_ptr<Owner> acquire_owner(const Config&, bool takeover);
void run_owned(const Config&, Owner&, bool takeover = false, bool data_control = false, const Json& rearm_instance = Json(), bool first_enable = false);
void run(const Json& config, bool takeover);
void maintain(const Json& record, bool takeover, bool rearm = false, bool first_enable = false);
Json activate(const Json& enrollment);

} // namespace rescue
