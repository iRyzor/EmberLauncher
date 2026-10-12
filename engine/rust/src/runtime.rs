use crate::{
    fs,
    install::{cache, Installation},
    now, processes, require, Error, Result,
};
use serde_json::{json, Value};
use std::{
    io::{Read, Write},
    path::{Path, PathBuf},
    process::{Command, ExitStatus, Stdio},
    time::{Duration, Instant},
};
pub fn command(runner: &str, prefix: &Path, exe: impl AsRef<std::ffi::OsStr>) -> Command {
    let mut c = if cfg!(windows) {
        Command::new(exe)
    } else {
        let mut c = Command::new(runner);
        c.arg(exe)
            .env("WINEPREFIX", prefix)
            .env("WINEDEBUG", "-all");
        c
    };
    c.stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        c.creation_flags(0x08000000);
    }
    c
}
pub fn run(command: &mut Command, seconds: u64) -> Result<ExitStatus> {
    let mut child = command.spawn()?;
    let deadline = Instant::now() + Duration::from_secs(seconds);
    loop {
        if let Some(status) = child.try_wait()? {
            return Ok(status);
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Err(Error::new("La operación superó el tiempo permitido."));
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}
pub fn probe(i: &Installation, runner: &str, helper: &Path) -> Option<String> {
    let sdk = i.game().join("bin/WebView2Loader.dll");
    if !sdk.is_file() {
        return None;
    }
    let probe = helper.with_file_name("webview-probe.exe");
    if !probe.is_file() {
        return None;
    }
    let mut c = command(runner, &i.prefix, probe);
    c.arg(i.windows(&sdk).ok()?).stdout(Stdio::piped());
    let mut child = c.spawn().ok()?;
    let stdout = child.stdout.take()?;
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let mut buf = Vec::new();
        let result = stdout.take(513).read_to_end(&mut buf);
        let _ = tx.send((result, buf));
    });
    let deadline = Instant::now() + Duration::from_secs(20);
    let status = loop {
        if let Ok(Some(status)) = child.try_wait() {
            break status;
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            return None;
        }
        std::thread::sleep(Duration::from_millis(100));
    };
    if !status.success() {
        return None;
    }
    let (result, raw) = rx.recv_timeout(Duration::from_secs(1)).ok()?;
    result.ok()?;
    let text = std::str::from_utf8(&raw)
        .ok()?
        .strip_prefix("WEBVIEW2_VERSION=")?
        .strip_suffix('\n')?
        .trim_end_matches('\r');
    if valid_version(text) {
        Some(text.into())
    } else {
        None
    }
}
fn valid_version(s: &str) -> bool {
    s.len() <= 128
        && s.split('.').count() == 4
        && s.split('.')
            .all(|p| !p.is_empty() && p.bytes().all(|c| c.is_ascii_digit()))
}
pub fn files(i: &Installation) -> bool {
    let mut folders = if cfg!(windows) {
        ["PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"]
            .iter()
            .filter_map(|k| std::env::var_os(k).map(PathBuf::from))
            .map(|p| p.join("Microsoft/EdgeWebView/Application"))
            .collect::<Vec<_>>()
    } else {
        vec![
            i.prefix
                .join("drive_c/Program Files (x86)/Microsoft/EdgeWebView/Application"),
            i.prefix
                .join("drive_c/Program Files/Microsoft/EdgeWebView/Application"),
        ]
    };
    if let Ok(users) = std::fs::read_dir(i.prefix.join("drive_c/users")) {
        for u in users.flatten() {
            folders.push(
                u.path()
                    .join("AppData/Local/Microsoft/EdgeWebView/Application"),
            );
        }
    }
    files_in(&folders)
}
pub(crate) fn files_in(folders: &[PathBuf]) -> bool {
    folders.iter().any(|p| {
        std::fs::read_dir(p).ok().is_some_and(|dirs| {
            dirs.flatten().any(|e| {
                valid_version(&e.file_name().to_string_lossy())
                    && ["msedgewebview2.exe", "msedge.dll", "icudtl.dat"]
                        .iter()
                        .all(|n| e.path().join(n).is_file())
            })
        })
    })
}
pub fn require_runtime(i: &Installation, runner: &str, helper: &Path) -> Result<()> {
    require(probe(i,runner,helper).is_some(),"No se detecta WebView2 para el juego. Abre Ajustes → Preparar dependencias y vuelve a intentarlo.")
}
fn allowed(url: &reqwest::Url) -> bool {
    url.scheme() == "https"
        && matches!(
            url.host_str(),
            Some("go.microsoft.com" | "msedge.sf.dl.delivery.mp.microsoft.com")
        )
        && url.username().is_empty()
        && url.password().is_none()
        && url.port_or_known_default() == Some(443)
}
fn installer(progress: &mut impl FnMut(Value)) -> Result<PathBuf> {
    const URL: &str = "https://go.microsoft.com/fwlink/?linkid=2124701";
    const MAX: u64 = 300 * 1024 * 1024;
    let folder = cache().join("runtimes");
    let dir = fs::directory(&folder, true)?;
    let _lock = fs::Lock::at(&dir, ".download.lock")?;
    let target = folder.join("MicrosoftEdgeWebView2RuntimeInstallerX64.exe");
    let receipt = target.with_extension("json");
    if let Ok(v) = fs::json(&receipt) {
        if v["source"] == URL
            && v["downloaded_at"]
                .as_f64()
                .is_some_and(|t| now() - t >= 0.0 && now() - t < 30.0 * 86400.0)
            && target
                .metadata()
                .is_ok_and(|m| m.len() > 1024 * 1024 && m.len() <= MAX)
            && fs::hash_path(&target, false).ok().as_deref() == v["sha256"].as_str()
        {
            return Ok(target);
        }
    }
    for p in [&target, &receipt] {
        if let Ok(m) = std::fs::symlink_metadata(p) {
            require(
                !m.file_type().is_symlink(),
                "La caché de dependencias contiene un enlace inesperado.",
            )?;
        }
    }
    let client = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(30))
        .redirect(reqwest::redirect::Policy::custom(|a| {
            if a.previous().len() < 5 && allowed(a.url()) {
                a.follow()
            } else {
                a.stop()
            }
        }))
        .build()
        .map_err(|_| Error::new("No se pudo descargar WebView2."))?;
    let mut response = client
        .get(URL)
        .send()
        .map_err(|_| Error::new("No se pudo descargar WebView2 desde Microsoft."))?;
    require(
        response.status().is_success() && allowed(response.url()),
        "Microsoft devolvió una dirección inesperada.",
    )?;
    let total = response.content_length().unwrap_or(0);
    require(total <= MAX, "El instalador supera el tamaño permitido.")?;
    let name = format!(".webview2-{}", uuid::Uuid::new_v4().simple());
    let mut output = fs::open(&dir, &name, true, true)?;
    let mut count = 0;
    let mut buffer = [0u8; 262144];
    let result = (|| {
        loop {
            let n = response.read(&mut buffer)?;
            if n == 0 {
                break;
            }
            if count == 0 {
                require(
                    buffer[..n].starts_with(b"MZ"),
                    "Microsoft no devolvió un instalador válido.",
                )?;
            }
            count += n as u64;
            require(count <= MAX, "El instalador supera el tamaño permitido.")?;
            output.write_all(&buffer[..n])?;
            progress(
                json!({"phase":"runtime_download","downloaded_bytes":count,"total_bytes":total}),
            );
        }
        require(
            count > 1024 * 1024 && (total == 0 || count == total),
            "La descarga del runtime quedó incompleta.",
        )?;
        output.sync_all()?;
        drop(output);
        dir.rename(&name, &dir, target.file_name().unwrap())?;
        fs::atomic_json(
            &receipt,
            &json!({"source":URL,"sha256":fs::hash_path(&target,false)?,"bytes":count,"downloaded_at":now()}),
        )?;
        Ok(target)
    })();
    let _ = dir.remove_file(name);
    result
}
pub fn prepare(
    i: &Installation,
    runner: &str,
    helper: &Path,
    mut progress: impl FnMut(Value),
) -> Result<Value> {
    processes::idle(i)?;
    let dir = fs::directory(&i.prefix, false)?;
    let _lock = fs::Lock::at(&dir, ".ember-runtime.lock")?;
    processes::idle(i)?;
    let has_sdk = i.game().join("bin/WebView2Loader.dll").is_file();
    let ready = if has_sdk {
        probe(i, runner, helper).is_some()
    } else {
        files(i)
    };
    let mut timed_out = false;
    if !ready {
        let installer = installer(&mut progress)?;
        processes::idle(i)?;
        progress(json!({"phase":"runtime_install"}));
        let mut c = command(runner, &i.prefix, installer);
        c.args(["/silent", "/install"]);
        timed_out = run(&mut c, 180).is_err();
    }
    if !cfg!(windows) {
        for (key, name, kind, value) in [
            (
                r"HKCU\Software\Wine\AppDefaults\msedgewebview2.exe",
                "Version",
                "REG_SZ",
                "win7",
            ),
            (
                r"HKLM\System\CurrentControlSet\Services\edgeupdate",
                "Start",
                "REG_DWORD",
                "3",
            ),
        ] {
            let mut c = command(runner, &i.prefix, "reg");
            c.args(["add", key, "/v", name, "/t", kind, "/d", value, "/f"]);
            require(
                run(&mut c, 30)?.success(),
                "No se pudo preparar la compatibilidad de WebView2.",
            )?;
        }
    }
    let version = if has_sdk {
        probe(i, runner, helper)
    } else {
        None
    };
    require(
        if has_sdk { version.is_some() } else { files(i) },
        "WebView2 aún no se detecta. Reintenta Preparar dependencias.",
    )?;
    Ok(
        json!({"runtime_ready":true,"runtime_installed":!ready,"runtime_version":version,"installer_timed_out":timed_out}),
    )
}
