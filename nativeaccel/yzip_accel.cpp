#include "yzip_accel.h"

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <Windows.h>
#include <ObjBase.h>
#include <OleAuto.h>

#include "CPP/Common/MyInitGuid.h"
#include "CPP/7zip/Archive/IArchive.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <limits>
#include <new>
#include <string>
#include <vector>

namespace {
thread_local std::wstring g_last_error;
static_assert(sizeof(std::uint32_t) == sizeof(LONG));
static_assert(sizeof(std::uint64_t) == sizeof(LONG64));

const GUID kZipClassId = {
    0x23170F69,
    0x40C1,
    0x278A,
    {0x10, 0x00, 0x00, 0x01, 0x10, 0x01, 0x00, 0x00},
};

using CreateObjectFunction = HRESULT(WINAPI*)(const GUID*, const GUID*, void**);

void set_error(const wchar_t* message) {
    g_last_error = message == nullptr ? L"native ZIP extraction failed" : message;
}

bool is_cancelled(const volatile std::uint32_t* flag) {
    if (flag == nullptr) {
        return false;
    }
    auto* value = reinterpret_cast<volatile LONG*>(
        const_cast<volatile std::uint32_t*>(flag));
    return InterlockedCompareExchange(value, 0L, 0L) != 0;
}

std::uint32_t crc32_update(std::uint32_t crc, const Byte* data, UInt32 size) {
    static const std::array<std::uint32_t, 256> table = [] {
        std::array<std::uint32_t, 256> values{};
        for (std::uint32_t value = 0; value < values.size(); ++value) {
            std::uint32_t entry = value;
            for (unsigned bit = 0; bit < 8; ++bit) {
                const std::uint32_t mask = 0U - (entry & 1U);
                entry = (entry >> 1U) ^ (0xEDB88320U & mask);
            }
            values[value] = entry;
        }
        return values;
    }();
    crc = ~crc;
    for (UInt32 index = 0; index < size; ++index) {
        crc = table[(crc ^ data[index]) & 0xFFU] ^ (crc >> 8U);
    }
    return ~crc;
}

class InFileStream final : public IInStream {
public:
    explicit InFileStream(const wchar_t* path) noexcept
        : refs_(1), handle_(CreateFileW(
              path,
              GENERIC_READ,
              FILE_SHARE_READ,
              nullptr,
              OPEN_EXISTING,
              FILE_ATTRIBUTE_NORMAL | FILE_FLAG_SEQUENTIAL_SCAN,
              nullptr)) {}

    ~InFileStream() {
        if (handle_ != INVALID_HANDLE_VALUE) {
            CloseHandle(handle_);
        }
    }

    bool valid() const noexcept { return handle_ != INVALID_HANDLE_VALUE; }

    STDMETHOD(QueryInterface)(REFIID iid, void** object) noexcept override {
        if (object == nullptr) {
            return E_POINTER;
        }
        *object = nullptr;
        if (iid == IID_IUnknown || iid == IID_ISequentialInStream || iid == IID_IInStream) {
            *object = static_cast<IInStream*>(this);
            AddRef();
            return S_OK;
        }
        return E_NOINTERFACE;
    }

    STDMETHOD_(ULONG, AddRef)() noexcept override { return ++refs_; }

    STDMETHOD_(ULONG, Release)() noexcept override {
        const ULONG remaining = --refs_;
        if (remaining == 0) {
            delete this;
        }
        return remaining;
    }

    STDMETHOD(Read)(void* data, UInt32 size, UInt32* processed_size) noexcept override {
        if (processed_size == nullptr) {
            return E_POINTER;
        }
        *processed_size = 0;
        DWORD read = 0;
        if (!ReadFile(handle_, data, size, &read, nullptr)) {
            return HRESULT_FROM_WIN32(GetLastError());
        }
        *processed_size = read;
        return S_OK;
    }

