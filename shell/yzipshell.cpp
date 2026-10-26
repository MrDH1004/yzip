// yzip Explorer context-menu handler. When every selected item is an archive (an extension listed in
// HKCU\Software\yzip\ArchiveExts), first the extract items, so they sit in the menu's extract section next to
// Windows' and Bandizip's instead of the static-verb group at the top:
//   2 "여기에 압축 풀기": <Command> --extract-list here <list>
//   3 "폴더에 압축 풀기": <Command> --extract-list folder <list>
// otherwise (not all archives) the compress items:
//   0 "<name>.zip으로 압축하기 - yzip": compresses at once. The handler decides the output path (same "(2)" rule
//     as yzip's unique_path) so the label and the file always agree: <Command> --archive-list <list> --out <zip>
//   1 "yzip으로 압축하기(W)...": opens yzip's compress window (format, password, ...):
//     <Command> --compress-dialog-list <list>
// The selection goes in a UTF-8 list file. <Command> comes from HKCU\Software\yzip\Command (--register-shell).
#include <windows.h>
#include <shlobj.h>
#include <shlwapi.h>
#include <shellapi.h>
#include <strsafe.h>
#include <new>
#include <string>
#include <vector>

// {7E3B1C52-9A4D-4F1E-B6C2-3D8A5F0E9C71}
static const CLSID CLSID_YzipMenu = {0x7e3b1c52, 0x9a4d, 0x4f1e, {0xb6, 0xc2, 0x3d, 0x8a, 0x5f, 0x0e, 0x9c, 0x71}};
enum { CMD_COMPRESS, CMD_DIALOG, CMD_HERE, CMD_FOLDER, N_CMDS };
static const wchar_t* const VERBS_W[] = {L"yzip.compress", L"yzip.compressdlg", L"yzip.extracthere", L"yzip.extractfolder"};
static const char* const VERBS_A[] = {"yzip.compress", "yzip.compressdlg", "yzip.extracthere", "yzip.extractfolder"};

static HINSTANCE g_inst;
static long g_refs;

static bool exists(const std::wstring& p) { return GetFileAttributesW(p.c_str()) != INVALID_FILE_ATTRIBUTES; }

static bool is_dir(const std::wstring& p) {
    DWORD a = GetFileAttributesW(p.c_str());
    return a != INVALID_FILE_ATTRIBUTES && (a & FILE_ATTRIBUTE_DIRECTORY);
}

static size_t last_sep(const std::wstring& p) { return p.find_last_of(L"\\/"); }

static std::wstring file_name(const std::wstring& p) {
    size_t i = last_sep(p);
    return i == std::wstring::npos ? p : p.substr(i + 1);
}

// os.path.splitext: the extension starts at the last dot that follows a non-dot character of the name
static std::wstring strip_ext(const std::wstring& p) {
    size_t start = last_sep(p);
    start = start == std::wstring::npos ? 0 : start + 1;
    size_t first = p.find_first_not_of(L'.', start);
    size_t dot = p.find_last_of(L'.');
    if (first == std::wstring::npos || dot == std::wstring::npos || dot <= first || dot < start)
        return p;
    return p.substr(0, dot);
}

// Same naming as yzip: one file -> name without extension, one folder -> folder name,
// several items -> name of the folder they are in; then "name (2).zip" if taken.
static std::wstring output_path(const std::vector<std::wstring>& paths) {
    std::wstring base;
    if (paths.size() == 1) {
        std::wstring p = paths[0];
        if (p.size() <= 3 && p.size() >= 2 && p[1] == L':')
            return L"";  // drive root
        base = is_dir(p) ? p : strip_ext(p);
    } else {
        std::wstring dir = paths[0].substr(0, last_sep(paths[0]));
        if (dir.size() == 2 && dir[1] == L':')
            dir += L"\\";
        std::wstring name = file_name(dir);
        base = (dir.back() == L'\\' ? dir : dir + L"\\") + (name.empty() ? L"archive" : name);
    }
    std::wstring out = base + L".zip";
    for (int i = 2; exists(out); i++)
        out = base + L" (" + std::to_wstring(i) + L").zip";
    return out;
}

static std::wstring menu_label(const std::wstring& out) {
    std::wstring name = file_name(out);
    if (name.size() > 52)
        name = name.substr(0, 24) + L"…" + name.substr(name.size() - 24);
    std::wstring esc;
    for (wchar_t c : name) {
        if (c == L'&')
            esc += L'&';
        esc += c;
    }
    return esc + L"으로 압축하기 - &yzip";  // Bandizip uses the same wording with &Z, so name ourselves and take Y
}

