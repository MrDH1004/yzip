#![deny(unsafe_op_in_unsafe_fn)]

use std::ffi::{CStr, CString};
use std::fs::File;
use std::io::{BufReader, Read, Seek};
use std::os::raw::{c_char, c_int};
use std::path::Path;
use std::sync::{Mutex, OnceLock};
use std::time::UNIX_EPOCH;

use serde::Serialize;

type Result<T> = std::result::Result<T, NativeError>;

const OK: c_int = 0;
const ERR_ARGUMENT: c_int = 1;
const ERR_FAILURE: c_int = 2;
const ERR_PASSWORD: c_int = 3;
const ERR_PARTIAL: c_int = 4;

static LAST_ERROR: OnceLock<Mutex<Vec<u8>>> = OnceLock::new();
static VERSION: &[u8] = b"yzip-native/0.1\0";

#[derive(Debug)]
struct NativeError {
    code: c_int,
    message: String,
}

impl NativeError {
    fn argument(message: impl Into<String>) -> Self {
        Self {
            code: ERR_ARGUMENT,
            message: message.into(),
        }
    }

    fn failure(message: impl Into<String>) -> Self {
        Self {
            code: ERR_FAILURE,
            message: message.into(),
        }
    }

    fn password(message: impl Into<String>) -> Self {
        Self {
            code: ERR_PASSWORD,
            message: message.into(),
        }
    }

    fn partial(message: impl Into<String>) -> Self {
        Self {
            code: ERR_PARTIAL,
            message: message.into(),
        }
    }
}

#[derive(Serialize)]
struct JsonEntry {
    name: String,
    is_dir: bool,
    size: u64,
    csize: u64,
    method: String,
    encrypted: bool,
    mtime: i64,
}

trait ReadSeek: Read + Seek {}
impl<T: Read + Seek> ReadSeek for T {}
type EggReader = Box<dyn ReadSeek>;

fn error_cell() -> &'static Mutex<Vec<u8>> {
    LAST_ERROR.get_or_init(|| Mutex::new(Vec::new()))
}

fn set_error(error: &NativeError) {
    let mut bytes = error.message.as_bytes().to_vec();
    bytes.retain(|byte| *byte != 0);
    bytes.push(0);
    if let Ok(mut current) = error_cell().lock() {
        *current = bytes;
    }
}

fn c_string<'a>(ptr: *const c_char, label: &str) -> Result<&'a str> {
    if ptr.is_null() {
        return Err(NativeError::argument(format!("{label} is null")));
    }
    // SAFETY: the C ABI requires NUL-terminated UTF-8 strings owned by the caller.
    let raw = unsafe { CStr::from_ptr(ptr) };
    raw.to_str()
        .map_err(|_| NativeError::argument(format!("{label} is not UTF-8")))
}

fn password(ptr: *const c_char) -> Result<Option<String>> {
    if ptr.is_null() {
        return Ok(None);
    }
    Ok(Some(c_string(ptr, "password")?.to_string()))
}

fn open_egg(path: &str) -> Result<unegg::archive::EggArchive<EggReader>> {
    let source = Path::new(path);
    if let Some(reader) = unegg::volume::MultiVolumeReader::try_open(source).map_err(egg_error)? {
        let reader: EggReader = Box::new(reader);
        return unegg::archive::EggArchive::open(reader).map_err(egg_error);
    }
    let file = File::open(source)
        .map_err(|error| egg_error(unegg::error::EggError::CantOpenFile(error)))?;
    let reader: EggReader = Box::new(BufReader::new(file));
    unegg::archive::EggArchive::open(reader).map_err(egg_error)
}

fn open_alz(path: &str) -> Result<unalz::archive::AlzArchive> {
    unalz::archive::AlzArchive::open(path).map_err(alz_error)
}

fn filetime_to_unix(filetime: Option<u64>) -> i64 {
    const TICKS_PER_SECOND: u64 = 10_000_000;
    const WINDOWS_EPOCH: u64 = 11_644_473_600;
    let Some(value) = filetime else { return 0 };
    let seconds = value / TICKS_PER_SECOND;
    seconds.saturating_sub(WINDOWS_EPOCH).min(i64::MAX as u64) as i64
}

fn alz_time(value: u32) -> i64 {
    let Some(time) = unalz::dostime::dos_datetime_to_systime(value) else {
        return 0;
    };
    time.duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs().min(i64::MAX as u64) as i64)
        .unwrap_or(0)
}

fn list_egg(path: &str) -> Result<String> {
    let archive = open_egg(path)?;
    let entries = archive
        .entries
        .iter()
        .map(|entry| JsonEntry {
            name: entry.file_name.clone(),
            is_dir: entry.is_directory(),
            size: entry.uncompressed_size,
            csize: entry
                .blocks
                .iter()
                .map(|block| u64::from(block.compressed_size))
                .sum(),
            method: entry
                .blocks
                .first()
                .map(|block| block.compression_method.name().to_string())
                .unwrap_or_else(|| "Store".to_string()),
            encrypted: entry.encrypt_info.is_some(),
            mtime: filetime_to_unix(entry.file_time),
        })
        .collect::<Vec<_>>();
    serde_json::to_string(&entries).map_err(|error| NativeError::failure(error.to_string()))
}