    STDMETHOD(Seek)(Int64 offset, UInt32 origin, UInt64* new_position) noexcept override {
        LARGE_INTEGER distance{};
        distance.QuadPart = offset;
        LARGE_INTEGER position{};
        if (!SetFilePointerEx(handle_, distance, &position, origin)) {
            return HRESULT_FROM_WIN32(GetLastError());
        }
        if (position.QuadPart < 0) {
            return STG_E_INVALIDFUNCTION;
        }
        if (new_position != nullptr) {
            *new_position = static_cast<UInt64>(position.QuadPart);
        }
        return S_OK;
    }

private:
    std::atomic<ULONG> refs_;
    HANDLE handle_;
};

class StagingOutStream final : public ISequentialOutStream {
public:
    StagingOutStream(
        const yz_zip_job& job,
        const std::uint64_t output_limit,
        yz_progress_counters* progress,
        volatile std::uint32_t* cancellation_flag,
        bool* corrupt,
        bool* io_failed) noexcept
        : refs_(1),
          handle_(CreateFileW(
              job.staging_path,
              GENERIC_WRITE,
              0,
              nullptr,
              CREATE_ALWAYS,
              FILE_ATTRIBUTE_NORMAL | FILE_FLAG_SEQUENTIAL_SCAN,
              nullptr)),
          expected_size_(job.expected_size),
          expected_crc_(job.expected_crc),
          output_limit_(output_limit),
          progress_(progress),
          cancellation_flag_(cancellation_flag),
          corrupt_(corrupt),
          io_failed_(io_failed),
          size_(0),
          crc_(0) {}

    ~StagingOutStream() {
        if (handle_ != INVALID_HANDLE_VALUE) {
            CloseHandle(handle_);
        }
    }

    bool valid() const noexcept { return handle_ != INVALID_HANDLE_VALUE; }
    bool verified() const noexcept {
        return size_ == expected_size_ && crc_ == expected_crc_;
    }

    STDMETHOD(QueryInterface)(REFIID iid, void** object) noexcept override {
        if (object == nullptr) {
            return E_POINTER;
        }
        *object = nullptr;
        if (iid == IID_IUnknown || iid == IID_ISequentialOutStream) {
            *object = static_cast<ISequentialOutStream*>(this);
            AddRef();
            return S_OK;
        }
        return E_NOINTERFACE;
    }

    STDMETHOD_(ULONG, AddRef)() noexcept override { return ++refs_; }

    STDMETHOD_(ULONG, Release)() noexcept override {
        const ULONG remaining = --refs_;
        if (remaining == 0) {
            delete this;
        }
        return remaining;
    }

    STDMETHOD(Write)(const void* data, UInt32 size, UInt32* processed_size) noexcept override {
        if (processed_size == nullptr) {
            return E_POINTER;
        }
        *processed_size = 0;
        if (is_cancelled(cancellation_flag_)) {
            return E_ABORT;
        }
        if (size_ > expected_size_ || size > expected_size_ - size_) {
            *corrupt_ = true;
            return E_FAIL;
        }
        auto* completed = reinterpret_cast<volatile LONG64*>(&progress_->completed_bytes);
        LONG64 observed = InterlockedCompareExchange64(completed, 0, 0);
        while (true) {
            if (observed < 0) {
                return E_FAIL;
            }
            const std::uint64_t current = static_cast<std::uint64_t>(observed);
            if (current > output_limit_ || size > output_limit_ - current) {
                *corrupt_ = true;
                return E_FAIL;
            }
            const LONG64 desired = observed + static_cast<LONG64>(size);
            const LONG64 prior = InterlockedCompareExchange64(completed, desired, observed);
            if (prior == observed) {
                break;
            }
            observed = prior;
        }
        DWORD written = 0;
        if (!WriteFile(handle_, data, size, &written, nullptr) || written != size) {
            *io_failed_ = true;
            InterlockedExchangeAdd64(completed, -static_cast<LONG64>(size));
            return HRESULT_FROM_WIN32(GetLastError());
        }
        size_ += written;
        crc_ = crc32_update(crc_, static_cast<const Byte*>(data), written);
        *processed_size = written;
        return S_OK;
    }

private:
    std::atomic<ULONG> refs_;
    HANDLE handle_;
    std::uint64_t expected_size_;
    std::uint32_t expected_crc_;
    std::uint64_t output_limit_;
    yz_progress_counters* progress_;
    volatile std::uint32_t* cancellation_flag_;
    bool* corrupt_;
    bool* io_failed_;
    std::uint64_t size_;
    std::uint32_t crc_;
};

class ExtractCallback final : public IArchiveExtractCallback {
public:
    ExtractCallback(
        const yz_zip_job* jobs,
        const std::size_t count,
        const std::uint64_t output_limit,
        yz_progress_counters* progress,
        volatile std::uint32_t* cancellation_flag) noexcept
        : refs_(1),
          jobs_(jobs),
          count_(count),
          output_limit_(output_limit),
          progress_(progress),
          cancellation_flag_(cancellation_flag),
          current_(nullptr),
          operation_failed_(false),
          corrupt_(false),
          io_failed_(false) {}