static std::wstring read_command() {
    wchar_t buf[2048];
    DWORD cb = sizeof(buf);
    if (RegGetValueW(HKEY_CURRENT_USER, L"Software\\yzip", L"Command", RRF_RT_REG_SZ, nullptr, buf, &cb) != ERROR_SUCCESS)
        return L"";
    return buf;
}

static std::wstring icon_source() {
    wchar_t buf[2048];
    DWORD cb = sizeof(buf);
    if (RegGetValueW(HKEY_CURRENT_USER, L"Software\\yzip", L"Icon", RRF_RT_REG_SZ, nullptr, buf, &cb) != ERROR_SUCCESS)
        return L"";
    return buf;
}

// ";.zip;.7z;...;" (lowercase) from --register-shell: ArchiveExts (archives proper) or ImageExts (disk images,
// installers: yzip extracts them, but they are not "just an archive", so compressing stays offered)
static std::wstring ext_list(const wchar_t* value) {
    wchar_t buf[2048];
    DWORD cb = sizeof(buf);
    if (RegGetValueW(HKEY_CURRENT_USER, L"Software\\yzip", value, RRF_RT_REG_SZ, nullptr, buf, &cb) != ERROR_SUCCESS)
        return L"";
    std::wstring s = L";" + std::wstring(buf) + L";";
    CharLowerBuffW(&s[0], (DWORD)s.size());
    return s;
}

// formats.stem + unique_dir: "C:\a\Kings.zip" -> "C:\a\Kings", or "C:\a\Kings (2)" when that name is taken;
// a volume set is named without its volume part: "x.part1.rar", "x.vol1.egg" -> "x"
static std::wstring extract_dir(const std::wstring& arc) {
    std::wstring dir = arc.substr(0, last_sep(arc) + 1), name = file_name(arc), low = name;
    CharLowerBuffW(&low[0], (DWORD)low.size());
    std::wstring stem;
    for (const wchar_t* e : {L".tar.gz", L".tar.bz2", L".tar.xz", L".tar.zst"}) {
        size_t n = wcslen(e);
        if (low.size() > n && low.compare(low.size() - n, n, e) == 0)
            stem = name.substr(0, name.size() - n);
    }
    for (const wchar_t* e : {L".rar", L".egg"}) {  // ".partN.rar" / ".volN.egg"
        size_t n = wcslen(e);
        if (stem.empty() && low.size() > n && low.compare(low.size() - n, n, e) == 0) {
            size_t dot = low.find_last_of(L'.', low.size() - n - 1);
            std::wstring mid = dot == std::wstring::npos ? L"" : low.substr(dot + 1, low.size() - n - dot - 1);
            const wchar_t* word = e[1] == L'r' ? L"part" : L"vol";
            size_t w = wcslen(word);
            if (mid.size() > w && mid.compare(0, w, word) == 0 &&
                mid.find_first_not_of(L"0123456789", w) == std::wstring::npos)
                stem = name.substr(0, dot);
        }
    }
    if (stem.empty())
        stem = file_name(strip_ext(arc));
    std::wstring out = dir + stem;
    for (int i = 2; exists(out); i++)
        out = dir + stem + L" (" + std::to_wstring(i) + L")";
    return out;
}

static std::wstring escape_amp(const std::wstring& s) {
    std::wstring esc;
    for (wchar_t c : s) {
        if (c == L'&')
            esc += L'&';
        esc += c;
    }
    return esc;
}

// 0: not something yzip extracts, 1: an archive, 2: a disk image / installer yzip can extract
static int extract_kind(const std::wstring& p, const std::wstring& archives, const std::wstring& images) {
    std::wstring name = file_name(p);
    size_t dot = name.find_last_of(L'.');
    if (dot == std::wstring::npos || dot == 0 || is_dir(p))
        return 0;
    std::wstring ext = L";" + name.substr(dot) + L";";
    CharLowerBuffW(&ext[0], (DWORD)ext.size());
    return archives.find(ext) != std::wstring::npos ? 1 : images.find(ext) != std::wstring::npos ? 2 : 0;
}

