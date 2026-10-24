"""End-to-end test of the BUILT exe through the exact command lines Explorer runs (read back from HKCU).

Headless: QT_QPA_PLATFORM=offscreen, YZIP_NO_OPEN (don't open Explorer), YZIP_AUTOACCEPT (accept the compress
dialog and quit after it), LOCALAPPDATA redirected so error.log is isolated. Startup timing is real (no scaling).
Usage: python test_exe_verbs.py [--keep-registered]
"""
import hashlib
import os
import subprocess
import sys
import tempfile
import time
import winreg

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(HERE, "dist", "yzip", "yzip.exe")
sys.path.insert(0, HERE)
import ziplib  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))


def reg_cmd(sub):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub + r"\command") as k:
        return winreg.QueryValueEx(k, "")[0]


def key_exists(sub):
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub))
        return True
    except FileNotFoundError:
        return False


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def env_for(local):
    return dict(os.environ, QT_QPA_PLATFORM="offscreen", YZIP_NO_OPEN="1", YZIP_AUTOACCEPT="1", LOCALAPPDATA=local)


def log_text(local):
    p = os.path.join(local, "yzip", "error.log")
    return open(p, encoding="utf-8").read() if os.path.exists(p) else ""


assert os.path.exists(EXE), "build first: pyinstaller ... --name yzip"


