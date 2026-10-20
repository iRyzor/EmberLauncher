//! Private SDK worker. Account values never enter argv, environment or logs.
use crate::{
    auth::{self, Session},
    fs,
    install::Installation,
    require, runtime, Error, Result, GAME, SDK,
};
use rand::RngCore;
use serde_json::{json, Value};
use std::{
    io::{Read, Write},
    path::Path,
    process::{Child, ChildStdin, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc, Mutex,
    },
    thread,
    time::{Duration, Instant},
};
use zeroize::Zeroizing;
const NETWORK_ERROR: &str =
    "Wine no pudo preparar el servicio de red necesario para iniciar el juego.";

fn information(tokens: &Value, received: u64) -> Value {
    let mut info = json!({"game_id":GAME,"access_token":tokens["access_token"],"refresh_token":tokens["refresh_token"],"env":"live","gds_isdefault_yn":"n","gds_nation":"DO","gds_regulation":"ETC","gds_timezone":"America/Santo_Domingo","gds_utcoffset":"-240","language":"en"});
    for (source, target) in [
        ("expire_in", "expire_in"),
        ("member_no", "member_no"),
        ("user_id", "guid"),
    ] {
        if let Some(v) = tokens.get(source) {
            if !v.is_null() && v.as_str() != Some("") {
                info[target] = json!(v
                    .as_str()
                    .map(str::to_owned)
                    .unwrap_or_else(|| v.to_string()));
            }
        }
    }
    if let Some(expires) = tokens["expires_in"].as_u64() {
        info["expire_time"] = json!(received.saturating_add(expires).to_string());
    }
    info
}
struct Handshake {
    handle: *mut std::ffi::c_void,
    key: Zeroizing<[u8; 32]>,
    info: Zeroizing<Vec<u8>>,
}
impl Drop for Handshake {
    fn drop(&mut self) {
        unsafe { ember_protocol::sdk::ember_sdk_free(self.handle) }
    }
}
impl Handshake {
    fn new(mut tokens: Value, received: u64) -> Result<Self> {
        let mut info = information(&tokens, received);
        auth::wipe(&mut tokens);
        let raw = Zeroizing::new(serde_json::to_vec(&info)?);
        auth::wipe(&mut info);
        require(
            unsafe { ember_protocol::sdk::ember_sdk_info(raw.as_ptr(), raw.len()) } == 0,
            "Información SDK inválida.",
        )?;
        let mut random = Zeroizing::new([0u8; 16]);
        rand::rngs::OsRng.fill_bytes(&mut *random);
        let hex = Zeroizing::new(
            random
                .iter()
                .map(|b| format!("{b:02x}"))
                .collect::<String>(),
        );
        let mut key = Zeroizing::new([0u8; 32]);
        key.copy_from_slice(hex.as_bytes());
        Ok(Self {
            handle: ember_protocol::sdk::ember_sdk_new(),
            key,
            info: raw,
        })
    }
    fn receive(&mut self, id: u16, payload: &[u8]) -> Result<Vec<u8>> {
        let action = unsafe {
            ember_protocol::sdk::ember_sdk_step(
                self.handle,
                0,
                id as u32,
                payload.as_ptr(),
                payload.len(),
            )
        };
        require(action >= 0, "Falló el intercambio local con el SDK.")?;
        if action == 2 {
            return Ok(Vec::new());
        }
        let mut output = vec![0u8; 2 * 65535];
        let count = unsafe {
            ember_protocol::sdk::ember_sdk_responses(
                payload.as_ptr(),
                payload.len(),
                self.key.as_ptr(),
                self.key.len(),
                self.info.as_ptr(),
                self.info.len(),
                output.as_mut_ptr(),
                output.len(),
            )
        };
        require(count >= 0, "No se pudieron construir las respuestas SDK.")?;
        output.truncate(count as usize);
        require(
            unsafe { ember_protocol::sdk::ember_sdk_step(self.handle, 1, 0, std::ptr::null(), 0) }
                == 0,
            "Estado SDK inválido.",
        )?;
        Ok(output)
    }
    fn heartbeat(&self) -> u64 {
        unsafe { ember_protocol::sdk::ember_sdk_step(self.handle, 3, 0, std::ptr::null(), 0) }
            .max(0) as u64
    }
}
fn reader(
    mut stream: impl Read + Send + 'static,
    chunk: usize,
) -> mpsc::Receiver<std::io::Result<Vec<u8>>> {
    let (tx, rx) = mpsc::sync_channel(4);
    thread::spawn(move || loop {
        let mut buffer = vec![0u8; chunk];
        let result = stream.read(&mut buffer).map(|n| {
            buffer.truncate(n);
            buffer
        });
        let finished = result.as_ref().map_or(true, |b| b.is_empty());
        if tx.send(result).is_err() || finished {
            break;
        }
    });
    rx
}
fn status(state: &Arc<Mutex<Value>>, phase: &str, detail: &str, heartbeats: u64) {
    if let Ok(mut v) = state.lock() {
        *v = json!({"phase":phase,"detail":detail,"heartbeats":heartbeats});
    }
}
pub struct Launcher {
    pub state: Arc<Mutex<Value>>,
    stop: Arc<AtomicBool>,
    done: mpsc::Receiver<()>,
    thread: Option<thread::JoinHandle<()>>,
}
impl Launcher {
    pub fn start(
        i: Installation,
        runner: String,
        helper: std::path::PathBuf,
        tokens: Value,
    ) -> Result<Self> {
        let sdk = i.game().join("bin/BaseSDK.dll");
        require(
            fs::hash_path(&sdk, false)? == SDK,
            "El SDK instalado necesita validar un nuevo perfil.",
        )?;
        i.executable()?;
        require(
            helper.is_file() && (cfg!(windows) || crate::runtime::resolve(&runner).is_some()),
            "Falta el ejecutable Wine o el componente de comunicación.",
        )?;
        let users = if cfg!(windows) {
            std::env::var_os("LOCALAPPDATA")
                .map(|p| vec![std::path::PathBuf::from(p)])
                .unwrap_or_default()
        } else {
            std::fs::read_dir(i.prefix.join("drive_c/users"))
                .map(|r| {
                    r.flatten()
                        .map(|e| e.path().join("AppData/Local"))
                        .collect()
                })
                .unwrap_or_default()
        };
        for user in users {
            if let Ok(entries) = std::fs::read_dir(user) {
                for entry in entries.flatten() {
                    if entry
                        .file_name()
                        .to_string_lossy()
                        .starts_with("STOVEPCSDK3")
                        && entry.path().join("StoveConfigs/launcherData.json").exists()
                    {
                        return Err(Error::new(
                            "Existe una configuración del canal SDK que requiere revisión.",
                        ));
                    }
                }
            }
        }
        let lock = fs::Lock::game(&i.game())?;
        require(
            fs::hash_path(&sdk, false)? == SDK,
            "El SDK cambió antes del arranque.",
        )?;
        let state = Arc::new(Mutex::new(
            json!({"phase":"launch_preparing","detail":"Preparando el canal local del SDK.","heartbeats":0}),
        ));
        let stop = Arc::new(AtomicBool::new(false));
        let (tx, done) = mpsc::channel();
        let local_state = state.clone();
        let local_stop = stop.clone();
        let thread = thread::spawn(move || {
            let _lock = lock;
            let mut tokens = Session(tokens);
            let received = tokens.0["_received_at_ms"]
                .as_u64()
                .unwrap_or((crate::now() * 1000.0) as u64);
            let result = worker(
                &i,
                &runner,
                &helper,
                std::mem::take(&mut tokens.0),
                received,
                &local_state,
                &local_stop,
            );
            if result.is_err() && !local_stop.load(Ordering::Relaxed) {
                let detail = if result
                    .as_ref()
                    .err()
                    .is_some_and(|error| error.message == NETWORK_ERROR)
                {
                    NETWORK_ERROR
                } else {
                    "Falló el arranque o el intercambio local con el SDK. Los tokens no se muestran."
                };
                status(&local_state, "launch_error", detail, 0);
            }
            let _ = tx.send(());
        });
        Ok(Self {
            state,
            stop,
            done,
            thread: Some(thread),
        })
    }
    pub fn snapshot(&self) -> Value {
        self.state.lock().map(|s| s.clone()).unwrap_or(
            json!({"phase":"launch_error","detail":"Motor no disponible.","heartbeats":0}),
        )
    }
    pub fn close(&mut self) {
        if self.thread.is_none() {
            return;
        }
        self.stop.store(true, Ordering::Relaxed);
        if self.done.recv_timeout(Duration::from_secs(6)).is_ok() {
            if let Some(t) = self.thread.take() {
                let _ = t.join();
            }
        } else {
            self.thread.take();
        }
    }
}
impl Drop for Launcher {
    fn drop(&mut self) {
        self.close()
    }
}
struct Bridge(Child);
impl Drop for Bridge {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
fn writer(
    mut stream: ChildStdin,
    stop: Arc<AtomicBool>,
) -> (mpsc::SyncSender<Vec<u8>>, mpsc::Receiver<bool>) {
    let (tx, rx) = mpsc::sync_channel::<Vec<u8>>(1);
    let (ack, done) = mpsc::channel();
    thread::spawn(move || {
        while let Ok(frame) = rx.recv() {
            if stop.load(Ordering::Relaxed) {
                break;
            }
            let ok = stream
                .write_all(&frame)
                .and_then(|_| stream.flush())
                .is_ok();
            let _ = ack.send(ok);
            if !ok {
                break;
            }
        }
    });
    (tx, done)
}
fn worker(
    i: &Installation,
    runner: &str,
    helper: &Path,
    tokens: Value,
    received: u64,
    state: &Arc<Mutex<Value>>,
    stop: &Arc<AtomicBool>,
) -> Result<()> {
    let mut handshake = Handshake::new(tokens, received)?;
    let pipe = format!(r"\\.\pipe\15BADE76-0E87-422B-A75B-B2049FE5F5F5\{GAME}");
    let mut command = runtime::command(runner, &i.prefix, helper);
    command
        .args([&pipe, "300"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut bridge = Bridge(command.spawn()?);
    let stderr = reader(
        bridge
            .0
            .stderr
            .take()
            .ok_or_else(|| Error::new("Canal no disponible"))?,
        512,
    );
    let stdout = reader(
        bridge
            .0
            .stdout
            .take()
            .ok_or_else(|| Error::new("Canal no disponible"))?,
        65535,
    );
    let (tx, ack) = writer(
        bridge
            .0
            .stdin
            .take()
            .ok_or_else(|| Error::new("Canal no disponible"))?,
        stop.clone(),
    );
    let mut diagnostics = Vec::new();
    let deadline = Instant::now() + Duration::from_secs(45);
    loop {
        if stop.load(Ordering::Relaxed) {
            return Ok(());
        }
        require(
            Instant::now() < deadline,
            "El componente de comunicación tardó demasiado.",
        )?;
        match stderr.recv_timeout(Duration::from_millis(500)) {
            Ok(Ok(data)) => {
                require(
                    !data.is_empty() && diagnostics.len() + data.len() <= 8192,
                    "Componente no disponible.",
                )?;
                diagnostics.extend(data);
                let clean: Vec<_> = diagnostics
                    .iter()
                    .filter(|b| **b != b'\r')
                    .copied()
                    .collect();
                if clean
                    .split(|byte| *byte == b'\n')
                    .any(|line| line.starts_with(b"NETWORK_ERROR="))
                {
                    return Err(Error::new(NETWORK_ERROR));
                }
                if clean.windows(11).any(|w| w == b"PIPE_READY\n") {
                    break;
                }
            }
            Err(mpsc::RecvTimeoutError::Timeout) => (),
            _ => return Err(Error::new("Componente no disponible.")),
        }
    }
    if stop.load(Ordering::Relaxed) {
        return Ok(());
    }
    let mut command = runtime::command(runner, &i.prefix, i.windows(&i.executable()?)?);
    command
        .arg(r"bin\ssr-stove-shield.exe")
        .current_dir(i.game());
    let mut game = command.spawn()?;
    status(
        state,
        "launch_waiting_sdk",
        "El juego se inició. Esperando la conexión de su SDK.",
        0,
    );
    let mut decoder = ember_protocol::codec::Decoder::new();
    let mut deadline = Instant::now() + Duration::from_secs(180);
    struct Packets(Vec<(u16, Vec<u8>)>);
    unsafe extern "C" fn sink(
        context: *mut std::ffi::c_void,
        id: u16,
        data: *const u8,
        size: usize,
    ) -> i32 {
        (*(context as *mut Packets))
            .0
            .push((id, std::slice::from_raw_parts(data, size).to_vec()));
        0
    }
    while !stop.load(Ordering::Relaxed) {
        let _ = game.try_wait();
        require(
            Instant::now() < deadline,
            "El SDK no mantuvo la conexión dentro del tiempo permitido.",
        )?;
        match stdout.recv_timeout(Duration::from_millis(500)) {
            Ok(Ok(data)) => {
                if data.is_empty() {
                    require(decoder.finish() == 0, "Paquete SDK incompleto.")?;
                    status(state,"launch_closed","Se cerró la conexión del SDK. Comprueba el resultado en la ventana del juego.",handshake.heartbeat());
                    return Ok(());
                }
                let mut packets = Packets(Vec::new());
                require(
                    unsafe { decoder.feed(&data, sink, (&mut packets as *mut Packets).cast()) }
                        == 0,
                    "Paquete SDK inválido.",
                )?;
                for (id, payload) in packets.0 {
                    let response = handshake.receive(id, &payload)?;
                    if !response.is_empty() {
                        tx.send(response)
                            .map_err(|_| Error::new("Canal no disponible"))?;
                        require(
                            ack.recv_timeout(Duration::from_secs(5)).unwrap_or(false),
                            "El canal no recibió las respuestas.",
                        )?;
                    }
                    if unsafe {
                        ember_protocol::sdk::ember_sdk_step(
                            handshake.handle,
                            2,
                            0,
                            std::ptr::null(),
                            0,
                        )
                    } == 1
                    {
                        deadline = Instant::now() + Duration::from_secs(300);
                        status(state,"launch_info_sent","Los datos de sesión se enviaron al SDK. Comprueba si puedes entrar al juego.",handshake.heartbeat());
                    }
                }
            }
            Err(mpsc::RecvTimeoutError::Timeout) => (),
            _ => return Err(Error::new("Canal SDK no disponible.")),
        }
    }
    Ok(())
}

#[cfg(test)]
impl Launcher {
    pub(crate) fn testing() -> Self {
        let (_tx, done) = mpsc::channel();
        Self {
            state: Arc::new(Mutex::new(
                json!({"phase":"launch_info_sent","detail":"synthetic","heartbeats":1}),
            )),
            stop: Arc::new(AtomicBool::new(false)),
            done,
            thread: None,
        }
    }
}

#[cfg(all(test, unix))]
mod network_tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    #[test]
    fn fragmented_network_failure_blocks_game_before_launch_without_exposing_details() {
        let temp = tempfile::tempdir().unwrap();
        let runner = temp.path().join("runner");
        std::fs::write(
            &runner,
            "#!/bin/sh\nprintf 'Wine startup notice\\nNETWORK_' >&2\nsleep 0.05\nprintf 'ERROR=1058\\n' >&2\nexit 6\n",
        ).unwrap();
        std::fs::set_permissions(&runner, std::fs::Permissions::from_mode(0o700)).unwrap();
        let installation = Installation {
            root: temp.path().join("absent-library"),
            prefix: temp.path().to_owned(),
        };
        let state = Arc::new(Mutex::new(json!({})));
        let stop = Arc::new(AtomicBool::new(false));
        let error = worker(
            &installation,
            runner.to_str().unwrap(),
            &temp.path().join("helper.exe"),
            json!({"access_token":"synthetic-access", "refresh_token":"synthetic-refresh"}),
            0,
            &state,
            &stop,
        )
        .unwrap_err();
        assert_eq!(error.message, NETWORK_ERROR);
        assert!(!error.message.contains("1058"));
        assert!(!error.message.contains("synthetic"));
        assert!(!installation.root.exists());
    }
}
