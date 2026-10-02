#include "core.hpp"

#include <atomic>
#include <cerrno>
#include <cmath>
#include <fcntl.h>
#include <fstream>
#include <future>
#include <iostream>
#include <limits>
#include <poll.h>
#include <signal.h>
#include <stdexcept>
#include <sys/file.h>
#include <sys/wait.h>
#include <thread>
#include <unistd.h>

using namespace rescue;
namespace {
std::atomic<unsigned> checks{0};
void require(bool condition, const std::string& description) {
    ++checks;
    if (!condition) throw std::runtime_error(description);
}
template<class Fn> void rejects(Fn fn, const std::string& description) {
    bool rejected = false;
    try { fn(); } catch (const std::exception&) { rejected = true; }
    require(rejected, description);
}
template<class Fn> void until(Fn fn) {
    const auto deadline = mono() + 3;
    while (!fn()) {
        if (mono() >= deadline) throw std::runtime_error("Timed out waiting for test worker");
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
}
struct Temp {
    fs::path path;
    Temp() {
        std::string pattern = "/tmp/rescue-core-test-XXXXXX";
        auto* result = ::mkdtemp(pattern.data());
        if (!result) throw std::runtime_error("mkdtemp failed");
        path = result;
    }
    ~Temp() { std::error_code error; fs::remove_all(path, error); }
};
bool lock_available(const fs::path& run) {
    try { Owner owner(run); return true; }
    catch (const std::system_error& error) {
        if (error.code().value() == EWOULDBLOCK || error.code().value() == EAGAIN) return false;
        throw;
    }
}

void json_contract() {
    Json value = {{"z", "中文😀\n"}, {"a", Json::array({1, true, nullptr, -0.0, 1.0,
        1e6, 1e15, 1e16, 1e-4, 1e-5, 1e-7, 1.2345678901234567})}};
    const std::string expected = R"({"a":[1,true,null,-0.0,1.0,1000000.0,1000000000000000.0,1e+16,0.0001,1e-05,1e-07,1.2345678901234567],"z":"\u4e2d\u6587\ud83d\ude00\n"})";
    require(canonical_json(value) == expected, "Python canonical JSON compatibility");
    require(digest(Json::object()) == "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a", "SHA256 known vector");
    require(canonical_json(std::numeric_limits<double>::denorm_min()) == "5e-324", "Subnormal JSON float");
    require(canonical_json(std::numeric_limits<double>::max()) == "1.7976931348623157e+308", "Maximum JSON float");
    rejects([] { canonical_json(std::numeric_limits<double>::infinity()); }, "Reject infinite digest input");
    rejects([] { canonical_json(std::numeric_limits<double>::quiet_NaN()); }, "Reject NaN digest input");
    const Json original = Json::array({Json::array({0, 4096, "multipath", "3 queue_if_no_path queue_mode bio 0 1 1 round-robin 0 1 1 8:17 1"})});
    Json changed = original;
    changed[0][3] = "2 queue_mode bio 0 1 0 round-robin 0 1 1 8:17 1";
    require(table_digest(original) == table_digest(changed), "Normalize only queue feature and selected group");
    changed[0][3] = "2 queue_mode bio 0 1 1 round-robin 0 1 1 8:33 1";
    require(table_digest(original) != table_digest(changed), "Backend remains authenticated");
    changed[0][3] = "2 queue_mode bio 0 2 1 round-robin 0 1 1 8:17 1";
    rejects([&] { table_digest(changed); }, "Unexpected topology rejected");
    changed[0][3] = "999";
    rejects([&] { table_digest(changed); }, "Malformed feature count rejected");
    changed[0][3] = "0";
    rejects([&] { table_digest(changed); }, "Missing multipath handler tail rejected");
}

void evidence_contract() {
    Temp temp;
    Owner owner(temp.path);
    require(owner.epoch.size() == 32 && !owner.boot_id.empty(), "Owner identity available");
    require(!lock_available(temp.path), "Exclusive owner lock");
    Fd duplicate(::fcntl(owner.fd.get(), F_DUPFD_CLOEXEC, 3));
    owner.close();
    require(!lock_available(temp.path), "Closing owner retains duplicate fence");
    duplicate.reset();
    require(lock_available(temp.path), "Final fence release admits owner");
    Owner journal_owner(temp.path);
    Journal journal(journal_owner, "fixture", "mpath-fixture");
    journal.write("waiting", {{"deadline", 123.5}, {"config_digest", digest({{"a", 1}})}});
    const auto record = load_json(journal.path);
    require(record == journal.record && record["phase"] == "waiting", "Journal writes atomically");
    rejects([&] { journal.write("huge", {{"payload", std::string(log_limit, 'x')}}); }, "Oversized journal rejected");
    require(load_json(journal.path) == record && journal.record == record, "Rejected record preserves prior state");
    Evidence evidence(temp.path);
    for (unsigned index = 0; index < 3; ++index)
        evidence.event({{"index", index}, {"payload", std::string(40000, 'x')}});
    require(load_json(temp.path / "path-state.json")["index"] == 2, "Evidence latest state");
    require(fs::file_size(temp.path / "path-events.jsonl") <= log_limit &&
            fs::file_size(temp.path / "path-events.previous.jsonl") <= log_limit,
            "Event rotation bounds both logs");
    require(Json::parse(read_text(temp.path / "path-events.previous.jsonl"))["index"] == 1,
            "Only one previous evidence log retained");
    std::ofstream(temp.path / "oversized") << std::string(log_limit + 1, 'x');
    rejects([&] { load_json(temp.path / "oversized"); }, "Oversized evidence read rejected");
}

void event_contract() {
    Events events;
    // Explicit construction avoids embedding-length assumptions in fixtures.
    const auto block = [](const std::string& path) {
        std::string text = "SUBSYSTEM=block";
        text += '\0'; text += "DEVPATH="; text += path; text += '\0'; return text;
    };
    events.watch({"/sys/devices/pci/usb/block/sda", "/sys/devices/virtual/block/dm-0"});
    require(events.relevant(block("/devices/pci/usb/block/sda/sda1")), "Registered disk descendants wake checks");
    require(events.relevant(block("/devices/virtual/block/dm-0")), "Registered DM wakes checks");
    require(!events.relevant(block("/devices/pci/usb/block/sdaa")), "Prefix collision does not wake checks");
    require(!events.relevant(block("/devices/virtual/block/loop0")), "Unrelated block events filtered");
    require(events.relevant("SUBSYSTEM=block"), "Malformed scoped block hint reconciles");
    require(!events.relevant("SUBSYSTEM=usb"), "USB hints alone do not change fault evidence");
    events.watch();
    require(events.relevant(block("/devices/other/usb/block/sdc")), "Recovery accepts changed USB topology");
    Schedule schedule(1);
    require(schedule.due(1), "Schedule initially due");
    schedule.completed(1, false);
    require(!schedule.due(1.05, true) && schedule.due(1.1, true) && schedule.due(2), "Healthy event coalescing and fallback");
    schedule.completed(2, true);
    require(std::abs(schedule.next_check - 2.1) < 1e-9 && schedule.delay == 0.2, "Initial recovery backoff");
    for (int index = 0; index < 8; ++index) schedule.completed(3, true);
    require(schedule.delay == 0.8, "Backoff capped");
}

void operation_contract() {
    Temp temp;
    Owner owner(temp.path);
    OwnedOperation operation(owner.fd.get());
    rejects([&] { operation.fence_fd(); }, "Main thread cannot borrow worker fence");
    std::promise<void> release;
    auto allowed = release.get_future().share();
    std::atomic<unsigned> deleted{0};
    struct Resource {
        std::atomic<unsigned>* deleted;
        explicit Resource(std::atomic<unsigned>* value) : deleted(value) {}
        ~Resource() { ++*deleted; }
    };
    const auto token = operation.start("verify", [&] {
        require(operation.fence_fd() >= 3, "Worker owns a live fence");
        allowed.wait();
        return std::any(std::make_shared<Resource>(&deleted));
    });
    rejects([&] { operation.start("another", [] { return std::any(); }); }, "Only one operation admitted");
    rejects([&] { operation.close(); }, "Pending close needs explicit cleanup");
    owner.close();
    require(!lock_available(temp.path), "Worker retains fence after controller owner closes");
    release.set_value();
    pollfd notification{operation.fileno(), POLLIN, 0};
    require(::poll(&notification, 1, 3000) == 1, "Worker completion wakes poll");
    require(!lock_available(temp.path), "Unread result retains fence");
    auto outcome = operation.poll();
    require(outcome && outcome->token == token && outcome->kind == "verify" && outcome->error.is_null(), "Poll transfers result");
    require(!operation.busy() && !operation.poll() && lock_available(temp.path), "Result consumed exactly once and fence released");
    auto resource = std::any_cast<std::shared_ptr<Resource>>(outcome->value);
    require(deleted == 0, "Transferred credential still lives");
    outcome.reset(); resource.reset();
    require(deleted == 1, "Transferred credential releases once");
    operation.close();
    rejects([&] { operation.start("again", [] { return std::any(); }); }, "Closed executor cannot restart");
}

void abandon_contract(bool throws) {
    Temp temp;
    Owner owner(temp.path);
    OwnedOperation operation(owner.fd.get());
    std::promise<void> release;
    auto allowed = release.get_future().share();
    std::atomic<unsigned> cleanup{0};
    operation.start("late", [allowed] { allowed.wait(); return std::any(Json{{"finished", true}}); });
    owner.close();
    operation.abandon([&](const Outcome& outcome) {
        require(outcome.kind == "late" && std::any_cast<Json>(outcome.value)["finished"] == true,
                "Late cleanup receives original outcome");
        ++cleanup;
        if (throws) throw std::runtime_error("cleanup fixture");
    });
    operation.abandon([&](const Outcome&) { cleanup += 100; });
    require(!operation.poll() && !lock_available(temp.path), "Abandon prevents admission while task remains fenced");
    release.set_value();
    until([&] { return !operation.busy(); });
    require(cleanup == 1 && lock_available(temp.path), "Late cleanup exactly once before fence release");
    require(throws != operation.cleanup_error().is_null(), "Cleanup error retained when present");
}

void helper_contract(const std::string& executable) {
    require(command({"/usr/bin/printf", "%s", "literal $() ` \\ 中文"}, 1) == "literal $() ` \\ 中文",
            "Helper arguments never pass through a shell");
    rejects([&] { command({"/bin/sleep", "1"}, 0.02); }, "Command timeout terminates and reaps helper");
    rejects([&] { command({executable, "--flood"}, 2); }, "Helper output is bounded");
    Temp temp;
    const auto child = ::fork();
    if (child < 0) throw std::runtime_error("fork failed");
    if (!child) {
        try {
            Owner owner(temp.path);
            command({executable, "--hold-fence", temp.path.string()}, 3, owner.fd.get());
            _exit(0);
        } catch (...) { _exit(1); }
    }
    try {
        until([&] { return fs::exists(temp.path / "helper-ready"); });
        require(!lock_available(temp.path), "Helper inherits owner fence");
        ::kill(child, SIGKILL);
        int status = 0;
        require(::waitpid(child, &status, 0) == child && WIFSIGNALED(status), "Controller killed during helper");
        require(!lock_available(temp.path), "Helper fences takeover after parent SIGKILL");
        until([&] { return lock_available(temp.path); });
        require(true, "Helper exit releases final inherited fence");
    } catch (...) {
        ::kill(child, SIGKILL);
        int status;
        while (::waitpid(child, &status, 0) < 0 && errno == EINTR) {}
        throw;
    }
}
}  // namespace

int main(int argc, char** argv) {
    try {
        if (argc > 1 && std::string(argv[1]) == "--canonical") {
            for (std::string line; std::getline(std::cin, line);)
                std::cout << canonical_json(Json::parse(line)) << '\n';
            return 0;
        }
        if (argc > 1 && std::string(argv[1]) == "--flood") {
            for (;;) std::cout << std::string(8192, 'x') << std::flush;
        }
        if (argc > 2 && std::string(argv[1]) == "--hold-fence") {
            if (::fcntl(3, F_GETFD) < 0) return 2;
            std::ofstream(fs::path(argv[2]) / "helper-ready") << "ready";
            std::this_thread::sleep_for(std::chrono::milliseconds(500));
            return 0;
        }
        json_contract();
        evidence_contract();
        event_contract();
        operation_contract();
        abandon_contract(false);
        abandon_contract(true);
        helper_contract(fs::canonical(argv[0]).string());
        std::cout << "core: " << checks.load() << " checks passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "core test failed: " << error.what() << '\n';
        return 1;
    }
}
