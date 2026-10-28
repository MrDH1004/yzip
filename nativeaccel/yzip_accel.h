#pragma once

#include <cstddef>
#include <cstdint>

#if defined(_WIN32)
#define YZ_ACCEL_EXPORT extern "C" __declspec(dllexport)
#define YZ_ACCEL_CALL __cdecl
#else
#error yzip_accel is supported only on Windows
#endif

#define YZ_ACCEL_ABI_VERSION 1U
#define YZ_ACCEL_CAP_ZIP_EXTRACT (1ULL << 0)

enum yz_accel_status : std::uint32_t {
    YZ_ACCEL_OK = 0,
    YZ_ACCEL_CANCELLED = 1,
    YZ_ACCEL_UNSUPPORTED = 2,
    YZ_ACCEL_ERROR = 3,
    YZ_ACCEL_CORRUPT = 4,
};

struct yz_zip_job {
    std::uint32_t source_index;
    const wchar_t* staging_path;
    std::uint64_t expected_size;
    std::uint32_t expected_crc;
};

struct yz_progress_counters {
    volatile std::uint64_t completed_bytes;
    std::uint64_t total_bytes;
};

YZ_ACCEL_EXPORT std::uint32_t YZ_ACCEL_CALL yz_accel_abi_version() noexcept;
YZ_ACCEL_EXPORT std::uint64_t YZ_ACCEL_CALL yz_accel_capabilities() noexcept;
YZ_ACCEL_EXPORT std::size_t YZ_ACCEL_CALL yz_accel_last_error(
    wchar_t* buffer,
    std::size_t buffer_chars) noexcept;
YZ_ACCEL_EXPORT void YZ_ACCEL_CALL yz_accel_cancel(
    volatile std::uint32_t* cancellation_flag) noexcept;
YZ_ACCEL_EXPORT std::uint32_t YZ_ACCEL_CALL yz_accel_extract_zip(
    const wchar_t* seven_zip_dll,
    const wchar_t* source,
    const yz_zip_job* jobs,
    std::size_t job_count,
    std::uint64_t output_limit,
    yz_progress_counters* progress,
    volatile std::uint32_t* cancellation_flag) noexcept;
