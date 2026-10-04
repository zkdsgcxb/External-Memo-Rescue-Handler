#include "core.hpp"

#include <algorithm>
#include <cerrno>
#include <charconv>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstring>
#include <dlfcn.h>
#include <fcntl.h>
#include <limits>
#include <linux/netlink.h>
#include <mutex>
#include <openssl/evp.h>
#include <poll.h>
#include <signal.h>
#include <set>
#include <spawn.h>
#include <sstream>
#include <stdexcept>
#include <system_error>
#include <sys/eventfd.h>
#include <sys/file.h>
#include <sys/ioctl.h>
#include <sys/random.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <thread>
#include <unistd.h>

namespace rescue {
namespace {
[[noreturn]] void system_failure(const std::string& action, int error = errno) {
    throw std::system_error(error, std::generic_category(), action);
}

void check_storage(int fd, bool directory, uid_t owner) {
    struct stat info{};
    if (::fstat(fd, &info)) system_failure("stat trusted storage");
    if (!storage_metadata_trusted(info, directory, owner))
        throw std::runtime_error("Storage must be owned by its trusted UID, not writable by group/other, and not aliased");
}

std::string bounded_read(int fd, std::size_t limit) {
    std::string output;
    std::array<char, 4096> buffer{};
    for (;;) {
        const auto count = ::read(fd, buffer.data(), buffer.size());
        if (count < 0) {
            if (errno == EINTR) continue;
            system_failure("read bounded input");
        }
        if (!count) return output;
        if (static_cast<std::size_t>(count) > limit - output.size())
            throw std::runtime_error("Input exceeds bounded read limit");
        output.append(buffer.data(), static_cast<std::size_t>(count));
    }
}

void check_existing(int directory, const std::string& name) {
    struct stat info{};
    if (::fstatat(directory, name.c_str(), &info, AT_SYMLINK_NOFOLLOW)) {
        if (errno == ENOENT) return;
        system_failure("stat state destination");
    }
    if (!S_ISREG(info.st_mode) || info.st_uid != ::geteuid() ||
            (info.st_mode & 0022) || info.st_nlink != 1)
        throw std::runtime_error("State destination is not a trusted regular file");
}

Fd state_directory(const fs::path& path) {
    Fd directory(::open(path.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
    if (!directory) system_failure("open state directory");
    check_storage(directory.get(), true, ::geteuid());
    return directory;
}

void write_all(int fd, std::string_view text) {
    while (!text.empty()) {
        const auto count = ::write(fd, text.data(), text.size());
        if (count < 0) {
            if (errno == EINTR) continue;
            system_failure("write");
        }
        if (!count) throw std::runtime_error("write made no progress");
        text.remove_prefix(static_cast<std::size_t>(count));
    }
}

std::string epoch() {
    std::array<unsigned char, 16> bytes{};
    std::size_t have = 0;
    while (have < bytes.size()) {
        const auto count = ::getrandom(bytes.data() + have, bytes.size() - have, 0);
        if (count < 0) {
            if (errno == EINTR) continue;
            system_failure("getrandom");
        }
        if (!count) throw std::runtime_error("getrandom made no progress");
        have += static_cast<std::size_t>(count);
    }
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    constexpr char hex[] = "0123456789abcdef";
    std::string output;
    for (const auto byte : bytes) {
        output += hex[byte >> 4];
        output += hex[byte & 15];
    }
    return output;
}

// JSON's number grammar does not fix float formatting. Existing Python
// journals hash repr(double), including its -4/16 exponent thresholds and
// trailing .0. Use shortest-roundtrip digits, then apply that same notation.
std::string python_float(double value) {
    if (!std::isfinite(value)) throw std::invalid_argument("Non-finite JSON number");
    if (value == 0) return std::signbit(value) ? "-0.0" : "0.0";
    std::array<char, 64> buffer{};
    const auto result = std::to_chars(buffer.data(), buffer.data() + buffer.size(),
                                      std::abs(value), std::chars_format::general);
    if (result.ec != std::errc{}) throw std::runtime_error("Cannot format JSON float");
    std::string digits(buffer.data(), result.ptr);
    int exponent = 0;
    const auto marker = digits.find_first_of("eE");
    if (marker != std::string::npos) {
        exponent = std::stoi(digits.substr(marker + 1));
        digits.resize(marker);
    }
    const auto decimal = digits.find('.');
    int point = static_cast<int>(decimal == std::string::npos ? digits.size() : decimal) + exponent;
    if (decimal != std::string::npos) digits.erase(decimal, 1);
    while (digits.size() > 1 && digits.front() == '0') { digits.erase(0, 1); --point; }
    while (digits.size() > 1 && digits.back() == '0') digits.pop_back();
    exponent = point - 1;
    std::string output = std::signbit(value) ? "-" : "";
    if (exponent < -4 || exponent >= 16) {
        output += digits.front();
        if (digits.size() > 1) output += '.' + digits.substr(1);
        output += exponent < 0 ? "e-" : "e+";
        const auto number = std::to_string(std::abs(exponent));
        if (number.size() == 1) output += '0';
        output += number;
    } else if (point <= 0) {
        output += "0." + std::string(static_cast<std::size_t>(-point), '0') + digits;
    } else if (point >= static_cast<int>(digits.size())) {
        output += digits + std::string(static_cast<std::size_t>(point) - digits.size(), '0') + ".0";
    } else {
        output += digits.substr(0, static_cast<std::size_t>(point)) + '.' +
                  digits.substr(static_cast<std::size_t>(point));
    }
    return output;
}

void canonical_append(std::string& output, const Json& value) {
    if (value.is_object()) {
        output += '{';
        bool first = true;
        for (auto item = value.begin(); item != value.end(); ++item) {
            if (!first) output += ',';
            first = false;
            output += Json(item.key()).dump(-1, ' ', true);
            output += ':';
            canonical_append(output, item.value());
        }
        output += '}';
    } else if (value.is_array()) {
        output += '[';
        bool first = true;
        for (const auto& item : value) {
            if (!first) output += ',';
            first = false;
            canonical_append(output, item);
        }
        output += ']';
    } else if (value.is_number_float()) {
        output += python_float(value.get<double>());
    } else {
        output += value.dump(-1, ' ', true);
    }
}

Json exception_record() noexcept {
    try {
        try { throw; }
        catch (const std::exception& error) {
            // C++ exposes no portable traceback. Preserve a bounded message
            // rather than reading source files during a storage failure.
            return {{"type", "RuntimeError"}, {"message", std::string(error.what()).substr(0, 3072)},
                    {"traceback", ""}};
        }
        catch (...) { return {{"type", "UnknownException"}, {"message", "Non-standard exception"},
                             {"traceback", ""}}; }
    } catch (...) { return Json(); }
}

int milliseconds(double seconds) {
    if (!(seconds > 0)) return 0;
    return static_cast<int>(std::min(std::ceil(seconds * 1000),
                                    static_cast<double>(std::numeric_limits<int>::max())));
}
}  // namespace

Fd::~Fd() { reset(); }
Fd& Fd::operator=(Fd&& other) noexcept {
    if (this != &other) reset(other.release());
    return *this;
}
int Fd::release() noexcept { const int value = value_; value_ = -1; return value; }
void Fd::reset(int value) noexcept {
    if (value_ >= 0) ::close(value_);  // Never retry close: Linux may already have reused the number.
    value_ = value;
}

double mono() {
    timespec value{};
    if (::clock_gettime(CLOCK_MONOTONIC, &value)) system_failure("clock_gettime");
    return static_cast<double>(value.tv_sec) + static_cast<double>(value.tv_nsec) * 1e-9;
}

std::string read_text(const fs::path& path, std::size_t limit) {
    Fd fd(::open(path.c_str(), O_RDONLY | O_CLOEXEC));
    if (!fd) system_failure("open " + path.string());
    return bounded_read(fd.get(), limit);
}

std::string trim(std::string value) {
    constexpr auto spaces = " \t\r\n\f\v";
    const auto first = value.find_first_not_of(spaces);
    if (first == std::string::npos) return {};
    return value.substr(first, value.find_last_not_of(spaces) - first + 1);
}

std::vector<std::string> split_words(std::string_view value) {
    std::istringstream input{std::string(value)};
    std::vector<std::string> words;
    for (std::string word; input >> word;) words.push_back(std::move(word));
    return words;
}

std::string canonical_json(const Json& value) {
    std::string output;
    canonical_append(output, value);
    return output;
}

std::string digest(const Json& value) {
    const auto text = canonical_json(value);
    std::array<unsigned char, EVP_MAX_MD_SIZE> bytes{};
    unsigned length = 0;
    if (!EVP_Digest(text.data(), text.size(), bytes.data(), &length, EVP_sha256(), nullptr))
        throw std::runtime_error("SHA256 failed");
    constexpr char hex[] = "0123456789abcdef";
    std::string output;
    for (unsigned i = 0; i < length; ++i) {
        output += hex[bytes[i] >> 4];
        output += hex[bytes[i] & 15];
    }
    return output;
}

Json parse_json_input(std::string_view text, std::size_t limit) {
    if (text.size() > limit) throw std::invalid_argument("JSON exceeds bounded input limit");
    // Reject nested/duplicate keys before policy lookup or recursive hashing.
    std::vector<std::set<std::string>> keys;
    return Json::parse(text, [&keys](int depth, Json::parse_event_t event, Json& value) {
        if (depth > 64 || (depth >= 64 && (event == Json::parse_event_t::object_start ||
                event == Json::parse_event_t::array_start)))
            throw std::invalid_argument("JSON nesting exceeds 64 levels");
        if (event == Json::parse_event_t::object_start) keys.emplace_back();
        else if (event == Json::parse_event_t::object_end) keys.pop_back();
        else if (event == Json::parse_event_t::key && !keys.back().insert(value.get<std::string>()).second)
            throw std::invalid_argument("Duplicate JSON key");
        return true;
    });
}

Json load_json(const fs::path& path) { return parse_json_input(read_text(path)); }

bool storage_metadata_trusted(const struct stat& info, bool directory, uid_t owner) {
    return (directory ? S_ISDIR(info.st_mode) : S_ISREG(info.st_mode)) &&
        info.st_uid == owner && !(info.st_mode & 0022) &&
        (directory || info.st_nlink == 1);
}

Fd trusted_directory(const fs::path& path, bool create) {
    if (!path.is_absolute() || path.native().find('\0') != std::string::npos)
        throw std::runtime_error("Trusted storage path must be absolute");
    Fd current(::open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC));
    if (!current) system_failure("open root directory");
    check_storage(current.get(), true, 0);
    for (const auto& part : path.relative_path()) {
        if (part.empty() || part == "." || part == "..")
            throw std::runtime_error("Trusted storage path must not contain dot components");
        if (create && ::mkdirat(current.get(), part.c_str(), 0700) && errno != EEXIST)
            system_failure("create trusted directory");
        Fd next(::openat(current.get(), part.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
        if (!next) system_failure("open trusted directory component");
        check_storage(next.get(), true, 0);
        current = std::move(next);
    }
    return current;
}

Json load_trusted_json(const fs::path& path) {
    if (path.native().find('\0') != std::string::npos || path.filename().empty() ||
            path.filename() == "." || path.filename() == "..")
        throw std::runtime_error("Trusted input needs a filename");
    auto parent = trusted_directory(path.parent_path());
    Fd input(::openat(parent.get(), path.filename().c_str(), O_RDONLY | O_NONBLOCK | O_CLOEXEC | O_NOFOLLOW));
    if (!input) system_failure("open trusted JSON input");
    check_storage(input.get(), false, 0);
    return parse_json_input(bounded_read(input.get(), log_limit));
}

void atomic_json(const fs::path& path, const Json& value) {
    const auto text = canonical_json(value);
    if (text.size() > log_limit) throw std::invalid_argument("RAM evidence record exceeds limit");
    auto directory = state_directory(path.parent_path());
    const auto name = path.filename().string();
    check_existing(directory.get(), name);
    // Unique O_EXCL temporaries avoid following/truncating a planted file, even
    // if a previous owner died before publishing its record.
    const auto temporary = name + ".tmp-" + epoch();
    Fd output(::openat(directory.get(), temporary.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600));
    if (!output) system_failure("create state temporary");
    try {
        write_all(output.get(), text);
        if (::renameat(directory.get(), temporary.c_str(), directory.get(), name.c_str()))
            system_failure("rename RAM evidence");
    } catch (...) { ::unlinkat(directory.get(), temporary.c_str(), 0); throw; }
}

std::string table_digest(const Json& targets) {
    Json normalized = Json::array();
    if (!targets.is_array()) throw std::invalid_argument("Expected target list");
    for (const auto& target : targets) {
        if (!target.is_array() || target.size() != 4)
            throw std::invalid_argument("Expected four target fields");
        auto words = split_words(target.at(3).get<std::string>());
        if (target.at(2) == "multipath") {
            if (words.empty()) throw std::invalid_argument("Empty multipath table");
            std::size_t consumed = 0;
            const auto count = std::stoul(words.front(), &consumed);
            if (consumed != words.front().size() || count >= words.size() - 1)
                throw std::invalid_argument("Invalid multipath feature count");
            std::vector<std::string> features(words.begin() + 1, words.begin() + 1 + count);
            features.erase(std::remove(features.begin(), features.end(), "queue_if_no_path"), features.end());
            std::vector<std::string> tail(words.begin() + 1 + count, words.end());
            consumed = 0;
            const auto handlers = std::stoul(tail.front(), &consumed);
            if (consumed != tail.front().size() || handlers >= tail.size() ||
                tail.size() - handlers < 3 || tail.at(handlers + 1) != "1" ||
                (tail.at(handlers + 2) != "0" && tail.at(handlers + 2) != "1"))
                throw std::runtime_error("Unexpected multipath topology");
            tail.at(handlers + 2) = "1";
            words = {std::to_string(features.size())};
            words.insert(words.end(), features.begin(), features.end());
            words.insert(words.end(), tail.begin(), tail.end());
        }
        std::string parameters;
        for (const auto& word : words) {
            if (!parameters.empty()) parameters += ' ';
            parameters += word;
        }
        normalized.push_back(Json::array({target.at(0), target.at(1), target.at(2), parameters}));
    }
    return digest(normalized);
}

Json describe(Json snapshot) {
    snapshot["active_digest"] = table_digest(snapshot.at("active"));
    snapshot["inactive_digest"] = snapshot.at("inactive").empty() ? Json() :
                                      Json(table_digest(snapshot.at("inactive")));
    return snapshot;
}

Owner::Owner(fs::path run_dir) : run(std::move(run_dir)),
    fd(::open((run / "path-owner.lock").c_str(), O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, 0600)) {
    if (!fd) system_failure("open owner lock");
    check_storage(fd.get(), false, ::geteuid());
    if (::flock(fd.get(), LOCK_EX | LOCK_NB)) system_failure("acquire owner lock");
    epoch = rescue::epoch();
    boot_id = trim(read_text("/proc/sys/kernel/random/boot_id"));
}

Journal::Journal(const Owner& owner, const std::string& name, const std::string& map_uuid)
    : path(owner.run / "path-transaction.json"), record({
        {"schema", 1}, {"owner_epoch", owner.epoch}, {"boot_id", owner.boot_id},
        {"owner_pid", ::getpid()}, {"map_name", name}, {"map_uuid", map_uuid},
        {"phase", "starting"}, {"deadline", nullptr}, {"candidate", nullptr}}) {}

void Journal::write(const std::string& phase, const Json& details) {
    Json updated = record;
    updated.update(details);
    updated["phase"] = phase;
    updated["updated_at"] = mono();
    atomic_json(path, updated);
    record = std::move(updated);
}

void Evidence::event(const Json& entry) {
    const auto text = canonical_json(entry) + '\n';
    if (text.size() > log_limit) throw std::invalid_argument("Event exceeds RAM evidence limit");
        auto directory = state_directory(run);
    check_existing(directory.get(), "path-events.jsonl");
    check_existing(directory.get(), "path-events.previous.jsonl");
    struct stat info{};
    if (!::fstatat(directory.get(), "path-events.jsonl", &info, AT_SYMLINK_NOFOLLOW) &&
            static_cast<std::uint64_t>(info.st_size) + text.size() > log_limit &&
            ::renameat(directory.get(), "path-events.jsonl", directory.get(), "path-events.previous.jsonl"))
        system_failure("rotate evidence log");
    Fd output(::openat(directory.get(), "path-events.jsonl", O_WRONLY | O_APPEND | O_CREAT | O_CLOEXEC | O_NOFOLLOW, 0600));
    if (!output) system_failure("open evidence log");
    check_storage(output.get(), false, ::geteuid());
    write_all(output.get(), text);
    atomic_json(run / "path-state.json", entry);
}

Json Observations::sample(const Json& info, bool failed_path) {
    const auto now = mono();
    try {
        const auto path = fs::path("/sys/dev/block") /
            (std::to_string(info.at("major").get<unsigned>()) + ':' +
             std::to_string(info.at("minor").get<unsigned>())) / "stat";
        const auto words = split_words(read_text(path));
        if (words.size() <= 8) throw std::invalid_argument("Short block stat");
        std::vector<std::uint64_t> fields;
        for (const auto& word : words) {
            std::size_t end = 0;
            const auto number = std::stoull(word, &end);
            if (end != word.size()) throw std::invalid_argument("Invalid block stat");
            fields.push_back(number);
        }
        std::uint64_t completed = 0;
        for (const auto index : {0U, 4U, 11U, 15U}) if (index < fields.size()) completed += fields[index];
        const auto in_flight = fields[8];
        const bool progressed = previous && completed != *previous;
        if (progressed || !in_flight) last_progress = now;
        const std::string state = failed_path ? "explicit_failure" : progressed ? "completion_progress" :
            !in_flight ? "no_io" : now - last_progress >= 2 ? "suspected_stall" : "in_flight";
        previous = completed;
        transport = {{"state", state}, {"completed", completed}, {"in_flight", in_flight},
                     {"without_completion_seconds", now - last_progress}};
    } catch (const std::exception&) {
        transport = {{"state", failed_path ? "explicit_failure" : "unknown"}};
    }
    return transport;
}

void fault_hook(const std::string& stage, const Journal& journal, bool enabled) {
    const auto run = journal.path.parent_path();
    if (!enabled || !fs::exists(run / "lab-fault-config.json")) return;
    const auto setting = load_trusted_json(run / "lab-fault-config.json");
    if (setting.value("stage", "") != stage || setting.value("action", "") != "pause") return;
    const auto token = setting.value("token", Json());
    atomic_json(run / "lab-fault-reached.json", {{"stage", stage}, {"token", token},
        {"pid", ::getpid()}, {"monotonic", mono()}, {"transaction", journal.record}});
    for (;;) {
        if (fs::exists(run / "lab-fault-release.json") &&
            load_trusted_json(run / "lab-fault-release.json").value("token", Json()) == token) return;
        if (!journal.record.at("deadline").is_null() &&
            mono() >= journal.record.at("deadline").get<double>()) return;
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
}

namespace {
// Public libdevmapper ABI, independently compared with native C headers in
// lab/architecture_probe.py. dlsym avoids a build-time libdevmapper-dev package.
struct DMInfo {
    int exists, suspended, live_table, inactive_table;
    std::int32_t open_count;
    std::uint32_t event_nr, major, minor;
    int read_only;
    std::int32_t target_count;
    int deferred_remove, internal_suspend;
};
struct DMVersion { std::uint32_t next, version[3]; };
static_assert(sizeof(DMInfo) == 48 && alignof(DMInfo) == 4);
static_assert(sizeof(DMVersion) == 16);
static_assert(sizeof(void*) == 8 && sizeof(std::size_t) == 8 && sizeof(int) == 4);
#if !defined(__linux__) || (!defined(__x86_64__) && !defined(__aarch64__) && !defined(__riscv))
#error "Guard requires a validated Linux x86_64, aarch64, or riscv64 ABI"
#endif
#if __BYTE_ORDER__ != __ORDER_LITTLE_ENDIAN__
#error "Guard requires little-endian ABI"
#endif
constexpr unsigned long dm_mpath_probe_paths = _IO(0xfd, 18);
}

struct DeviceMapper::Impl {
    void* library = nullptr;
    void* (*create)(int) = nullptr;
    void (*destroy)(void*) = nullptr;
    int (*name)(void*, const char*) = nullptr;
    int (*run)(void*) = nullptr;
    const char* (*uuid)(void*) = nullptr;
    int (*info)(void*, DMInfo*) = nullptr;
    int (*inactive)(void*) = nullptr;
    void* (*next)(void*, void*, std::uint64_t*, std::uint64_t*, char**, char**) = nullptr;
    void* (*versions)(void*) = nullptr;
    template<class T> T symbol(const char* name) {
        void* value = ::dlsym(library, name);
        if (!value) throw std::runtime_error(std::string("Missing libdevmapper symbol: ") + name);
        return reinterpret_cast<T>(value);
    }
    Impl() {
        library = ::dlopen("libdevmapper.so.1.02.1", RTLD_NOW | RTLD_LOCAL);
        if (!library) throw std::runtime_error(::dlerror());
        try {
            create = symbol<decltype(create)>("dm_task_create");
            destroy = symbol<decltype(destroy)>("dm_task_destroy");
            name = symbol<decltype(name)>("dm_task_set_name");
            run = symbol<decltype(run)>("dm_task_run");
            uuid = symbol<decltype(uuid)>("dm_task_get_uuid");
            info = symbol<decltype(info)>("dm_task_get_info");
            inactive = symbol<decltype(inactive)>("dm_task_query_inactive_table");
            next = symbol<decltype(next)>("dm_get_next_target");
            versions = symbol<decltype(versions)>("dm_task_get_versions");
        } catch (...) { ::dlclose(library); throw; }
    }
    ~Impl() { ::dlclose(library); }
    std::unique_ptr<void, void (*)(void*)> task(int operation) {
        auto* pointer = create(operation);
        if (!pointer) throw std::runtime_error("Cannot allocate DM task");
        return {pointer, destroy};
    }
};

DeviceMapper::DeviceMapper() : impl_(std::make_unique<Impl>()) {}
DeviceMapper::~DeviceMapper() = default;

std::array<unsigned, 3> DeviceMapper::target_version(const std::string& name) {
    auto task = impl_->task(16);
    if (!impl_->run(task.get())) throw std::runtime_error("Cannot query kernel DM target versions");
    auto* cursor = static_cast<char*>(impl_->versions(task.get()));
    while (cursor) {
        const auto* entry = reinterpret_cast<const DMVersion*>(cursor);
        if (std::string_view(cursor + sizeof(DMVersion)) == name)
            return {entry->version[0], entry->version[1], entry->version[2]};
        if (!entry->next) break;
        cursor += entry->next;
    }
    throw std::runtime_error("Kernel DM target not loaded: " + name);
}

Json DeviceMapper::read(const std::string& name, int operation, bool inactive) {
    auto task = impl_->task(operation);
    if (!impl_->name(task.get(), name.c_str())) throw std::runtime_error("Cannot name DM task");
    if (inactive && !impl_->inactive(task.get())) throw std::runtime_error("Cannot select inactive DM table");
    if (!impl_->run(task.get())) throw std::runtime_error("Cannot query DM map " + name);
    DMInfo info{};
    if (!impl_->info(task.get(), &info) || !info.exists)
        throw std::runtime_error("DM map does not exist: " + name);
    const char* uuid = impl_->uuid(task.get());
    Json targets = Json::array();
    void* cursor = nullptr;
    do {
        std::uint64_t start = 0, size = 0;
        char *kind = nullptr, *parameters = nullptr;
        cursor = impl_->next(task.get(), cursor, &start, &size, &kind, &parameters);
        if (kind) targets.push_back(Json::array({start, size, kind, parameters ? parameters : ""}));
    } while (cursor);
    return {{"uuid", uuid ? uuid : ""}, {"targets", targets}, {"info", {
        {"exists", info.exists}, {"suspended", info.suspended}, {"live_table", info.live_table},
        {"inactive_table", info.inactive_table}, {"open_count", info.open_count}, {"event_nr", info.event_nr},
        {"major", info.major}, {"minor", info.minor}, {"read_only", info.read_only},
        {"target_count", info.target_count}, {"deferred_remove", info.deferred_remove},
        {"internal_suspend", info.internal_suspend}}}};
}

std::pair<std::string, Json> DeviceMapper::query(const std::string& name) {
    const auto result = read(name, 10);
    last_info = result.at("info");
    Json targets = Json::array();
    for (const auto& target : result.at("targets")) targets.push_back(Json::array({target.at(2), target.at(3)}));
    return {result.at("uuid").get<std::string>(), std::move(targets)};
}

Json DeviceMapper::snapshot(const std::string& name) {
    const auto active = read(name, 11);
    const auto inactive = read(name, 11, true);
    if (active.at("uuid") != inactive.at("uuid"))
        throw std::runtime_error("DM identity changed while reading tables");
    return {{"uuid", active.at("uuid")}, {"info", active.at("info")},
            {"active", active.at("targets")}, {"inactive", inactive.at("targets")}};
}

Json probe_paths(const std::string& device, std::uint64_t token) {
    const auto started = mono();
    int error = 0;
    Fd fd(::open(device.c_str(), O_RDONLY | O_NONBLOCK | O_CLOEXEC));
    if (!fd || ::ioctl(fd.get(), dm_mpath_probe_paths)) error = errno;
    return {{"token", token}, {"errno", error}, {"elapsed", mono() - started},
            {"status", !error ? "completed" : error == ENOTCONN ? "no_paths" : "error"}, {"source", "ioctl"}};
}

Events::Events() : socket_(::socket(AF_NETLINK, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC,
                                   NETLINK_KOBJECT_UEVENT)) {
    if (!socket_) system_failure("netlink socket");
    int bytes = 256 * 1024;
    sockaddr_nl address{};
    address.nl_family = AF_NETLINK;
    address.nl_groups = 1;
    if (::setsockopt(socket_.get(), SOL_SOCKET, SO_RCVBUF, &bytes, sizeof(bytes)) ||
        ::bind(socket_.get(), reinterpret_cast<sockaddr*>(&address), sizeof(address)))
        system_failure("netlink setup");
}

void Events::watch(const std::vector<std::string>& paths) {
    paths_.clear();
    for (const auto& path : paths) paths_.push_back(path.rfind("/sys", 0) == 0 ? path.substr(4) : path);
}

bool Events::relevant(std::string_view data) const {
    bool block = false;
    std::optional<std::string_view> path;
    while (!data.empty()) {
        const auto end = data.find('\0');
        const auto field = data.substr(0, end);
        if (field == "SUBSYSTEM=block") block = true;
        if (!path && field.substr(0, 8) == "DEVPATH=") path = field.substr(8);
        if (end == std::string_view::npos) break;
        data.remove_prefix(end + 1);
    }
    if (!block) return false;
    if (paths_.empty() || !path) return true;
    for (const auto& prefix : paths_)
        if (*path == prefix || (path->size() > prefix.size() &&
                               path->substr(0, prefix.size()) == prefix && (*path)[prefix.size()] == '/')) return true;
    return false;
}

bool Events::wait(double seconds, int completion_fd, bool defer_events) {
    std::array<pollfd, 2> descriptors{};
    nfds_t count = 0;
    if (!defer_events) descriptors[count++] = {socket_.get(), POLLIN, 0};
    if (completion_fd >= 0) descriptors[count++] = {completion_fd, POLLIN, 0};
    operation_ready = false;
    const int ready = ::poll(descriptors.data(), count, milliseconds(seconds));
    if (ready < 0) {
        if (errno == EINTR) return false;
        system_failure("netlink poll");
    }
    if (!ready) return false;
    bool socket_ready = false;
    for (nfds_t index = 0; index < count; ++index) {
        if (descriptors[index].revents & POLLNVAL) throw std::runtime_error("Invalid event descriptor");
        if (!descriptors[index].revents) continue;
        if (descriptors[index].fd == completion_fd) operation_ready = true;
        if (descriptors[index].fd == socket_.get()) socket_ready = true;
    }
    if (!socket_ready) return false;
    bool relevant_event = false;
    for (int batch = 0; batch < 64; ++batch) {
        std::array<char, 16384> data{};
        sockaddr_nl peer{};
        socklen_t length = sizeof(peer);
        const auto size = ::recvfrom(socket_.get(), data.data(), data.size(), 0,
                                     reinterpret_cast<sockaddr*>(&peer), &length);
        if (size < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK) break;
            if (errno == ENOBUFS) return true;
            if (errno == EINTR) continue;
            system_failure("netlink receive");
        }
        if (peer.nl_pid == 0 && relevant(std::string_view(data.data(), static_cast<std::size_t>(size))))
            relevant_event = true;
    }
    if (!relevant_event && !operation_ready)
        std::this_thread::sleep_for(std::chrono::duration<double>(std::clamp(seconds, 0.0, 0.05)));
    return relevant_event;
}

bool Schedule::due(double now, bool event) const {
    return now >= next_check || (event && now >= event_after);
}
void Schedule::completed(double now, bool recovering) {
    next_check = now + (recovering ? delay : 1.0);
    event_after = recovering ? next_check : now + 0.1;
    delay = recovering ? std::min(0.8, delay * 2) : 0.1;
}

struct OwnedOperation::State {
    struct Pending {
        std::string kind;
        std::uint64_t token;
        Fd fence;
        std::optional<Outcome> outcome;
        Cleanup cleanup;
        bool released = false, abandoned = false;
        std::thread::id worker;
        std::condition_variable release;
    };
    int owner_fd;
    mutable std::mutex mutex;
    std::shared_ptr<Pending> pending;
    std::uint64_t token = 0;
    bool abandoned = false;
    Json cleanup_error;
    Fd notification;
    explicit State(int fd) : owner_fd(fd), notification(::eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC)) {
        if (!notification) system_failure("eventfd");
    }
};

OwnedOperation::OwnedOperation(int owner_fd) : state_(std::make_shared<State>(owner_fd)) {}
OwnedOperation::~OwnedOperation() {
    // Destruction is the last-resort ownership transfer: discarding std::any
    // releases RAII candidate references, after any running worker returns.
    abandon([](const Outcome&) {});
}

int OwnedOperation::fileno() const {
    std::lock_guard<std::mutex> lock(state_->mutex);
    if (!state_->notification) throw std::runtime_error("Operation notification fd is closed");
    return state_->notification.get();
}
int OwnedOperation::fence_fd() const {
    std::lock_guard<std::mutex> lock(state_->mutex);
    const auto& pending = state_->pending;
    if (!pending || !pending->fence || pending->worker != std::this_thread::get_id())
        throw std::runtime_error("Only the current operation worker owns its fence fd");
    return pending->fence.get();
}
bool OwnedOperation::busy() const {
    std::lock_guard<std::mutex> lock(state_->mutex);
    return static_cast<bool>(state_->pending);
}
Json OwnedOperation::cleanup_error() const {
    std::lock_guard<std::mutex> lock(state_->mutex);
    return state_->cleanup_error;
}

std::uint64_t OwnedOperation::start(const std::string& kind, std::function<std::any()> fn) {
    if (kind.empty() || !fn) throw std::invalid_argument("An operation requires a kind and callable");
    std::unique_lock<std::mutex> lock(state_->mutex);
    if (state_->abandoned) throw std::runtime_error("Abandoned operation executor cannot restart");
    if (state_->pending) throw std::runtime_error("Previous operation has not been consumed");
    auto pending = std::make_shared<State::Pending>();
    pending->kind = kind;
    pending->token = ++state_->token;
    pending->fence.reset(::fcntl(state_->owner_fd, F_DUPFD_CLOEXEC, 3));
    if (!pending->fence) system_failure("duplicate operation fence");
    state_->pending = pending;
    auto state = state_;
    try {
        std::thread([state, pending, fn = std::move(fn)]() mutable {
            {
                std::lock_guard<std::mutex> lock(state->mutex);
                pending->worker = std::this_thread::get_id();
            }
            Outcome outcome;
            outcome.kind = pending->kind;
            outcome.token = pending->token;
            const auto started = mono();
            try { outcome.value = fn(); }
            catch (...) { outcome.error = exception_record(); }
            outcome.elapsed = mono() - started;
            fn = {};  // May own the controller: release it before waiting for result ownership.
            std::unique_lock<std::mutex> lock(state->mutex);
            pending->outcome = std::move(outcome);
            if (::eventfd_write(state->notification.get(), 1) && errno != EAGAIN) {
                // The periodic controller check remains a completion fallback.
            }
            pending->release.wait(lock, [&] { return pending->released; });
            if (!pending->abandoned) return;
            auto cleanup = std::move(pending->cleanup);
            lock.unlock();
            try { cleanup(*pending->outcome); }
            catch (...) {
                const auto error = exception_record();
                std::lock_guard<std::mutex> error_lock(state->mutex);
                state->cleanup_error = error;
            }
            // Destruct the value and callback before dropping the last fence.
            pending->outcome.reset();
            cleanup = {};
            lock.lock();
            pending->fence.reset();
            if (state->pending == pending) state->pending.reset();
            state->notification.reset();
        }).detach();
    } catch (...) {
        state_->pending.reset();
        throw;
    }
    return pending->token;
}

std::optional<Outcome> OwnedOperation::poll() {
    std::lock_guard<std::mutex> lock(state_->mutex);
    auto pending = state_->pending;
    if (state_->abandoned || !pending || !pending->outcome) return std::nullopt;
    eventfd_t count;
    if (::eventfd_read(state_->notification.get(), &count) && errno != EAGAIN)
        system_failure("read operation notification");
    auto outcome = std::move(pending->outcome);
    pending->outcome.reset();
    pending->fence.reset();
    state_->pending.reset();
    pending->released = true;
    pending->release.notify_one();
    return outcome;
}

void OwnedOperation::abandon(Cleanup cleanup) {
    if (!cleanup) throw std::invalid_argument("Abandon requires a cleanup callable");
    std::lock_guard<std::mutex> lock(state_->mutex);
    if (state_->abandoned) return;
    state_->abandoned = true;
    if (state_->pending) {
        state_->pending->abandoned = true;
        state_->pending->cleanup = std::move(cleanup);
        state_->pending->released = true;
        state_->pending->release.notify_one();
    } else state_->notification.reset();
}

void OwnedOperation::close(Cleanup cleanup) {
    {
        std::lock_guard<std::mutex> lock(state_->mutex);
        if (state_->abandoned) return;
        if (state_->pending && !cleanup)
            throw std::runtime_error("Pending operation requires an explicit cleanup callback");
    }
    abandon(cleanup ? std::move(cleanup) : Cleanup([](const Outcome&) {}));
}

std::string command(const std::vector<std::string>& args, double timeout, int owner_fd) {
    if (args.empty() || args.front().empty() || !std::isfinite(timeout) || timeout <= 0)
        throw std::invalid_argument("Command requires arguments and a positive finite timeout");
    for (const auto& arg : args) if (arg.find('\0') != std::string::npos)
        throw std::invalid_argument("NUL in command argument");
    if (owner_fd >= 0 && owner_fd < 3) throw std::invalid_argument("Fence must not use a standard stream");
    std::array<int, 2> out_pipe{}, err_pipe{};
    if (::pipe2(out_pipe.data(), O_CLOEXEC)) system_failure("stdout pipe");
    Fd out_read(out_pipe[0]), out_write(out_pipe[1]);
    if (::pipe2(err_pipe.data(), O_CLOEXEC)) system_failure("stderr pipe");
    Fd err_read(err_pipe[0]), err_write(err_pipe[1]);
    for (const auto fd : {out_read.get(), err_read.get()})
        if (::fcntl(fd, F_SETFL, O_NONBLOCK)) system_failure("nonblocking command pipe");
    struct Actions {
        posix_spawn_file_actions_t value;
        Actions() { const int result = ::posix_spawn_file_actions_init(&value); if (result) system_failure("spawn actions", result); }
        ~Actions() { ::posix_spawn_file_actions_destroy(&value); }
    } actions;
    const auto checked = [](int result) { if (result) system_failure("spawn file action", result); };
    checked(::posix_spawn_file_actions_addopen(&actions.value, 0, "/dev/null", O_RDONLY, 0));
    checked(::posix_spawn_file_actions_adddup2(&actions.value, out_write.get(), 1));
    checked(::posix_spawn_file_actions_adddup2(&actions.value, err_write.get(), 2));
    // Keep one known child fd, then close every other inherited descriptor.
    // This also excludes descriptors inherited by Guard from its launcher.
    // POSIX specifies that dup2(fd, fd) clears FD_CLOEXEC in spawn actions.
    if (owner_fd >= 0) checked(::posix_spawn_file_actions_adddup2(&actions.value, owner_fd, 3));
    checked(::posix_spawn_file_actions_addclosefrom_np(&actions.value, owner_fd >= 0 ? 4 : 3));
    std::vector<char*> argv;
    for (const auto& arg : args) argv.push_back(const_cast<char*>(arg.c_str()));
    argv.push_back(nullptr);
    std::array<std::string, 3> environment = {"PATH=/bin:/sbin:/usr/bin:/usr/sbin", "LC_ALL=C", "LVM_SYSTEM_DIR=/etc/lvm"};
    std::array<char*, 4> envp = {environment[0].data(), environment[1].data(), environment[2].data(), nullptr};
    pid_t child = -1;
    const int spawned = ::posix_spawnp(&child, argv.front(), &actions.value, nullptr, argv.data(), envp.data());
    if (spawned) system_failure("spawn " + args.front(), spawned);
    out_write.reset();
    err_write.reset();
    std::string output, errors;
    constexpr std::size_t output_limit = 1024 * 1024;
    std::size_t received = 0;
    bool timed_out = false, overflow = false, reaped = false;
    int status = 0;
    const double deadline = mono() + timeout;
    try {
        while (!reaped || out_read || err_read) {
            if (!timed_out && mono() >= deadline) {
                timed_out = true;
                if (!reaped) ::kill(child, SIGKILL);
            }
            if (!reaped) {
                const auto waited = ::waitpid(child, &status, WNOHANG);
                if (waited == child) reaped = true;
                else if (waited < 0 && errno != EINTR) system_failure("waitpid");
            }
            if (reaped && !out_read && !err_read) break;
            std::array<pollfd, 2> readers{{{out_read.get(), POLLIN, 0}, {err_read.get(), POLLIN, 0}}};
            const int polled = ::poll(readers.data(), readers.size(), 20);
            if (polled < 0 && errno != EINTR) system_failure("command poll");
            for (std::size_t index = 0; index < readers.size(); ++index) {
                if (readers[index].fd < 0 || !readers[index].revents) continue;
                auto& descriptor = index ? err_read : out_read;
                std::array<char, 8192> buffer{};
                // A bounded batch prevents a verbose child from indefinitely
                // postponing the timeout/reap checks.
                for (unsigned batch = 0; batch < 16; ++batch) {
                    const auto count = ::read(descriptor.get(), buffer.data(), buffer.size());
                    if (count < 0) {
                        if (errno == EINTR) continue;
                        if (errno == EAGAIN || errno == EWOULDBLOCK) break;
                        system_failure("command output");
                    }
                    if (!count) { descriptor.reset(); break; }
                    received += static_cast<std::size_t>(count);
                    if (received > output_limit) {
                        overflow = true;
                        if (!reaped) ::kill(child, SIGKILL);
                    }
                    if (!overflow) {
                        auto& destination = index ? errors : output;
                        destination.append(buffer.data(), static_cast<std::size_t>(count));
                        if (index && destination.size() > 4096) destination.erase(0, destination.size() - 4096);
                    }
                }
            }
            // A descendant could keep a pipe open after the direct helper
            // exits. It still owns any inherited fence; do not wait forever on
            // its output after the command budget has elapsed.
            if (reaped && (timed_out || overflow)) { out_read.reset(); err_read.reset(); }
        }
    } catch (...) {
        const auto error = std::current_exception();
        if (!reaped) {
            ::kill(child, SIGKILL);
            while (::waitpid(child, &status, 0) < 0 && errno == EINTR) {}
        }
        std::rethrow_exception(error);
    }
    if (overflow) throw std::runtime_error("Command output exceeds bounded limit");
    if (timed_out) throw std::runtime_error("Command timed out; in-flight kernel I/O may outlive a kill request");
    if (!WIFEXITED(status) || WEXITSTATUS(status)) {
        std::string description;
        for (const auto& arg : args) { if (!description.empty()) description += ' '; description += arg; }
        if (errors.size() > 3000) errors.erase(0, errors.size() - 3000);
        throw std::runtime_error("Command failed: " + description + '\n' + errors);
    }
    return output;
}
}  // namespace rescue
