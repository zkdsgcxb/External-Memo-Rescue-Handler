#pragma once
#include "controller.hpp"
#include <stdexcept>

namespace rescue {
// Cold, host-data-only control. These helpers never create/remove a DM map.
bool queue_enabled(const Json& snapshot);
Json path_instance(const std::string& node);
Json observed_config(const Config&, const Json& instance);
std::string own_cgroup();
void check_cgroup(const std::string& path, pid_t allowed_pid);
void check_proc_cgroup(const std::string& path, pid_t allowed_pid, const fs::path& proc = "/proc");
void validate_idle_table(const Config&, const Json& instance, const Json& snapshot, bool queue);
Json idle_data_snapshot(const Config&, const Json& instance, DeviceMapper&, bool queue);
Json stopped_receipt(const Json& journal, const Json& identity);
void validate_stopped(const Config&, const Json& identity, const std::string& boot,
                      const Json& journal, const Json& receipt);
// Ordered persistence seams also exercise every crash prefix without DM I/O.
using SaveState = std::function<void(const std::string&, const Json&)>;
using SetQueue = std::function<void(bool)>;
using ReadIdle = std::function<Json(bool)>;
struct StopRefused : std::runtime_error { using std::runtime_error::runtime_error; };
void finish_safe_stop(Journal&, const Json& details, const Json& identity,
                      const SaveState&, const SetQueue&, const ReadIdle&);
void begin_rearm(Journal&, const Json& old_journal, const Json& receipt,
                 const Json& old_invocation, const Json& new_invocation,
                 const SaveState&, const SetQueue&, const ReadIdle&);

class DataControl {
    Fd socket_;
public:
    explicit DataControl(const Config&);
    int fd() const { return socket_.get(); }
    void serve(const std::function<Json(const Json&)>&);
};
Json stop_data(const Json& record);
} // namespace rescue