// 32bpp top-down DIB with alpha, which is what menus need for a crisp icon
static HBITMAP icon_bitmap(const std::wstring& src) {
    if (src.empty())
        return nullptr;
    int cx = GetSystemMetrics(SM_CXSMICON), cy = GetSystemMetrics(SM_CYSMICON);
    HICON icon = nullptr;
    if (FAILED(SHDefExtractIconW(src.c_str(), 0, 0, nullptr, &icon, MAKELONG(0, cx))) || !icon)
        return nullptr;
    BITMAPINFO bi = {};
    bi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
    bi.bmiHeader.biWidth = cx;
    bi.bmiHeader.biHeight = -cy;
    bi.bmiHeader.biPlanes = 1;
    bi.bmiHeader.biBitCount = 32;
    bi.bmiHeader.biCompression = BI_RGB;
    void* bits = nullptr;
    HDC dc = CreateCompatibleDC(nullptr);
    HBITMAP bmp = CreateDIBSection(dc, &bi, DIB_RGB_COLORS, &bits, nullptr, 0);
    if (bmp) {
        HGDIOBJ old = SelectObject(dc, bmp);
        DrawIconEx(dc, 0, 0, icon, cx, cy, 0, nullptr, DI_NORMAL);
        SelectObject(dc, old);
    }
    DeleteDC(dc);
    DestroyIcon(icon);
    return bmp;
}

static bool write_utf8(const std::wstring& path, const std::wstring& text) {
    int n = WideCharToMultiByte(CP_UTF8, 0, text.c_str(), (int)text.size(), nullptr, 0, nullptr, nullptr);
    std::string s(n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, text.c_str(), (int)text.size(), &s[0], n, nullptr, nullptr);
    HANDLE h = CreateFileW(path.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE)
        return false;
    DWORD w = 0;
    BOOL ok = WriteFile(h, s.data(), (DWORD)s.size(), &w, nullptr);
    CloseHandle(h);
    return ok && w == s.size();
}

class YzipMenu : public IShellExtInit, public IContextMenu {
    long refs_ = 1;
    std::vector<std::wstring> paths_;
    std::wstring out_;
    std::wstring into_;  // one archive: the folder "폴더에 압축 풀기" names and extracts into
    std::vector<int> cmds_;  // command of each menu offset
    HBITMAP bmp_ = nullptr;

public:
    YzipMenu() { InterlockedIncrement(&g_refs); }
    ~YzipMenu() {
        if (bmp_)
            DeleteObject(bmp_);
        InterlockedDecrement(&g_refs);
    }

    IFACEMETHODIMP QueryInterface(REFIID riid, void** ppv) {
        static const QITAB qit[] = {QITABENT(YzipMenu, IShellExtInit), QITABENT(YzipMenu, IContextMenu), {0}};
        return QISearch(this, qit, riid, ppv);
    }
    IFACEMETHODIMP_(ULONG) AddRef() { return InterlockedIncrement(&refs_); }
    IFACEMETHODIMP_(ULONG) Release() {
        long r = InterlockedDecrement(&refs_);
        if (!r)
            delete this;
        return r;
    }

    IFACEMETHODIMP Initialize(PCIDLIST_ABSOLUTE, IDataObject* data, HKEY) {
        if (!data)
            return E_INVALIDARG;  // folder background: nothing selected
        paths_.clear();
        FORMATETC fe = {CF_HDROP, nullptr, DVASPECT_CONTENT, -1, TYMED_HGLOBAL};
        STGMEDIUM stg;
        if (FAILED(data->GetData(&fe, &stg)))
            return E_INVALIDARG;
        HDROP drop = (HDROP)GlobalLock(stg.hGlobal);
        if (drop) {
            UINT n = DragQueryFileW(drop, 0xFFFFFFFF, nullptr, 0);
            for (UINT i = 0; i < n; i++) {
                UINT len = DragQueryFileW(drop, i, nullptr, 0);
                std::wstring p(len, L'\0');
                DragQueryFileW(drop, i, &p[0], len + 1);
                paths_.push_back(p);
            }
            GlobalUnlock(stg.hGlobal);
        }
        ReleaseStgMedium(&stg);
        return paths_.empty() ? E_INVALIDARG : S_OK;
    }