    ~ExtractCallback() { release_current(); }

    bool operation_failed() const noexcept { return operation_failed_; }
    bool corrupt() const noexcept { return corrupt_; }
    bool io_failed() const noexcept { return io_failed_; }

    STDMETHOD(QueryInterface)(REFIID iid, void** object) noexcept override {
        if (object == nullptr) {
            return E_POINTER;
        }
        *object = nullptr;
        if (iid == IID_IUnknown || iid == IID_IArchiveExtractCallback) {
            *object = static_cast<IArchiveExtractCallback*>(this);
        } else if (iid == IID_IProgress) {
            *object = static_cast<IProgress*>(this);
        } else {
            return E_NOINTERFACE;
        }
        AddRef();
        return S_OK;
    }

    STDMETHOD_(ULONG, AddRef)() noexcept override { return ++refs_; }

    STDMETHOD_(ULONG, Release)() noexcept override {
        const ULONG remaining = --refs_;
        if (remaining == 0) {
            delete this;
        }
        return remaining;
    }

    STDMETHOD(SetTotal)(UInt64 total) noexcept override {
        if (total > output_limit_) {
            return E_FAIL;
        }
        return is_cancelled(cancellation_flag_) ? E_ABORT : S_OK;
    }

    STDMETHOD(SetCompleted)(const UInt64*) noexcept override {
        return is_cancelled(cancellation_flag_) ? E_ABORT : S_OK;
    }

    STDMETHOD(GetStream)(
        UInt32 index,
        ISequentialOutStream** stream,
        Int32 ask_mode) noexcept override {
        if (stream == nullptr) {
            return E_POINTER;
        }
        *stream = nullptr;
        release_current();
        if (is_cancelled(cancellation_flag_)) {
            return E_ABORT;
        }
        if (ask_mode != NArchive::NExtract::NAskMode::kExtract) {
            return S_OK;
        }
        const yz_zip_job* job = find_job(index);
        if (job == nullptr || job->staging_path == nullptr) {
            operation_failed_ = true;
            return E_FAIL;
        }
        current_ = new (std::nothrow) StagingOutStream(
            *job, output_limit_, progress_, cancellation_flag_, &corrupt_, &io_failed_);
        if (current_ == nullptr) {
            return E_OUTOFMEMORY;
        }
        if (!current_->valid()) {
            operation_failed_ = true;
            io_failed_ = true;
            release_current();
            return E_FAIL;
        }
        current_->AddRef();
        *stream = current_;
        return S_OK;
    }

    STDMETHOD(PrepareOperation)(Int32) noexcept override {
        return is_cancelled(cancellation_flag_) ? E_ABORT : S_OK;
    }

    STDMETHOD(SetOperationResult)(Int32 result) noexcept override {
        if (result != NArchive::NExtract::NOperationResult::kOK ||
            current_ == nullptr || !current_->verified()) {
            operation_failed_ = true;
            corrupt_ = true;
        }
        release_current();
        return operation_failed_ ? E_FAIL : S_OK;
    }

private:
    const yz_zip_job* find_job(const UInt32 index) const noexcept {
        for (std::size_t job = 0; job < count_; ++job) {
            if (jobs_[job].source_index == index) {
                return &jobs_[job];
            }
        }
        return nullptr;
    }

    void release_current() noexcept {
        if (current_ != nullptr) {
            current_->Release();
            current_ = nullptr;
        }
    }

    std::atomic<ULONG> refs_;
    const yz_zip_job* jobs_;
    std::size_t count_;
    std::uint64_t output_limit_;
    yz_progress_counters* progress_;
    volatile std::uint32_t* cancellation_flag_;
    StagingOutStream* current_;
    bool operation_failed_;
    bool corrupt_;
    bool io_failed_;
};
}  // namespace

std::uint32_t YZ_ACCEL_CALL yz_accel_abi_version() noexcept {
    return YZ_ACCEL_ABI_VERSION;
}

std::uint64_t YZ_ACCEL_CALL yz_accel_capabilities() noexcept {
    return YZ_ACCEL_CAP_ZIP_EXTRACT;
}