def registered_exe():
    """The exe the user's Explorer menu points at now (an installed yzip), to put it back afterwards."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\yzip") as k:
            exe = winreg.QueryValueEx(k, "Command")[0].strip().strip('"')
        return exe if os.path.isfile(exe) else None
    except OSError:
        return None


BEFORE = registered_exe()

# ---- registry: migrate stale ZipKit.* verbs, register yzip.* for this exe ----
legacy = [r"Software\Classes\*\shell\ZipKit.Compress", r"Software\Classes\Directory\shell\ZipKit.Compress",
          r"Software\Classes\SystemFileAssociations\.zip\shell\ZipKit.Here",
          r"Software\Classes\SystemFileAssociations\.zip\shell\ZipKit.Folder"]
had_legacy = [k for k in legacy if key_exists(k)]
r = subprocess.run([EXE, "--register-shell"], timeout=60)
check("exe --register-shell exits 0 (no UI)", r.returncode == 0, r.returncode)
check(f"stale ZipKit.* verbs removed (had {len(had_legacy)})", not any(key_exists(k) for k in legacy))
KEY_COMPRESS = r"Software\Classes\*\shell\yzip.Compress"
KEY_DIR = r"Software\Classes\Directory\shell\yzip.Compress"
KEY_HERE = r"Software\Classes\SystemFileAssociations\.zip\shell\yzip.Here"
KEY_FOLDER = r"Software\Classes\SystemFileAssociations\.zip\shell\yzip.Folder"
CLSID = "{7E3B1C52-9A4D-4F1E-B6C2-3D8A5F0E9C71}"
cmds = {k: reg_cmd(k) for k in (KEY_HERE, KEY_FOLDER)}
cmds[KEY_COMPRESS] = f'"{EXE}" --archive "%1"'  # fallback path when no shell extension is installed
check("extract verbs point at dist\\yzip\\yzip.exe", all(EXE.lower() in c.lower() for c in cmds.values()), cmds)
check("static compress verbs replaced by the shell extension", not key_exists(KEY_COMPRESS) and not key_exists(KEY_DIR))
with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\CLSID\{CLSID}\InprocServer32") as k:
    dll = winreg.QueryValueEx(k, "")[0]
with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\yzip") as k:
    app_cmd = winreg.QueryValueEx(k, "Command")[0]
check("shell extension DLL installed outside dist (rebuild-safe)", os.path.exists(dll) and "dist" not in dll.lower(), dll)
check("shell extension launches dist\\yzip\\yzip.exe", EXE.lower() in app_cmd.lower(), app_cmd)
PROBE = os.path.join(HERE, "shell", "build", "probe.exe")


def probe(mode, paths, env=None):
    r = subprocess.run([PROBE, mode] + paths, capture_output=True, timeout=60, env=env)
    out = r.stdout.decode("utf-8", "replace")
    return r.returncode, [ln.split("\t")[1:] for ln in out.splitlines() if ln.startswith("ITEM\t")], out


def yzip_item(items):  # by verb: Bandizip, if installed, adds an item with identical text
    return [it for it in items if len(it) > 2 and it[2] == "yzip.compress"]


def wait_done(out, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        if os.path.exists(out) and not os.path.exists(out + ".partial"):
            time.sleep(0.5)
            return True
        time.sleep(0.1)
    return False


def run_verb(key, path, env, timeout=60):
    line = cmds[key].replace("%1", path)
    return subprocess.run(line, env=env, capture_output=True, timeout=timeout)


def build_zip(t):
    src = os.path.join(t, "src")
    os.makedirs(os.path.join(src, "하위 폴더"))
    files = {}
    for rel, data in (("a.txt", b"alpha" * 1000), ("b.bin", os.urandom(80_000)), (os.path.join("하위 폴더", "c.txt"), "다".encode() * 500)):
        p = os.path.join(src, rel)
        with open(p, "wb") as f:
            f.write(data)
        files[rel] = p
    z = os.path.join(t, "pack.zip")
    ziplib.create_zip([files["a.txt"], files["b.bin"], os.path.join(src, "하위 폴더")], z)
    return files, z


# ---- extract verbs (no dialog) ----
with tempfile.TemporaryDirectory(prefix="yze_") as t:
    local = os.path.join(t, "local")
    files, z = build_zip(t)
    t0 = time.time()
    r = run_verb(KEY_HERE, z, env_for(local))
    check("exe '여기에 압축 풀기': exit 0", r.returncode == 0, (r.returncode, r.stderr[-300:]))
    out = os.path.join(t, "pack")
    check("exe '여기에 압축 풀기': smart folder pack/ with identical files",
          os.path.exists(os.path.join(out, "b.bin")) and sha(os.path.join(out, "b.bin")) == sha(files["b.bin"])
          and os.path.exists(os.path.join(out, "하위 폴더", "c.txt")), os.listdir(t))
    check("exe '여기에 압축 풀기': error.log empty", log_text(local) == "", log_text(local)[-300:])
    print(f"     ({time.time() - t0:.1f}s incl. startup)")

with tempfile.TemporaryDirectory(prefix="yze_") as t:
    local = os.path.join(t, "local")
    files, z = build_zip(t)
    r = run_verb(KEY_FOLDER, z, env_for(local))
    out = os.path.join(t, "pack")
    check("exe '폴더에 압축 풀기': exit 0 + files identical",
          r.returncode == 0 and os.path.exists(os.path.join(out, "a.txt")) and sha(os.path.join(out, "a.txt")) == sha(files["a.txt"]),
          (r.returncode, os.listdir(t)))
    check("exe '폴더에 압축 풀기': error.log empty", log_text(local) == "", log_text(local)[-300:])

# ---- compress verbs: single file, folder ----
with tempfile.TemporaryDirectory(prefix="yze_") as t:
    local = os.path.join(t, "local")
    files, z = build_zip(t)
    src = os.path.join(t, "src")
    r = run_verb(KEY_COMPRESS, files["b.bin"], env_for(local), timeout=90)
    out = os.path.join(src, "b.zip")
    ok = os.path.exists(out)
    check("exe '압축' on one file: exit 0 and b.zip created", r.returncode == 0 and ok, (r.returncode, os.listdir(src), r.stderr[-300:]))
    if ok:
        with ziplib.Archive(out) as a:
            check("exe '압축' on one file: archive is valid", a.test() == [])
    check("exe '압축' on one file: error.log empty", log_text(local) == "", log_text(local)[-300:])

    r = run_verb(KEY_COMPRESS, src, env_for(local), timeout=90)
    out = src + ".zip"
    check("exe '압축' on a folder: exit 0 and src.zip created", r.returncode == 0 and os.path.exists(out), (r.returncode, os.listdir(t)))
    if os.path.exists(out):
        with ziplib.Archive(out) as a:
            names = sorted(e.name for e in a.entries() if not e.is_dir)
            check("exe '압축' on a folder: includes Korean subfolder file", any("c.txt" in n for n in names), names)

# ---- shell extension, through the shell's own context-menu composition (what Explorer calls) ----
with tempfile.TemporaryDirectory(prefix="yze_") as t:
    local = os.path.join(t, "local")
    files, z = build_zip(t)
    src = os.path.join(t, "src")
    v12 = os.path.join(t, "v1.2")
    os.makedirs(v12)
    amp = os.path.join(t, "R&D 보고서.txt")
    open(amp, "w").close()
    sel = os.path.join(t, "선택 폴더")
    os.makedirs(sel)
    trio = []
    for i in range(3):
        p = os.path.join(sel, f"f{i}.txt")
        with open(p, "w") as f:
            f.write(f"file {i} " * 300)
        trio.append(p)
    S = "으로 압축하기 - &yzip"
    cases = [("one file", [files["b.bin"]], "b.zip" + S),
             ("folder", [src], "src.zip" + S),
             ("folder with a dot keeps its full name", [v12], "v1.2.zip" + S),
             ("'&' is escaped", [amp], "R&&D 보고서.zip" + S),
             ("3 files -> their folder's name", trio, "선택 폴더.zip" + S),
             ("existing name -> (2)", [z], "pack (2).zip" + S)]
    for name, paths, want in cases:
        code, items, raw = probe("list", paths)
        mine = yzip_item(items)
        check(f"menu label, {name}: '{want}'", code == 0 and len(mine) == 1 and mine[0][0] == want, (code, mine, raw[-300:]))
    check("menu item carries the yzip icon", any(len(it) > 1 and it[1] == "[icon]" for it in yzip_item(probe("list", [files["b.bin"]])[1])))
    dlg_items = [it for it in probe("list", trio)[1] if len(it) > 2 and it[2] == "yzip.compressdlg"]
    check("second item 'yzip으로 압축하기(&W)...' (opens the compress window)",
          [it[0] for it in dlg_items] == ["yzip으로 압축하기(&W)..."], dlg_items)
    code, items, _ = probe("defaultonly", [files["b.bin"]])
    check("CMF_DEFAULTONLY: handler adds nothing", not yzip_item(items), items)

    env = env_for(local)
    before = set(os.listdir(os.environ.get("TEMP")))
    code, _, raw = probe("invoke", [files["b.bin"]], env)
    out = os.path.join(src, "b.zip")
    check("invoke on one file: b.zip created", code == 0 and wait_done(out), (code, raw[-300:], os.listdir(src)))
    if os.path.exists(out):
        with ziplib.Archive(out) as a:
            check("invoke on one file: archive valid and identical", a.test() == [] and [e.name for e in a.entries()] == ["b.bin"])
        import zipfile
        with zipfile.ZipFile(out) as zf:  # yzip's writer streams with data descriptors; proves yzip (not Bandizip) ran
            check("invoke on one file: written by yzip", all(i.flag_bits & 0x08 for i in zf.infolist()),
                  [hex(i.flag_bits) for i in zf.infolist()])
    code, _, raw = probe("invoke", trio, env)
    out = os.path.join(sel, "선택 폴더.zip")
    check("invoke on 3 files: ONE archive named after the folder", code == 0 and wait_done(out)
          and [f for f in os.listdir(sel) if f.endswith(".zip")] == ["선택 폴더.zip"], (code, os.listdir(sel)))
    if os.path.exists(out):
        with ziplib.Archive(out) as a:
            got = sorted(e.name for e in a.entries() if not e.is_dir)
        check("invoke on 3 files: archive has all 3", got == ["f0.txt", "f1.txt", "f2.txt"], got)
    # "yzip으로 압축하기..." -> compress window (auto-accepted with format 7z) -> 7z made by the built exe
    code, _, raw = probe("invokedlg", [src], dict(env, YZIP_AUTOACCEPT="7z"))
    out7 = src + ".7z"
    check("invoke 'yzip으로 압축하기...': window -> src.7z created by the exe", code == 0 and wait_done(out7, 120),
          (code, raw[-300:], os.listdir(t)))
    if os.path.exists(out7):
        import formats  # noqa: E402
        with formats.open_any(out7) as a:
            names = sorted(e.name for e in a.entries() if not e.is_dir)
            ok7 = a.test() == []
        check("exe-made 7z is valid and complete", ok7 and any("c.txt" in n for n in names), names)
    time.sleep(1)
    left = [f for f in set(os.listdir(os.environ.get("TEMP"))) - before if f.startswith("yzip_sel_")]
    check("selection list files cleaned up", not left, left)
    check("invoke: error.log empty", log_text(local) == "", log_text(local)[-300:])

# ---- extract verbs on other formats (bundled py7zr / zstd / lzma in the exe) ----
with tempfile.TemporaryDirectory(prefix="yze_") as t:
    import formats  # noqa: E402
    local = os.path.join(t, "local")
    files, _ = build_zip(t)
    src = os.path.join(t, "src")
    for fid in ("7z", "tgz", "txz", "tzst"):
        arc = os.path.join(t, "pk_" + fid + formats.BY_ID[fid].ext)
        formats.create(fid, [src], arc)
        ext = ".gz" if fid == "tgz" else ".xz" if fid == "txz" else ".zst" if fid == "tzst" else ".7z"
        key = rf"Software\Classes\SystemFileAssociations\{ext}\shell\yzip.Folder"
        r = subprocess.run(reg_cmd(key).replace("%1", arc), env=env_for(local), capture_output=True, timeout=120)
        got = os.path.join(t, formats.stem(arc), "src", "b.bin")
        check(f"exe '폴더에 압축 풀기' on {os.path.basename(arc)}: files identical",
              r.returncode == 0 and os.path.exists(got) and sha(got) == sha(files["b.bin"]), (r.returncode, os.listdir(t)))
    check("other-format extracts: error.log empty", log_text(local) == "", log_text(local)[-300:])

# ---- fallback without the shell extension: N processes at once must still make ONE archive ----
with tempfile.TemporaryDirectory(prefix="yze_") as t:
    local = os.path.join(t, "local")
    folder = os.path.join(t, "선택 폴더")
    os.makedirs(folder)
    names = []
    for i in range(3):
        p = os.path.join(folder, f"f{i}.txt")
        with open(p, "w") as f:
            f.write(f"file {i} " * 300)
        names.append(p)
    procs = [subprocess.Popen(cmds[KEY_COMPRESS].replace("%1", p), env=env_for(local), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE) for p in names]
    codes = []
    for p in procs:
        try:
            p.communicate(timeout=120)
            codes.append(p.returncode)
        except subprocess.TimeoutExpired:
            p.kill()
            codes.append("timeout")
    zips = [f for f in os.listdir(folder) if f.lower().endswith(".zip")]
    check("multi-select x3 (real exe startup spread): all processes exit 0", all(c == 0 for c in codes), codes)
    check("multi-select x3: exactly ONE archive created", len(zips) == 1, zips)
    if len(zips) == 1:
        with ziplib.Archive(os.path.join(folder, zips[0])) as a:
            got = sorted(e.name for e in a.entries() if not e.is_dir)
        check("multi-select x3: that archive contains all 3 files", got == ["f0.txt", "f1.txt", "f2.txt"], got)
    check("multi-select x3: error.log empty", log_text(local) == "", log_text(local)[-300:])

if "--keep-registered" not in sys.argv:
    subprocess.run([EXE, "--unregister-shell"], timeout=60)
    check("cleanup: yzip verbs and shell extension unregistered",
          not key_exists(KEY_HERE) and not key_exists(rf"Software\Classes\CLSID\{CLSID}") and not key_exists(r"Software\yzip"))
    if BEFORE:  # the user's menu was registered before this run: restore it to the same exe
        subprocess.run([BEFORE, "--register-shell"], timeout=60)
        check("cleanup: the user's own registration is restored", registered_exe() == BEFORE, registered_exe())
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
