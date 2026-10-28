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

YZ_ACCEL_EXPORT std::uint32_t YZ_ACCEL_CALL yz_accel_abi_version() noexcept;
YZ_ACCEL_EXPORT std::uint64_t YZ_ACCEL_CALL yz_accel_capabilities() noexcept;
YZ_ACCEL_EXPORT std::size_t YZ_ACCEL_CALL yz_accel_last_error(
    wchar_t* buffer,
    std::size_t buffer_chars) noexcept;
YZ_ACCEL_EXPORT void YZ_ACCEL_CALL yz_accel_cancel(
    volatile std::uint32_t* cancellation_flag) noexcept;