    IFACEMETHODIMP QueryContextMenu(HMENU menu, UINT index, UINT first, UINT, UINT flags) {
        if (flags & CMF_DEFAULTONLY)
            return MAKE_HRESULT(SEVERITY_SUCCESS, 0, 0);
        if (paths_.empty() || read_command().empty())
            return MAKE_HRESULT(SEVERITY_SUCCESS, 0, 0);
        out_ = output_path(paths_);
        cmds_.clear();
        std::vector<std::wstring> labels;
        into_.clear();
        std::wstring arc_exts = ext_list(L"ArchiveExts"), img_exts = ext_list(L"ImageExts");
        bool extractable = true, archives = true;  // every item extractable / every item an archive proper
        for (auto& p : paths_) {
            int k = extract_kind(p, arc_exts, img_exts);
            extractable = extractable && k != 0;
            archives = archives && k == 1;
        }
        if (extractable) {
            cmds_.push_back(CMD_HERE);
            labels.push_back(L"여기에 압축 풀기");
            cmds_.push_back(CMD_FOLDER);
            if (paths_.size() == 1) {  // like Bandizip's "Kings (2)\ 에 풀기": the real folder, numbered if taken
                into_ = extract_dir(paths_[0]);
                std::wstring n = file_name(into_);
                if (n.size() > 52)
                    n = n.substr(0, 24) + L"…" + n.substr(n.size() - 24);
                labels.push_back(escape_amp(n) + L"\\에 압축 풀기");
            } else {
                labels.push_back(L"각각의 폴더에 압축 풀기");
            }
        }
        if (!out_.empty() && !archives) {  // archives get the extract items only
            cmds_.push_back(CMD_COMPRESS);
            labels.push_back(menu_label(out_));
            cmds_.push_back(CMD_DIALOG);
            labels.push_back(L"yzip으로 압축하기(&W)...");
        }
        if (cmds_.empty())
            return MAKE_HRESULT(SEVERITY_SUCCESS, 0, 0);
        if (!bmp_)
            bmp_ = icon_bitmap(icon_source());
        for (UINT i = 0; i < cmds_.size(); i++) {
            MENUITEMINFOW mii = {sizeof(mii)};
            mii.fMask = MIIM_STRING | MIIM_ID | MIIM_FTYPE | (bmp_ ? MIIM_BITMAP : 0);
            mii.fType = MFT_STRING;
            mii.wID = first + i;
            mii.dwTypeData = &labels[i][0];
            mii.hbmpItem = bmp_;
            if (!InsertMenuItemW(menu, index + i, TRUE, &mii))
                return HRESULT_FROM_WIN32(GetLastError());
        }
        return MAKE_HRESULT(SEVERITY_SUCCESS, 0, (USHORT)cmds_.size());
    }

    // which command (CMD_*) from a menu offset or a verb string, -1 if not ours
    int command_of(LPCMINVOKECOMMANDINFO pici) const {
        bool unicode = pici->cbSize == sizeof(CMINVOKECOMMANDINFOEX) && (pici->fMask & CMIC_MASK_UNICODE);
        if (unicode && HIWORD(((CMINVOKECOMMANDINFOEX*)pici)->lpVerbW)) {
            for (int i = 0; i < N_CMDS; i++)
                if (!lstrcmpiW(((CMINVOKECOMMANDINFOEX*)pici)->lpVerbW, VERBS_W[i]))
                    return i;
            return -1;
        }
        if (HIWORD(pici->lpVerb)) {
            for (int i = 0; i < N_CMDS; i++)
                if (!lstrcmpiA(pici->lpVerb, VERBS_A[i]))
                    return i;
            return -1;
        }
        UINT off = LOWORD(pici->lpVerb);
        return off < cmds_.size() ? cmds_[off] : -1;
    }

