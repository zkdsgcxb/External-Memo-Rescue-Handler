#pragma once

#include "core.hpp"
#include <functional>
#include <memory>
#include <mutex>
#include <sys/stat.h>

namespace rescue {

using Runner = std::function<std::string(const std::vector<std::string>&, double)>;
using Clock = std::function<double()>;

// Shared bounded parsers; exposed separately for hostile-output corpus checks.
Json parse_blkid_output(const std::string&);
Json parse_lvm_rows(const std::string&, const std::string& key);

std::string readonly(const std::vector<std::string>& args, double timeout = 3,
                     int owner_fd = -1);

// Discovery reads sysfs only. Content probes are restricted to the one enrolled
// partition, and always run through the owner's fenced command runner.
class Recovery {
public:
    Recovery(Json identity, Runner runner = {}, fs::path sysroot = "/sys",
             fs::path devroot = "/dev");
    Json identity;
    fs::path sys, dev;
    Runner run;
    std::vector<fs::path> candidates() const;
    std::string candidate_node() const;
    std::string verify() const;
    Json admission_layout(const std::string& node) const;
    bool filesystem() const;
};

// This narrow syscall seam permits synthetic race tests without touching real
// block devices. Production always uses the default Linux implementation.
class BlockAccess {
public:
    virtual ~BlockAccess() = default;
    virtual Fd open_readonly(const std::string& node) const;
    virtual struct stat held_stat(int fd) const;
    virtual struct stat node_stat(const std::string& node) const;
    virtual uint64_t number(int fd, unsigned long request) const;
};

class Admission;
class Candidate {
public:
    std::string node() const;
    dev_t dev() const;
    std::string sys_path() const;
    uint64_t diskseq() const;
    uint64_t partition_sectors() const;
    uint64_t logical_block_size() const;
    std::string layout_digest() const;
    double verified_at() const;
    double deadline() const;
    std::string owner_epoch() const;
    Json to_json() const;
    void revalidate(const std::string& epoch, bool check_layout = true);
    int fd() const { return fd_.get(); }

private:
    friend class Admission;
    Candidate(std::shared_ptr<Admission> admission, Fd fd, Json record);
    std::shared_ptr<Admission> admission_;
    Fd fd_;
    Json record_;
};

class Admission : public std::enable_shared_from_this<Admission> {
public:
    Admission(Json config, std::shared_ptr<Recovery> recovery,
              Clock clock = mono, std::string boot_id = {},
              std::shared_ptr<BlockAccess> access = std::make_shared<BlockAccess>());
    std::shared_ptr<Candidate> verify(double deadline, const std::string& epoch);
    void revalidate(Candidate& candidate, const std::string& epoch, bool check_layout = true);

private:
    Json enrollment() const;
    void check_enrollment() const;
    void budget(double deadline) const;
    Json snapshot(const std::string& node, int fd) const;
    void same_instance(const std::string& node, int fd, const Json& expected) const;
    std::string layout(const std::string& node) const;
    Json config_;
    std::shared_ptr<Recovery> recovery_;
    Clock clock_;
    std::string boot_id_, layout_digest_, enrollment_digest_;
    Json layout_version_;
    std::shared_ptr<BlockAccess> access_;
    std::mutex mutex_;
};

// Persistent records carry policy only; cold startup obtains a fresh instance.
void validate_data_config(const Json& config);
void validate_record(const Json& record);
Json record_from_profile(const Json& profile);
Json current_profile(const Json& record, Runner runner = {});
void validate_data_runtime(const Json& config, const std::shared_ptr<Recovery>& recovery);

} // namespace rescue