std::size_t YZ_ACCEL_CALL yz_accel_last_error(
    wchar_t* buffer,
    const std::size_t buffer_chars) noexcept {
    try {
        const std::size_t required = g_last_error.size() + 1;
        if (buffer != nullptr && buffer_chars != 0) {
            const std::size_t copied = std::min(g_last_error.size(), buffer_chars - 1);
            g_last_error.copy(buffer, copied);
            buffer[copied] = L'\0';
        }
        return required;
    } catch (...) {
        if (buffer != nullptr && buffer_chars != 0) {
            buffer[0] = L'\0';
        }
        return 1;
    }
}

void YZ_ACCEL_CALL yz_accel_cancel(
    volatile std::uint32_t* cancellation_flag) noexcept {
    if (cancellation_flag == nullptr) {
        return;
    }
    auto* target = reinterpret_cast<volatile LONG*>(cancellation_flag);
    InterlockedExchange(target, 1L);
}

std::uint32_t YZ_ACCEL_CALL yz_accel_extract_zip(
    const wchar_t* seven_zip_dll,
    const wchar_t* source,
    const yz_zip_job* jobs,
    const std::size_t job_count,
    const std::uint64_t output_limit,
    yz_progress_counters* progress,
    volatile std::uint32_t* cancellation_flag) noexcept {
    g_last_error.clear();
    if (seven_zip_dll == nullptr || source == nullptr || jobs == nullptr ||
        progress == nullptr || job_count == 0 ||
        job_count > std::numeric_limits<UInt32>::max()) {
        set_error(L"invalid native ZIP extraction arguments");
        return YZ_ACCEL_ERROR;
    }
    if (is_cancelled(cancellation_flag)) {
        return YZ_ACCEL_CANCELLED;
    }
    progress->completed_bytes = 0;
    progress->total_bytes = output_limit;
    HMODULE module = LoadLibraryExW(
        seven_zip_dll,
        nullptr,
        LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
    if (module == nullptr) {
        set_error(L"could not load pinned 7z.dll");
        return YZ_ACCEL_ERROR;
    }
    std::uint32_t status = YZ_ACCEL_ERROR;
    try {
        auto create_object = reinterpret_cast<CreateObjectFunction>(
            GetProcAddress(module, "CreateObject"));
        if (create_object == nullptr) {
            set_error(L"7z.dll does not export CreateObject");
        } else {
            IInArchive* archive = nullptr;
            HRESULT result = create_object(
                &kZipClassId, &IID_IInArchive, reinterpret_cast<void**>(&archive));
            if (result != S_OK || archive == nullptr) {
                set_error(L"7z.dll could not create the ZIP handler");
            } else {
                InFileStream* input = new (std::nothrow) InFileStream(source);
                if (input == nullptr || !input->valid()) {
                    if (input != nullptr) {
                        input->Release();
                    }
                    set_error(L"could not open the ZIP source");
                } else {
                    result = archive->Open(input, nullptr, nullptr);
                    input->Release();
                    if (result != S_OK) {
                        set_error(L"7z.dll could not open the ZIP archive");
                    } else {
                        std::vector<UInt32> indices;
                        indices.reserve(job_count);
                        for (std::size_t index = 0; index < job_count; ++index) {
                            indices.push_back(jobs[index].source_index);
                        }
                        ExtractCallback* callback = new (std::nothrow) ExtractCallback(
                            jobs, job_count, output_limit, progress, cancellation_flag);
                        if (callback == nullptr) {
                            set_error(L"could not allocate the ZIP extraction callback");
                        } else {
                            result = archive->Extract(
                                indices.data(),
                                static_cast<UInt32>(indices.size()),
                                0,
                                callback);
                            if (is_cancelled(cancellation_flag)) {
                                status = YZ_ACCEL_CANCELLED;
                            } else if (callback->corrupt() ||
                                       (result != S_OK && !callback->io_failed())) {
                                set_error(L"7z.dll reported corrupt ZIP data");
                                status = YZ_ACCEL_CORRUPT;
                            } else if (result != S_OK || callback->operation_failed()) {
                                set_error(L"7z.dll reported a ZIP extraction error");
                            } else {
                                status = YZ_ACCEL_OK;
                            }
                            callback->Release();
                        }
                        archive->Close();
                    }
                }
                archive->Release();
            }
        }
    } catch (...) {
        set_error(L"unexpected exception in native ZIP extraction");
        status = YZ_ACCEL_ERROR;
    }
    FreeLibrary(module);
    return status;
}
