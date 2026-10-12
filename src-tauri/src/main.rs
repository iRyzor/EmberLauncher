#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
use serde_json::{json, Value};
use std::{
    io::{BufRead, BufReader, Write},
    process::{Child, ChildStdin, ChildStdout, Command, Stdio},
    sync::Mutex,
};
use tauri::{Manager, State};
struct Engine {
    child: Child,
    input: ChildStdin,
    output: BufReader<ChildStdout>,
}
impl Drop for Engine {
    fn drop(&mut self) {
        let _ = writeln!(self.input, "{}", json!({"action":"shutdown"}));
        let _ = self.input.flush();
        // The engine may finish a bounded session-token rotation before shutting down.
        for _ in 0..250 {
            if self.child.try_wait().ok().flatten().is_some() {
                return;
            }
            std::thread::sleep(std::time::Duration::from_millis(100));
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}
#[tauri::command]
async fn engine_request(
    state: State<'_, Mutex<Engine>>,
    action: String,
    payload: Option<Value>,
) -> Result<Value, String> {
    const ACTIONS: &[&str] = &[
        "state",
        "news",
        "login",
        "poll",
        "logout",
        "renew_session",
        "account_settings",
        "restore_session",
        "launch",
        "stop_game",
        "force_stop_game",
        "check_updates",
        "prepare",
        "cancel_download",
        "apply_update",
        "recover_update",
        "verify",
        "settings",
        "new_installation",
        "uninstall",
        "bootstrap",
        "prepare_dependencies",
    ];
    if !ACTIONS.contains(&action.as_str()) {
        return Err("Operación no disponible".into());
    }
    let mut engine = state.lock().map_err(|_| "Motor no disponible")?;
    let request = json!({"action":action,"payload":payload});
    writeln!(engine.input, "{}", request).map_err(|_| "No se pudo contactar al motor")?;
    engine.input.flush().map_err(|_| "Motor no disponible")?;
    let mut response = String::new();
    engine
        .output
        .read_line(&mut response)
        .map_err(|_| "Respuesta no disponible")?;
    if response.len() > 1048576 {
        return Err("Respuesta demasiado grande".into());
    }
    serde_json::from_str(&response).map_err(|_| "Respuesta inválida".into())
}
#[tauri::command]
fn open_news_link(url: String) -> Result<(), String> {
    let prefix = "https://page.onstove.com/chaoszeronightmare/en/view/";
    let id = url.strip_prefix(prefix).ok_or("Enlace no permitido")?;
    if id.is_empty() || id.len() > 20 || !id.bytes().all(|b| b.is_ascii_digit()) {
        return Err("Enlace no permitido".into());
    }
    open_external(&url)
}
#[tauri::command]
fn open_creator_profile() -> Result<(), String> {
    open_external("https://github.com/iRyzor")
}
fn open_external(url: &str) -> Result<(), String> {
    #[cfg(target_os = "linux")]
    let result = Command::new("xdg-open").arg(&url).spawn();
    #[cfg(target_os = "macos")]
    let result = Command::new("open").arg(&url).spawn();
    #[cfg(target_os = "windows")]
    let result = Command::new("rundll32.exe")
        .arg("url.dll,FileProtocolHandler")
        .arg(&url)
        .spawn();
    result
        .map(|_| ())
        .map_err(|_| "No se pudo abrir el enlace en el navegador.".into())
}
fn main() {
    // XWayland is the verified backend on this machine. Explicit user choices win.
    if cfg!(target_os = "linux")
        && std::env::var_os("GDK_BACKEND").is_none()
        && std::env::var_os("DISPLAY").is_some()
    {
        std::env::set_var("GDK_BACKEND", "x11");
    }
    if cfg!(target_os = "linux") && std::env::var_os("WEBKIT_DISABLE_DMABUF_RENDERER").is_none() {
        std::env::set_var("WEBKIT_DISABLE_DMABUF_RENDERER", "1");
    }
    tauri::Builder::default()
        .setup(|app| {
            let resource = app.path().resource_dir()?;
            let dev = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"));
            let adjacent = std::env::current_exe()?
                .parent()
                .ok_or("Executable directory unavailable")?
                .to_path_buf();
            let engine_name = if cfg!(target_os = "windows") {
                "stove-engine.exe"
            } else {
                "stove-engine"
            };
            let engine_relative = std::path::PathBuf::from("engine").join(engine_name);
            let executable = if cfg!(debug_assertions) {
                dev.join(&engine_relative)
            } else if adjacent.join(&engine_relative).is_file() {
                adjacent.join(&engine_relative)
            } else {
                resource.join(&engine_relative)
            };
            let helper = if cfg!(debug_assertions) {
                dev.join("../engine/native/pipe-bridge.exe")
            } else {
                if adjacent.join("native/pipe-bridge.exe").is_file() {
                    adjacent.join("native/pipe-bridge.exe")
                } else {
                    resource.join("native/pipe-bridge.exe")
                }
            };
            let mut command = Command::new(executable);
            #[cfg(target_os = "windows")]
            {
                use std::os::windows::process::CommandExt;
                command.creation_flags(0x08000000);
            }
            let mut child = command
                .arg("--helper")
                .arg(helper)
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::null())
                .spawn()?;
            let input = child.stdin.take().ok_or("stdin unavailable")?;
            let output = BufReader::new(child.stdout.take().ok_or("stdout unavailable")?);
            app.manage(Mutex::new(Engine {
                child,
                input,
                output,
            }));
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            engine_request,
            open_news_link,
            open_creator_profile
        ])
        .run(tauri::generate_context!())
        .expect("No se pudo abrir Ember Launcher");
}
