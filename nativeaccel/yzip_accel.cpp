#include "yzip_accel.h"

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <Windows.h>

#include <algorithm>
#include <string>

namespace {
thread_local std::wstring g_last_error;
static_assert(sizeof(std::uint32_t) == sizeof(LONG));
}  // namespace

std::uint32_t YZ_ACCEL_CALL yz_accel_abi_version() noexcept {
    return YZ_ACCEL_ABI_VERSION;
}

std::uint64_t YZ_ACCEL_CALL yz_accel_capabilities() noexcept {
    // Codec operation bits are introduced by later tasks.
    return 0;
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
