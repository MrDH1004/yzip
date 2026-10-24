// Headless probe for the yzip context-menu handler, going through the shell's own menu composition
// (IShellFolder::GetUIObjectOf -> default context menu -> registered handlers), like Explorer does.
// usage: probe list|defaultonly|invoke|invokedlg <path> [<path> ...]   (all paths in one folder)
// prints one "ITEM\t<text>\t[icon]\t<verb>" line per top-level menu item (UTF-8);
// "invoke" runs yzip's "<name>.zip으로 압축하기", "invokedlg" runs "yzip으로 압축하기...".
#include <windows.h>
#include <shlobj.h>
#include <shlwapi.h>
#include <stdio.h>
#include <string>
#include <vector>

static void put(const std::wstring& s) {
    int n = WideCharToMultiByte(CP_UTF8, 0, s.c_str(), (int)s.size(), nullptr, 0, nullptr, nullptr);
    std::string b(n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, s.c_str(), (int)s.size(), &b[0], n, nullptr, nullptr);
    fwrite(b.data(), 1, b.size(), stdout);
    fputc('\n', stdout);
    fflush(stdout);
}

int wmain(int argc, wchar_t** argv) {
    if (argc < 3)
        return 2;
    std::wstring mode = argv[1];
    CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    std::vector<PIDLIST_ABSOLUTE> abs;
    for (int i = 2; i < argc; i++) {
        PIDLIST_ABSOLUTE p = nullptr;
        if (FAILED(SHParseDisplayName(argv[i], nullptr, &p, 0, nullptr))) {
            put(L"ERROR parse " + std::wstring(argv[i]));
            return 3;
        }
        abs.push_back(p);
    }
    IShellFolder* parent = nullptr;
    PCUITEMID_CHILD first = nullptr;
    if (FAILED(SHBindToParent(abs[0], IID_PPV_ARGS(&parent), &first))) {
        put(L"ERROR bind");
        return 3;
    }
    std::vector<PCUITEMID_CHILD> kids;
    for (auto p : abs)
        kids.push_back(ILFindLastID(p));
    IContextMenu* cm = nullptr;
    HRESULT hr = parent->GetUIObjectOf(nullptr, (UINT)kids.size(), kids.data(), IID_IContextMenu, nullptr, (void**)&cm);
    if (FAILED(hr)) {
        put(L"ERROR GetUIObjectOf");
        return 3;
    }
    HMENU menu = CreatePopupMenu();
    const UINT FIRST = 1;
    hr = cm->QueryContextMenu(menu, 0, FIRST, 0x7FFF, mode == L"defaultonly" ? CMF_DEFAULTONLY : CMF_NORMAL);
    if (FAILED(hr)) {
        put(L"ERROR QueryContextMenu");
        return 3;
    }
    int target = -1;
    for (int i = 0; i < GetMenuItemCount(menu); i++) {
        wchar_t text[512] = L"";
        MENUITEMINFOW mii = {sizeof(mii)};
        mii.fMask = MIIM_STRING | MIIM_ID | MIIM_BITMAP;
        mii.dwTypeData = text;
        mii.cch = 512;
        if (!GetMenuItemInfoW(menu, i, TRUE, &mii) || !text[0])
            continue;
        // the verb, not the text, identifies our item (Bandizip adds an item with the very same text)
        wchar_t verb[128] = L"";
        if ((int)mii.wID >= (int)FIRST && (int)mii.wID < 0x7FFF)
            cm->GetCommandString(mii.wID - FIRST, GCS_VERBW, nullptr, (LPSTR)verb, 128);
        put(L"ITEM\t" + std::wstring(text) + L"\t" + (mii.hbmpItem ? L"[icon]" : L"") + L"\t" + verb);
        if (lstrcmpW(verb, mode == L"invokedlg" ? L"yzip.compressdlg" : L"yzip.compress") == 0)
            target = (int)mii.wID;
    }
    if (mode == L"invoke" || mode == L"invokedlg") {
        if (target < 0) {
            put(L"ERROR no yzip item");
            return 4;
        }
        CMINVOKECOMMANDINFOEX ici = {sizeof(ici)};
        ici.fMask = CMIC_MASK_UNICODE;
        ici.lpVerb = MAKEINTRESOURCEA(target - FIRST);
        ici.lpVerbW = MAKEINTRESOURCEW(target - FIRST);
        ici.nShow = SW_SHOWNORMAL;
        hr = cm->InvokeCommand((LPCMINVOKECOMMANDINFO)&ici);
        put(SUCCEEDED(hr) ? L"INVOKED" : L"ERROR invoke");
        if (FAILED(hr))
            return 5;
    }
    cm->Release();
    parent->Release();
    for (auto p : abs)
        CoTaskMemFree(p);
    CoUninitialize();
    return 0;
}
