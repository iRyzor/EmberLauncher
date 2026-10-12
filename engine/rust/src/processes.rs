use crate::{install::Installation, require, Error, Result};
use serde_json::json;
use std::{
    collections::BTreeMap,
    time::{Duration, Instant},
};
#[cfg(not(target_os = "linux"))]
use sysinfo::{Pid, ProcessStatus, ProcessesToUpdate, System};
use unicode_casefold::UnicodeCaseFold;
fn predicate(kind: u32, game: &[u8], windows: &[u8], candidate: &[u8]) -> bool {
    unsafe {
        ember_protocol::process::ember_process_check(
            kind,
            game.as_ptr(),
            game.len(),
            windows.as_ptr(),
            windows.len(),
            candidate.as_ptr(),
            candidate.len(),
        ) == 1
    }
}
fn norm(path: &str) -> Vec<u8> {
    path.replace('\\', "/")
        .case_fold()
        .collect::<String>()
        .into_bytes()
}
pub fn identity(pid: u32) -> Result<u64> {
    #[cfg(target_os = "linux")]
    {
        let raw = std::fs::read_to_string(format!("/proc/{pid}/stat"))?;
        let fields: Vec<_> = raw
            .rsplit_once(')')
            .ok_or_else(|| Error::new("Identidad inválida"))?
            .1
            .split_whitespace()
            .collect();
        require(fields.first() != Some(&"Z"), "El proceso terminó.")?;
        fields
            .get(19)
            .and_then(|v| v.parse().ok())
            .ok_or_else(|| Error::new("Identidad inválida"))
    }
    #[cfg(windows)]
    {
        use windows_sys::Win32::{Foundation::*, System::Threading::*};
        unsafe {
            let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
            require(!handle.is_null(), "No se pudo comprobar el proceso.")?;
            let mut creation: FILETIME = std::mem::zeroed();
            let mut exit: FILETIME = std::mem::zeroed();
            let mut kernel: FILETIME = std::mem::zeroed();
            let mut user: FILETIME = std::mem::zeroed();
            let mut status = 0;
            let valid = GetProcessTimes(handle, &mut creation, &mut exit, &mut kernel, &mut user)
                != 0
                && GetExitCodeProcess(handle, &mut status) != 0
                && status == 259;
            CloseHandle(handle);
            require(valid, "El proceso terminó.")?;
            return Ok((creation.dwHighDateTime as u64) << 32 | creation.dwLowDateTime as u64);
        }
    }
    #[cfg(target_os = "macos")]
    {
        unsafe {
            let mut info: libc::proc_bsdinfo = std::mem::zeroed();
            let size = std::mem::size_of_val(&info) as i32;
            require(
                libc::proc_pidinfo(
                    pid as i32,
                    libc::PROC_PIDTBSDINFO,
                    0,
                    (&mut info as *mut libc::proc_bsdinfo).cast(),
                    size,
                ) == size
                    && info.pbi_status != 5,
                "No se pudo comprobar el proceso.",
            )?;
            return Ok(info.pbi_start_tvsec * 1_000_000 + info.pbi_start_tvusec);
        }
    }
}
pub fn scan(installation: &Installation) -> Result<BTreeMap<u32, u64>> {
    let game = installation.game();
    let native = norm(&format!("{}/", game.display()));
    let windows = norm(&format!("{}/", installation.windows(&game)?));
    let mut found = BTreeMap::new();
    #[cfg(target_os = "linux")]
    {
        use std::os::unix::{ffi::OsStrExt, fs::MetadataExt};
        let prefix = [
            b"WINEPREFIX=".as_slice(),
            installation.prefix.as_os_str().as_bytes(),
        ]
        .concat();
        for entry in std::fs::read_dir("/proc")? {
            let path = entry?.path();
            let Some(pid) = path
                .file_name()
                .and_then(|v| v.to_str())
                .and_then(|v| v.parse::<u32>().ok())
            else {
                continue;
            };
            if path.metadata().map(|m| m.uid()).ok() != Some(unsafe { libc::getuid() }) {
                continue;
            }
            let Ok(environment) = std::fs::read(path.join("environ")) else {
                continue;
            };
            if !environment.split(|b| *b == 0).any(|v| v == prefix) {
                continue;
            }
            let Ok(command) = std::fs::read(path.join("cmdline")) else {
                continue;
            };
            let exe = command.split(|b| *b == 0).next().unwrap_or(&[]);
            if predicate(
                2,
                b"",
                b"",
                exe.rsplit(|b| *b == b'/').next().unwrap_or(exe),
            ) {
                continue;
            }
            let Ok(maps) = std::fs::read(path.join("maps")) else {
                continue;
            };
            if predicate(1, &[game.as_os_str().as_bytes(), b"/"].concat(), b"", &maps)
                || predicate(0, &native, &windows, &norm(&String::from_utf8_lossy(exe)))
            {
                if let Ok(start) = identity(pid) {
                    found.insert(pid, start);
                }
            }
        }
    }
    #[cfg(not(target_os = "linux"))]
    {
        let mut system = System::new_all();
        system.refresh_processes(ProcessesToUpdate::All, true);
        let current = system
            .process(Pid::from_u32(std::process::id()))
            .and_then(|p| p.user_id())
            .cloned();
        for (pid, process) in system.processes() {
            if process.user_id() != current.as_ref() || process.status() == ProcessStatus::Zombie {
                continue;
            }
            let Some(exe) = process.exe() else {
                continue;
            };
            let name = exe.file_name().unwrap_or_default().to_string_lossy();
            if predicate(2, b"", b"", name.as_bytes()) {
                continue;
            }
            if !cfg!(windows)
                && !process.environ().iter().any(|v| {
                    v.to_string_lossy() == format!("WINEPREFIX={}", installation.prefix.display())
                })
            {
                continue;
            }
            let mut candidates = vec![exe.to_string_lossy().to_string()];
            if let Some(first) = process.cmd().first() {
                candidates.push(first.to_string_lossy().to_string());
            }
            if cfg!(target_os = "macos")
                && matches!(
                    name.as_ref(),
                    "wine" | "wine64" | "wine-preloader" | "wine64-preloader"
                )
            {
                if let Some(p) = process.cmd().get(1) {
                    candidates.push(p.to_string_lossy().to_string());
                }
            }
            if candidates
                .iter()
                .any(|p| predicate(0, &native, &windows, &norm(p)))
            {
                if let Ok(start) = identity(pid.as_u32()) {
                    found.insert(pid.as_u32(), start);
                }
            }
        }
    }
    Ok(found)
}
pub fn idle(installation: &Installation) -> Result<()> {
    require(
        scan(installation)?.is_empty(),
        "Cierra el juego antes de modificar archivos.",
    )?;
    #[cfg(target_os = "linux")]
    {
        use std::os::unix::{ffi::OsStrExt, fs::MetadataExt};
        let prefix = [
            b"WINEPREFIX=".as_slice(),
            installation.prefix.as_os_str().as_bytes(),
        ]
        .concat();
        let mapped = [installation.game().as_os_str().as_bytes(), b"/"].concat();
        for entry in std::fs::read_dir("/proc")? {
            let path = entry?.path();
            if !path
                .file_name()
                .unwrap_or_default()
                .to_string_lossy()
                .bytes()
                .all(|b| b.is_ascii_digit())
            {
                continue;
            }
            if path.metadata().map(|m| m.uid()).ok() != Some(unsafe { libc::getuid() }) {
                continue;
            }
            let command = match std::fs::read(path.join("cmdline")) {
                Ok(c) => c.to_ascii_lowercase(),
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
                Err(_) => {
                    return Err(Error::new(
                        "No se pudo comprobar si los archivos están en uso.",
                    ))
                }
            };
            let candidate = [
                b"chaoszeronightmare".as_slice(),
                b"ssr-stove",
                b"stoveupdater",
                b"stove.exe",
            ]
            .iter()
            .any(|n| command.windows(n.len()).any(|p| p == *n));
            if candidate {
                match std::fs::read(path.join("environ")) {
                    Ok(e) => require(
                        !e.split(|b| *b == 0).any(|v| v == prefix),
                        "Cierra el juego o el cliente STOVE antes de modificar archivos.",
                    )?,
                    Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
                    Err(_) => {
                        return Err(Error::new(
                            "No se pudo comprobar si los archivos están en uso.",
                        ))
                    }
                }
            }
            match std::fs::read(path.join("maps")) {
                Ok(m) => require(
                    !m.windows(mapped.len()).any(|s| s == mapped),
                    "Los archivos del juego están en uso.",
                )?,
                Err(e) if candidate && e.kind() != std::io::ErrorKind::NotFound => {
                    return Err(Error::new(
                        "No se pudo comprobar si los archivos están en uso.",
                    ))
                }
                _ => (),
            }
        }
    }
    #[cfg(not(target_os = "linux"))]
    {
        let system = System::new_all();
        let uid = system
            .process(Pid::from_u32(std::process::id()))
            .and_then(|p| p.user_id());
        for process in system.processes().values() {
            let names: Vec<_> = process
                .cmd()
                .iter()
                .take(2)
                .map(|p| {
                    p.to_string_lossy()
                        .replace('\\', "/")
                        .rsplit('/')
                        .next()
                        .unwrap_or("")
                        .to_lowercase()
                })
                .collect();
            let name = process.name().to_string_lossy().to_lowercase();
            if (names
                .iter()
                .any(|n| matches!(n.as_str(), "stove.exe" | "stoveupdater.exe"))
                || matches!(name.as_str(), "stove.exe" | "stoveupdater.exe"))
                && process.user_id() == uid
                && (cfg!(windows)
                    || process.environ().iter().any(|e| {
                        e.to_string_lossy()
                            == format!("WINEPREFIX={}", installation.prefix.display())
                    }))
            {
                return Err(Error::new(
                    "Cierra el cliente STOVE antes de modificar archivos.",
                ));
            }
        }
    }
    Ok(())
}
pub struct Tracker {
    pub installation: Installation,
    pub baseline: BTreeMap<u32, u64>,
    pub owned: BTreeMap<u32, u64>,
    pub seen: bool,
    pub empty: Option<f64>,
    origin: Instant,
    pub finished: bool,
}
impl Tracker {
    pub fn new(installation: Installation) -> Result<Self> {
        let baseline = scan(&installation)?;
        Ok(Self {
            installation,
            baseline,
            owned: BTreeMap::new(),
            seen: false,
            empty: None,
            origin: Instant::now(),
            finished: false,
        })
    }
    pub fn refresh(&mut self) -> Result<bool> {
        let current = scan(&self.installation)?;
        let alive: BTreeMap<_, _> = self
            .owned
            .keys()
            .chain(current.keys())
            .filter_map(|p| identity(*p).ok().map(|s| (*p, s)))
            .collect();
        let raw = serde_json::to_vec(
            &json!({"baseline":self.baseline,"owned":self.owned,"current":current,"alive":alive,"seen":self.seen,"empty_since":self.empty,"now":self.origin.elapsed().as_secs_f64()}),
        )?;
        let out = ember_protocol::process::lifecycle(&raw)
            .ok_or_else(|| Error::new("Estado de procesos inválido"))?;
        let value: serde_json::Value = serde_json::from_slice(&out)?;
        self.owned = serde_json::from_value(value["owned"].clone())?;
        self.seen = value["seen"].as_bool().unwrap_or(false);
        self.empty = value["empty_since"].as_f64();
        self.finished = value["finished"].as_bool().unwrap_or(false);
        Ok(!self.owned.is_empty())
    }
    pub fn stop(&mut self, force: bool) -> Result<serde_json::Value> {
        self.refresh()?;
        for (&pid, &start) in &self.owned {
            signal(pid, start, force)?;
        }
        let deadline = Instant::now() + Duration::from_secs(8);
        while Instant::now() < deadline {
            if !self.refresh()? {
                return Ok(json!({"closed":true,"force_required":false}));
            }
            std::thread::sleep(Duration::from_millis(200));
        }
        Ok(json!({"closed":false,"force_required":true}))
    }
}
pub fn signal(pid: u32, start: u64, force: bool) -> Result<()> {
    #[cfg(target_os = "linux")]
    {
        let descriptor = unsafe { libc::syscall(libc::SYS_pidfd_open, pid, 0) } as i32;
        if descriptor < 0 {
            return Ok(());
        }
        let result = if identity(pid).ok() == Some(start) {
            unsafe {
                libc::syscall(
                    libc::SYS_pidfd_send_signal,
                    descriptor,
                    if force { libc::SIGKILL } else { libc::SIGTERM },
                    std::ptr::null::<libc::siginfo_t>(),
                    0,
                )
            }
        } else {
            0
        };
        unsafe { libc::close(descriptor) };
        if result < 0 && std::io::Error::last_os_error().raw_os_error() != Some(libc::ESRCH) {
            return Err(Error::new("No se pudo cerrar el proceso del juego."));
        }
    }
    #[cfg(target_os = "macos")]
    {
        if identity(pid).ok() == Some(start) {
            unsafe {
                libc::kill(
                    pid as i32,
                    if force { libc::SIGKILL } else { libc::SIGTERM },
                );
            }
        }
    }
    #[cfg(windows)]
    {
        use windows_sys::Win32::{Foundation::*, System::Threading::*, UI::WindowsAndMessaging::*};
        unsafe {
            let handle = OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE | PROCESS_SYNCHRONIZE,
                0,
                pid,
            );
            if handle.is_null() {
                return Ok(());
            }
            if identity(pid).ok() == Some(start) {
                if force {
                    TerminateProcess(handle, 1);
                } else {
                    unsafe extern "system" fn visit(hwnd: HWND, param: LPARAM) -> BOOL {
                        let mut pid = 0;
                        GetWindowThreadProcessId(hwnd, &mut pid);
                        if pid == param as u32 {
                            PostMessageW(hwnd, WM_CLOSE, 0, 0);
                        }
                        1
                    }
                    EnumWindows(Some(visit), pid as LPARAM);
                }
            }
            CloseHandle(handle);
        }
    }
    Ok(())
}