fn list_alz(path: &str) -> Result<String> {
    let archive = open_alz(path)?;
    let entries = archive
        .entries
        .iter()
        .map(|entry| JsonEntry {
            name: entry.file_name.clone(),
            is_dir: entry.is_directory(),
            size: entry.uncompressed_size,
            csize: entry.compressed_size,
            method: entry.compression_method.to_string(),
            encrypted: entry.is_encrypted(),
            mtime: alz_time(entry.file_time_date),
        })
        .collect::<Vec<_>>();
    serde_json::to_string(&entries).map_err(|error| NativeError::failure(error.to_string()))
}

fn list(kind: &str, path: &str) -> Result<String> {
    match kind {
        "egg" => list_egg(path),
        "alz" => list_alz(path),
        _ => Err(NativeError::argument(format!(
            "unsupported legacy format: {kind}"
        ))),
    }
}

fn extract(kind: &str, path: &str, destination: &str, password: Option<&str>) -> Result<()> {
    let dest = Path::new(destination);
    std::fs::create_dir_all(dest).map_err(|error| NativeError::failure(error.to_string()))?;
    match kind {
        "egg" => {
            let mut archive = open_egg(path)?;
            unegg::extract::extract_all(&mut archive, dest, password, false).map_err(egg_error)?;
            Ok(())
        }
        "alz" => {
            let mut archive = open_alz(path)?;
            let complete = unalz::extract::extract_all(&mut archive, dest, password, false, true)
                .map_err(alz_error)?;
            if complete {
                Ok(())
            } else {
                Err(NativeError::partial(
                    "ALZ archive is truncated or missing a volume",
                ))
            }
        }
        _ => Err(NativeError::argument(format!(
            "unsupported legacy format: {kind}"
        ))),
    }
}

fn egg_error(error: unegg::error::EggError) -> NativeError {
    let message = error.to_string();
    match error {
        unegg::error::EggError::PasswordNotSet
        | unegg::error::EggError::InvalidPassword
        | unegg::error::EggError::AuthenticationFailed => NativeError::password(message),
        _ => NativeError::failure(message),
    }
}

fn alz_error(error: unalz::error::AlzError) -> NativeError {
    let message = error.to_string();
    match error {
        unalz::error::AlzError::PasswordNotSet | unalz::error::AlzError::InvalidPassword => {
            NativeError::password(message)
        }
        _ => NativeError::failure(message),
    }
}

fn finish(error: Result<()>) -> c_int {
    match error {
        Ok(()) => OK,
        Err(error) => {
            set_error(&error);
            error.code
        }
    }
}

#[no_mangle]
pub extern "C" fn yzip_native_version() -> *const c_char {
    VERSION.as_ptr().cast()
}

#[no_mangle]
pub extern "C" fn yzip_native_last_error() -> *const c_char {
    match error_cell().lock() {
        Ok(current) if !current.is_empty() => current.as_ptr().cast(),
        _ => c"native backend error".as_ptr().cast(),
    }
}

#[no_mangle]
pub extern "C" fn yzip_native_free_string(value: *mut c_char) {
    if !value.is_null() {
        // SAFETY: only pointers returned by yzip_native_list are accepted.
        unsafe {
            drop(CString::from_raw(value));
        }
    }
}

#[no_mangle]
pub extern "C" fn yzip_native_list(
    kind: *const c_char,
    path: *const c_char,
    _password: *const c_char,
    output: *mut *mut c_char,
) -> c_int {
    if output.is_null() {
        let error = NativeError::argument("output is null");
        set_error(&error);
        return error.code;
    }
    // SAFETY: output was checked for null and points to caller-owned storage.
    unsafe {
        *output = std::ptr::null_mut();
    }
    let result = (|| {
        let kind = c_string(kind, "kind")?;
        let path = c_string(path, "path")?;
        let json = list(kind, path)?;
        let value =
            CString::new(json).map_err(|_| NativeError::failure("list contains a NUL byte"))?;
        // SAFETY: output is valid for the duration of this call.
        unsafe {
            *output = value.into_raw();
        }
        Ok(())
    })();
    finish(result)
}

#[no_mangle]
pub extern "C" fn yzip_native_extract(
    kind: *const c_char,
    path: *const c_char,
    destination: *const c_char,
    password_ptr: *const c_char,
) -> c_int {
    let result = (|| {
        let kind = c_string(kind, "kind")?;
        let path = c_string(path, "path")?;
        let destination = c_string(destination, "destination")?;
        let password = password(password_ptr)?;
        extract(kind, path, destination, password.as_deref())
    })();
    finish(result)
}
