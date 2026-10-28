// Test the real DLL menu implementation against a private HKCU view.
// Compile with the same Windows SDK/libraries as yzipshell.cpp.
#include "yzipshell.cpp"
#include <stdio.h>

static bool verify_menu(const wchar_t* path, DWORD mask, bool archive) {
    HKEY config = nullptr;
    if (RegCreateKeyExW(HKEY_CURRENT_USER, L"Software\\yzip", 0, nullptr, 0,
                        KEY_ALL_ACCESS, nullptr, &config, nullptr) != ERROR_SUCCESS)
        return false;
    const wchar_t command[] = L"yzip.exe";
    const wchar_t exts[] = L".zip;.alz";
    bool written = RegSetValueExW(config, L"Command", 0, REG_SZ,
                                (const BYTE*)command, sizeof(command)) == ERROR_SUCCESS &&
                   RegSetValueExW(config, L"ArchiveExts", 0, REG_SZ,
                                (const BYTE*)exts, sizeof(exts)) == ERROR_SUCCESS &&
                   RegSetValueExW(config, L"MenuMask", 0, REG_DWORD,
                                (const BYTE*)&mask, sizeof(mask)) == ERROR_SUCCESS;
    RegCloseKey(config);
    if (!written)
        return false;
    PIDLIST_ABSOLUTE absolute = nullptr;
    if (FAILED(SHParseDisplayName(path, nullptr, &absolute, 0, nullptr)))
        return false;
    IShellFolder* parent = nullptr;
    PCUITEMID_CHILD child = nullptr;
    HRESULT hr = SHBindToParent(absolute, IID_PPV_ARGS(&parent), &child);
    PIDLIST_ABSOLUTE parent_id = ILCloneFull(absolute);
    ILRemoveLastID(parent_id);
    IDataObject* data = nullptr;
    if (SUCCEEDED(hr))
        hr = SHCreateDataObject(parent_id, 1, &child, nullptr, IID_PPV_ARGS(&data));
    bool ok = SUCCEEDED(hr);
    YzipMenu* handler = new YzipMenu();
    HMENU menu = CreatePopupMenu();
    if (ok)
        ok = SUCCEEDED(handler->Initialize(nullptr, data, nullptr));
    if (ok) {
        hr = handler->QueryContextMenu(menu, 0, 1, 100, CMF_NORMAL);
        int expected = archive ? ((mask & 2) ? 1 : 0) + ((mask & 4) ? 1 : 0) : ((mask & 1) ? 2 : 0);
        ok = SUCCEEDED(hr) && GetMenuItemCount(menu) == expected && HRESULT_CODE(hr) == expected;
        for (int i = 0; ok && i < expected; ++i) {
            wchar_t verb[128] = {};
            ok = SUCCEEDED(handler->GetCommandString(i, GCS_VERBW, nullptr, (LPSTR)verb, 128));
            if (archive)
                ok = ok && ((lstrcmpW(verb, L"yzip.extracthere") == 0 && (mask & 2)) ||
                            (lstrcmpW(verb, L"yzip.extractfolder") == 0 && (mask & 4)));
            else
                ok = ok && ((lstrcmpW(verb, L"yzip.compress") == 0 ||
                             lstrcmpW(verb, L"yzip.compressdlg") == 0) && (mask & 1));
        }
    }
    DestroyMenu(menu);
    handler->Release();
    if (data) data->Release();
    if (parent) parent->Release();
    CoTaskMemFree(parent_id);
    CoTaskMemFree(absolute);
    return ok;
}

int wmain(int argc, wchar_t** argv) {
    if (argc != 3 || !exists(argv[1]) || !exists(argv[2])) return 2;
    HRESULT hr = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    if (FAILED(hr)) return 3;
    std::wstring sandbox = L"Software\\yzip-menu-test-" + std::to_wstring(GetCurrentProcessId()) + L"-" + std::to_wstring(GetTickCount64());
    HKEY root = nullptr;
    if (RegCreateKeyExW(HKEY_CURRENT_USER, sandbox.c_str(), 0, nullptr, 0,
                        KEY_ALL_ACCESS, nullptr, &root, nullptr) != ERROR_SUCCESS) return 4;
    if (RegOverridePredefKey(HKEY_CURRENT_USER, root) != ERROR_SUCCESS) return 5;
    bool ok = true;
    for (DWORD mask = 0; mask < 8; ++mask) {
        bool result = verify_menu(argv[1], mask, false) && verify_menu(argv[2], mask, true);
        printf("%s native menu mask %lu\n", result ? "PASS" : "FAIL", mask);
        ok = ok && result;
    }
    RegOverridePredefKey(HKEY_CURRENT_USER, nullptr);
    RegCloseKey(root);
    RegDeleteTreeW(HKEY_CURRENT_USER, sandbox.c_str());
    CoUninitialize();
    return ok ? 0 : 1;
}