    IFACEMETHODIMP InvokeCommand(LPCMINVOKECOMMANDINFO pici) {
        int which = command_of(pici);
        if (which < 0)
            return E_FAIL;
        std::wstring cmd = read_command();
        if (cmd.empty() || paths_.empty())
            return E_FAIL;
        if (which == CMD_COMPRESS && out_.empty())
            out_ = output_path(paths_);
        if (which == CMD_COMPRESS && out_.empty())
            return E_FAIL;

        wchar_t tmp[MAX_PATH];
        GetTempPathW(MAX_PATH, tmp);
        wchar_t list[MAX_PATH];
        StringCchPrintfW(list, MAX_PATH, L"%syzip_sel_%lu_%llu.txt", tmp, GetCurrentProcessId(), GetTickCount64());
        std::wstring text;
        for (auto& p : paths_)
            text += p + L"\n";
        if (!write_utf8(list, text))
            return E_FAIL;

        std::wstring line = which == CMD_COMPRESS ? cmd + L" --archive-list \"" + list + L"\" --out \"" + out_ + L"\""
                          : which == CMD_DIALOG   ? cmd + L" --compress-dialog-list \"" + list + L"\""
                          : which == CMD_HERE     ? cmd + L" --extract-list here \"" + list + L"\""
                                                  : cmd + L" --extract-list folder \"" + list + L"\"" +
                                                        (into_.empty() ? L"" : L" --out \"" + into_ + L"\"");
        std::wstring dir = paths_[0].substr(0, last_sep(paths_[0]) + 1);
        STARTUPINFOW si = {sizeof(si)};
        PROCESS_INFORMATION pi = {};
        if (!CreateProcessW(nullptr, &line[0], nullptr, nullptr, FALSE, 0, nullptr, dir.c_str(), &si, &pi)) {
            DeleteFileW(list);
            return HRESULT_FROM_WIN32(GetLastError());
        }
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        return S_OK;
    }

    IFACEMETHODIMP GetCommandString(UINT_PTR id, UINT type, UINT*, LPSTR name, UINT cch) {
        if (id >= cmds_.size())
            return E_INVALIDARG;
        int c = cmds_[id];
        static const wchar_t* const HELP_W[] = {L"선택한 항목을 ZIP 파일로 바로 압축합니다", L"형식과 암호 등을 골라 압축합니다",
                                                L"압축 파일이 있는 폴더에 풉니다", L"압축 파일 이름의 폴더에 풉니다"};
        static const char* const HELP_A[] = {"Compress the selection to a ZIP file", "Choose format and options, then compress",
                                             "Extract next to the archive", "Extract into a folder named after the archive"};
        switch (type) {
        case GCS_VERBW:
            return StringCchCopyW((LPWSTR)name, cch, VERBS_W[c]);
        case GCS_VERBA:
            return StringCchCopyA(name, cch, VERBS_A[c]);
        case GCS_HELPTEXTW:
            return StringCchCopyW((LPWSTR)name, cch, HELP_W[c]);
        case GCS_HELPTEXTA:
            return StringCchCopyA(name, cch, HELP_A[c]);
        case GCS_VALIDATEW:
        case GCS_VALIDATEA:
            return S_OK;
        }
        return E_NOTIMPL;
    }
};

class Factory : public IClassFactory {
    long refs_ = 1;

public:
    IFACEMETHODIMP QueryInterface(REFIID riid, void** ppv) {
        static const QITAB qit[] = {QITABENT(Factory, IClassFactory), {0}};
        return QISearch(this, qit, riid, ppv);
    }
    IFACEMETHODIMP_(ULONG) AddRef() { return InterlockedIncrement(&refs_); }
    IFACEMETHODIMP_(ULONG) Release() {
        long r = InterlockedDecrement(&refs_);
        if (!r)
            delete this;
        return r;
    }
    IFACEMETHODIMP CreateInstance(IUnknown* outer, REFIID riid, void** ppv) {
        *ppv = nullptr;
        if (outer)
            return CLASS_E_NOAGGREGATION;
        YzipMenu* m = new (std::nothrow) YzipMenu();
        if (!m)
            return E_OUTOFMEMORY;
        HRESULT hr = m->QueryInterface(riid, ppv);
        m->Release();
        return hr;
    }
    IFACEMETHODIMP LockServer(BOOL lock) {
        lock ? InterlockedIncrement(&g_refs) : InterlockedDecrement(&g_refs);
        return S_OK;
    }
};

BOOL APIENTRY DllMain(HMODULE mod, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        g_inst = mod;
        DisableThreadLibraryCalls(mod);
    }
    return TRUE;
}

STDAPI DllGetClassObject(REFCLSID clsid, REFIID riid, void** ppv) {
    *ppv = nullptr;
    if (!IsEqualCLSID(clsid, CLSID_YzipMenu))
        return CLASS_E_CLASSNOTAVAILABLE;
    Factory* f = new (std::nothrow) Factory();
    if (!f)
        return E_OUTOFMEMORY;
    HRESULT hr = f->QueryInterface(riid, ppv);
    f->Release();
    return hr;
}

STDAPI DllCanUnloadNow() { return g_refs == 0 ? S_OK : S_FALSE; }
