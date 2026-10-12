//! Desktop RPC controller. Private sessions never enter snapshots. Optional refresh credentials use the OS vault.
use crate::{
    auth::{self, Client, Session},
    download, fs,
    install::{self, Installation},
    launch::Launcher,
    processes::{self, Tracker},
    require, runtime, text, updates, Error, Result,
};
use serde_json::{json, Value};
use std::{
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    time::{Duration, Instant},
};
pub type Shared = Arc<Mutex<Engine>>;
pub struct Engine {
    pub root: PathBuf,
    pub installation: Option<Installation>,
    pub runner: String,
    pub helper: PathBuf,
    pub client: Option<Client>,
    pub session: Option<Session>,
    pub phase: String,
    pub detail: String,
    pub image: Option<String>,
    pub reauth: bool,
    pub remember_session: bool,
    pub account_warning: Option<String>,
    restore_attempted: bool,
    saved_refresh_hash: Option<String>,
    pub launcher: Option<Launcher>,
    pub tracker: Option<Tracker>,
    pub local: Value,
    pub updates: Value,
    pub job: Value,
    pub prepared: Option<PathBuf>,
    pub staging: PathBuf,
    cancel: Arc<AtomicBool>,
    last_poll: Option<Instant>,
    pub closed: bool,
}
impl Engine {
    pub fn new(root: Option<PathBuf>, helper: PathBuf) -> Self {
        let settings = install::settings();
        let root = root
            .or_else(|| std::env::var_os("EMBER_GAME_ROOT").map(PathBuf::from))
            .or_else(|| settings["root"].as_str().map(PathBuf::from))
            .unwrap_or_else(install::default_root);
        let runner = std::env::var("EMBER_WINE_RUNNER").unwrap_or_else(|_| {
            settings["runner"]
                .as_str()
                .unwrap_or(if cfg!(windows) { "native" } else { "wine" })
                .into()
        });
        let installation = Installation::discover(&root).ok();
        let local = updates::installed(installation.as_ref())
            .unwrap_or_else(|_| json!({"distribution_version":null}));
        let mut engine = Self {
            root,
            installation,
            runner,
            helper,
            client: None,
            session: None,
            phase: "signed_out".into(),
            detail: "Conecta tu cuenta para jugar.".into(),
            image: None,
            reauth: false,
            remember_session: settings["remember_session"] == true,
            account_warning: None,
            restore_attempted: false,
            saved_refresh_hash: None,
            launcher: None,
            tracker: None,
            local,
            updates: Value::Null,
            job: Value::Null,
            prepared: None,
            staging: install::cache().join("updates"),
            cancel: Arc::new(AtomicBool::new(false)),
            last_poll: None,
            closed: false,
        };
        if let (Some(i), Ok(pointer)) = (
            &engine.installation,
            fs::json(&install::config().join("pending-update.json")),
        ) {
            if pointer["root"].as_str() == Some(i.root.to_string_lossy().as_ref()) {
                if let Some(path) = pointer["directory"].as_str() {
                    let path = PathBuf::from(path);
                    if path.starts_with(&engine.staging) && fs::physical(&path).is_ok() {
                        if let Ok(review) = updates::summary(&path) {
                            if review["installed_version"].as_u64()
                                == Some(engine.local["distribution_version"].as_u64().unwrap_or(0))
                            {
                                engine.prepared = Some(path);
                            }
                        }
                    }
                }
            }
        }
        engine
    }
    fn ready_client(&mut self) -> Result<()> {
        if self.client.is_none() {
            let i = self.installation.as_ref().ok_or_else(|| {
                Error::new("Selecciona la instalación de Chaos Zero Nightmare en Ajustes.")
            })?;
            self.client = Some(Client::new(i)?);
        }
        Ok(())
    }
    fn busy(&self) -> bool {
        self.job["running"] == true
    }
    fn active(&mut self) -> Result<bool> {
        let running = if let Some(t) = self.tracker.as_mut() {
            t.refresh()? || t.seen && !t.finished
        } else {
            false
        };
        Ok(running
            || self.launcher.as_ref().is_some_and(|l| {
                !matches!(
                    l.snapshot()["phase"].as_str(),
                    Some("idle" | "launch_closed" | "launch_error")
                )
            }))
    }
    fn persist_session(&mut self) {
        if self.remember_session {
            if let Some(session) = &self.session {
                let hash = fs::sha(session.0["refresh_token"].as_str().unwrap_or("").as_bytes());
                if self.saved_refresh_hash.as_ref() == Some(&hash) {
                    return;
                }
                if crate::remember::save(session).is_err() {
                    // Never retry a potentially superseded refresh credential on the next start.
                    let _ = crate::remember::delete();
                    self.account_warning = Some("No se pudo guardar la sesión en el almacén seguro. Desactiva Recordar sesión o vuelve a conectar mediante QR al reiniciar.".into());
                    let _ = self.set_remember(false);
                } else {
                    self.account_warning = None;
                    self.saved_refresh_hash = Some(hash);
                }
            }
        }
    }
    fn set_remember(&mut self, enabled: bool) -> Result<()> {
        let mut settings = install::settings();
        settings["remember_session"] = json!(enabled);
        fs::atomic_json(&install::config().join("settings.json"), &settings)?;
        self.remember_session = enabled;
        if !enabled {
            self.saved_refresh_hash = None;
        }
        Ok(())
    }
    fn expire(&mut self) {
        if self.remember_session {
            let _ = self.set_remember(false);
            if crate::remember::delete().is_err() {
                self.account_warning = Some("No se pudo borrar la sesión del almacén seguro. Desactiva su acceso desde el almacén de credenciales del sistema.".into());
            }
        }
        self.session = None;
        self.reauth = true;
        self.phase = "session_expired".into();
        self.detail = "Conecta de nuevo mediante QR para continuar.".into();
    }
    fn save_prepared(&self) -> Result<()> {
        let pointer = install::config().join("pending-update.json");
        if let Some(p) = &self.prepared {
            fs::atomic_json(
                &pointer,
                &json!({"root":self.installation.as_ref().ok_or_else(||Error::new("Instalación no seleccionada"))?.root,"directory":p}),
            )
        } else {
            match std::fs::remove_file(pointer) {
                Ok(()) => Ok(()),
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
                Err(e) => Err(e.into()),
            }
        }
    }
    pub fn snapshot(&mut self) -> Value {
        let mut running = false;
        let mut finished = false;
        if let Some(t) = self.tracker.as_mut() {
            running = t.refresh().unwrap_or(!t.owned.is_empty());
            finished = t.finished;
            if t.seen && !finished {
                running = true;
            }
        }
        if finished && self.launcher.is_some() {
            self.launcher.take();
            self.tracker = None;
            self.phase = if self.session.is_some() {
                "ready"
            } else {
                "signed_out"
            }
            .into();
            self.detail = "El juego se cerró. Puedes iniciar otra partida.".into();
        }
        if self.busy()
            && matches!(
                self.job["kind"].as_str(),
                Some("stop_game" | "force_stop_game")
            )
        {
            running = true;
        }
        let launch = self.launcher.as_ref().map(Launcher::snapshot);
        let starting = launch.as_ref().is_some_and(|v| {
            matches!(
                v["phase"].as_str(),
                Some("launch_preparing" | "launch_waiting_sdk")
            )
        }) && !running;
        let review = self
            .prepared
            .as_ref()
            .and_then(|p| updates::summary(p).ok());
        if review.is_none() && self.prepared.is_some() {
            self.prepared = None;
        }
        let i = self.installation.as_ref();
        let game_installed = i.is_some_and(|i| i.executable().is_ok_and(|p| p.is_file()));
        let phase = launch
            .as_ref()
            .filter(|l| l["phase"] != "idle")
            .map(|l| text(l, "phase"))
            .unwrap_or_else(|| self.phase.clone());
        let detail = launch
            .as_ref()
            .filter(|l| l["phase"] != "idle")
            .map(|l| text(l, "detail"))
            .unwrap_or_else(|| self.detail.clone());
        let default = install::default_root();
        let suggested = if cfg!(windows) {
            default.parent()
        } else {
            default.parent().and_then(Path::parent)
        };
        let region = crate::region::Region::load();
        json!({"remember_session":self.remember_session,"account_warning":self.account_warning,"country":region.country,"system_language":region.language,"phase":phase,"detail":detail,"image":self.image,"connected":self.session.is_some(),"reauth_required":self.reauth,"version":self.local["distribution_version"],"updates":self.updates,"job":self.job,"prepared":self.prepared.is_some(),"resumable_download":download::pending(i,&self.staging),"prepared_summary":review,"recovery_needed":updates::recovery(i),"root":self.root,"suggested_prefix":suggested,"platform":if cfg!(windows){"win32"}else if cfg!(target_os="macos"){"darwin"}else{"linux"},"native_game":cfg!(windows),"runtime_prefix":i.map(|i|&i.prefix),"game_directory":i.map(Installation::game),"runner":self.runner,"game_installed":game_installed,"fresh_destination":i.is_some_and(|i|!i.game().exists()),"game_running":running,"game_starting":starting})
    }
    fn select(&mut self, i: Installation, runner: String) -> Result<()> {
        let local = updates::installed(Some(&i))?;
        install::save_settings(&i, &runner)?;
        self.root = i.root.clone();
        self.installation = Some(i);
        self.runner = runner;
        self.local = local;
        self.prepared = None;
        self.save_prepared()?;
        self.updates = Value::Null;
        self.client = None;
        self.session = None;
        self.reauth = false;
        self.image = None;
        self.job = Value::Null;
        self.phase = "signed_out".into();
        self.detail = if self.local["distribution_version"].is_null() {
            "Destino seleccionado. Conecta tu cuenta para instalar el juego."
        } else {
            "Instalación seleccionada. Conecta tu cuenta para jugar."
        }
        .into();
        Ok(())
    }
    pub fn dispatch(shared: &Shared, action: &str, payload: &Value) -> Value {
        if action == "restore_account" {
            return json!({"error":"Operación no disponible."});
        }
        if action == "news" {
            return crate::news::request(payload);
        }
        let mut e = match shared.lock() {
            Ok(e) => e,
            Err(_) => return json!({"error":"Motor no disponible."}),
        };
        if action == "shutdown" {
            e.close();
            return json!({"closed":true});
        }
        if action == "state" {
            return e.snapshot();
        }
        let result = e.action(shared, action, payload);
        match result {
            Ok(()) => e.snapshot(),
            Err(error) => {
                if error.expired {
                    e.expire();
                }
                let mut state = e.snapshot();
                state["error"] = json!(error.message);
                state
            }
        }
    }
    fn action(&mut self, shared: &Shared, action: &str, payload: &Value) -> Result<()> {
        match action {
            "account_settings" => {
                require(
                    !self.busy() && !self.active()?,
                    "Espera a que termine la operación actual.",
                )?;
                if let Some(enabled) = payload["remember_session"].as_bool() {
                    if !enabled {
                        crate::remember::delete()?;
                    }
                    self.set_remember(enabled)?;
                    self.account_warning = None;
                    self.persist_session();
                }
            }
            "restore_session" => {
                if (self.restore_attempted && payload["retry"] != true)
                    || !self.remember_session
                    || self.session.is_some()
                {
                    return Ok(());
                }
                require(
                    !self.busy() && !self.active()? && self.image.is_none(),
                    "Espera a que termine la operación actual.",
                )?;
                self.ready_client()?;
                self.restore_attempted = true;
                // Reuse the background job path, keeping the UI responsive.
                return self.action(shared, "restore_account", payload);
            }
            "login" => {
                require(
                    !self.active()?,
                    "Cierra el juego antes de cambiar de sesión.",
                )?;
                require(!self.busy(), "Espera a que termine la operación actual.")?;
                self.ready_client()?;
                self.session = None;
                self.image = Some(self.client.as_mut().unwrap().begin()?);
                self.phase = "qr_pending".into();
                self.detail = "Escanea el QR con la app de STOVE y confirma el acceso.".into();
                self.last_poll = None;
            }
            "poll" => {
                if self.image.is_some()
                    && self
                        .last_poll
                        .is_none_or(|t| t.elapsed() >= Duration::from_secs(3))
                {
                    self.last_poll = Some(Instant::now());
                    let (status, session) = self
                        .client
                        .as_mut()
                        .ok_or_else(|| Error::new("Genera un QR nuevo."))?
                        .poll()?;
                    if status == 2 {
                        self.session = session;
                        self.image = None;
                        self.reauth = false;
                        self.persist_session();
                        self.phase = "ready".into();
                        self.detail = "Cuenta conectada. Todo listo para jugar.".into();
                    } else if matches!(status, 3 | 4) {
                        self.image = None;
                        self.phase = "qr_expired".into();
                        self.detail = "El código ha vencido. Genera otro para continuar.".into();
                    }
                }
            }
            "logout" => {
                require(
                    !self.active()?,
                    "Cierra el juego antes de desconectar la cuenta.",
                )?;
                require(!self.busy(), "Espera a que termine la operación actual.")?;
                let had_remembered = self.remember_session;
                self.set_remember(false)?;
                if had_remembered && crate::remember::delete().is_err() {
                    self.account_warning = Some("No se pudo borrar la sesión del almacén seguro. Desactiva su acceso desde el almacén de credenciales del sistema.".into());
                }
                self.session = None;
                self.client = None;
                self.image = None;
                self.reauth = false;
                self.phase = "signed_out".into();
                self.detail = "Sesión desconectada.".into();
            }
            "launch" => {
                require(self.session.is_some(), "Conecta tu cuenta para jugar.")?;
                require(!self.busy(), "Espera a que termine la operación actual.")?;
                require(!self.active()?, "El juego ya está en ejecución.")?;
                let i = self
                    .installation
                    .as_ref()
                    .ok_or_else(|| Error::new("Selecciona una instalación."))?
                    .clone();
                processes::idle(&i)?;
                self.launcher.take();
                runtime::require_runtime(&i, &self.runner, &self.helper)?;
                self.ready_client()?;
                let result = self
                    .client
                    .as_ref()
                    .unwrap()
                    .authenticated(&mut self.session, |c, s| c.game(s));
                if !result.as_ref().is_err_and(|error| error.expired) {
                    self.persist_session();
                }
                let tokens = result?;
                self.tracker = Some(Tracker::new(i.clone())?);
                self.launcher = Some(Launcher::start(
                    i,
                    self.runner.clone(),
                    self.helper.clone(),
                    tokens,
                )?);
            }
            "cancel_download" => {
                require(
                    self.busy() && self.job["kind"] == "prepare",
                    "No hay ninguna descarga en curso.",
                )?;
                self.cancel.store(true, Ordering::Relaxed);
                self.job["cancelling"] = json!(true);
            }
            "settings" | "new_installation" => {
                let root = text(payload, "root");
                let runner = text(payload, "runner");
                require(
                    !root.is_empty() && !runner.is_empty(),
                    "Indica la carpeta Smilegate y el ejecutable Wine.",
                )?;
                require(
                    self.session.is_none() && !self.busy() && !self.active()?,
                    "Desconecta la cuenta antes de cambiar la instalación.",
                )?;
                require(
                    cfg!(windows) || which::which(&runner).is_ok(),
                    "No se encontró el ejecutable Wine indicado.",
                )?;
                let i = if action == "new_installation" {
                    let i = Installation::fresh(Path::new(&root))?;
                    if let Some(old) = &self.installation {
                        auth::transfer(old, &i)?;
                    } else {
                        auth::profile(&i)?;
                    }
                    i
                } else {
                    let i = Installation::discover(Path::new(&root))?;
                    i.executable()?;
                    i
                };
                self.select(i, runner)?;
            }
            "restore_account"
            | "renew_session"
            | "check_updates"
            | "prepare"
            | "apply_update"
            | "recover_update"
            | "verify"
            | "prepare_dependencies"
            | "bootstrap"
            | "uninstall"
            | "stop_game"
            | "force_stop_game" => {
                require(!self.busy(), "Hay otra operación en curso.")?;
                if action == "restore_account" {
                    require(
                        self.restore_attempted
                            && self.remember_session
                            && self.session.is_none()
                            && self.image.is_none(),
                        "Operación no disponible.",
                    )?;
                }
                if matches!(action, "renew_session" | "check_updates" | "prepare") {
                    require(self.session.is_some(), "Conecta tu cuenta para continuar.")?;
                    self.ready_client()?;
                }
                if matches!(
                    action,
                    "apply_update" | "recover_update" | "prepare_dependencies" | "uninstall"
                ) {
                    require(
                        !self.active()?,
                        "Cierra el juego antes de modificar archivos.",
                    )?;
                }
                if action == "apply_update" {
                    require(
                        self.prepared.is_some(),
                        "Primero prepara los archivos de la actualización.",
                    )?;
                }
                if action == "bootstrap" {
                    require(
                        self.session.is_none() && !self.active()?,
                        "Cierra el juego y desconecta la cuenta antes de crear otro prefijo.",
                    )?;
                    require(
                        !text(payload, "prefix").is_empty() && !text(payload, "runner").is_empty(),
                        "Indica la carpeta del prefijo nuevo y el ejecutable Wine.",
                    )?;
                    self.image = None;
                }
                if action == "uninstall" {
                    require(
                        payload["confirmed"] == true,
                        "Confirma la ruta del juego antes de desinstalar.",
                    )?;
                    require(
                        self.session.is_none(),
                        "Desconecta la cuenta antes de desinstalar.",
                    )?;
                    require(
                        self.prepared.is_none() && !updates::recovery(self.installation.as_ref()),
                        "Completa o recupera la actualización pendiente antes de desinstalar.",
                    )?;
                    self.image = None;
                    self.launcher.take();
                }
                if action == "prepare" {
                    self.prepared = None;
                    self.save_prepared()?;
                }
                self.cancel.store(false, Ordering::Relaxed);
                let tracker = if matches!(action, "stop_game" | "force_stop_game") {
                    require(
                        self.launcher.is_some() && self.tracker.is_some(),
                        "No hay una partida iniciada por Ember.",
                    )?;
                    self.tracker.take()
                } else {
                    None
                };
                let launcher = if matches!(action, "stop_game" | "force_stop_game") {
                    self.launcher.take()
                } else {
                    None
                };
                let context = Worker {
                    installation: self.installation.clone(),
                    runner: self.runner.clone(),
                    helper: self.helper.clone(),
                    staging: self.staging.clone(),
                    prepared: self.prepared.clone(),
                    local: self.local.clone(),
                    client: self.client.as_ref().map(Client::requests_clone),
                    session: self.session.clone(),
                    tracker,
                    launcher,
                    cancel: self.cancel.clone(),
                    new_installation: None,
                    new_runner: None,
                    new_updates: None,
                    new_prepared: None,
                };
                self.job = json!({"kind":action,"running":true,"error":null,"result":null});
                let action = action.to_owned();
                let payload = payload.clone();
                let shared = shared.clone();
                std::thread::spawn(move || {
                    let mut worker = context;
                    let outcome=std::panic::catch_unwind(std::panic::AssertUnwindSafe(||worker.run(&shared,&action,&payload))).unwrap_or_else(|_|Err(Error::new("No se pudo completar la operación. Revisa el estado de recuperación antes de iniciar el juego.")));
                    if let Ok(mut e) = shared.lock() {
                        if e.closed {
                            // Closing the window must not discard an already rotated refresh token.
                            if matches!(
                                action.as_str(),
                                "restore_account" | "renew_session" | "check_updates" | "prepare"
                            ) {
                                if outcome.as_ref().is_err_and(|error| error.expired) {
                                    e.expire();
                                } else {
                                    e.session = worker.session.take();
                                    e.persist_session();
                                    e.session = None;
                                }
                            }
                            e.job["running"] = json!(false);
                            return;
                        }
                        if matches!(
                            action.as_str(),
                            "restore_account" | "renew_session" | "check_updates" | "prepare"
                        ) {
                            e.session = worker.session.take();
                            if !outcome.as_ref().is_err_and(|error| error.expired) {
                                e.persist_session();
                            }
                            if action == "restore_account" && e.session.is_some() {
                                e.phase = "ready".into();
                                e.detail = "Cuenta conectada. Todo listo para jugar.".into();
                                e.reauth = false;
                            }
                        }
                        if let Some(v) = worker.new_updates.take() {
                            e.updates = v;
                        }
                        if let Some(p) = worker.new_prepared.take() {
                            e.prepared = Some(p);
                            if e.save_prepared().is_err() {
                                e.prepared = None;
                            }
                        }
                        if let Some(i) = worker.new_installation.take() {
                            let runner =
                                worker.new_runner.take().unwrap_or_else(|| e.runner.clone());
                            if let Err(error) = e.select(i, runner) {
                                e.job = json!({"kind":action,"running":false,"error":error.message,"result":null});
                                return;
                            }
                        }
                        if matches!(action.as_str(), "stop_game" | "force_stop_game") {
                            e.tracker = worker.tracker.take();
                            e.launcher = worker.launcher.take();
                        }
                        if outcome.as_ref().is_ok_and(|v| v["closed"] == true) {
                            e.launcher.take();
                            e.tracker = None;
                            e.phase = if e.session.is_some() {
                                "ready"
                            } else {
                                "signed_out"
                            }
                            .into();
                            e.detail = "El juego se cerró. Puedes iniciar otra partida.".into();
                        }
                        if outcome.is_ok()
                            && matches!(
                                action.as_str(),
                                "apply_update" | "recover_update" | "uninstall"
                            )
                        {
                            e.local = updates::installed(e.installation.as_ref())
                                .unwrap_or(json!({"distribution_version":null}));
                            e.prepared = None;
                            let _ = e.save_prepared();
                            if action == "uninstall" {
                                e.updates = Value::Null;
                                e.phase = "signed_out".into();
                                e.detail =
                                    "Juego enviado a la papelera. Puedes volver a instalarlo."
                                        .into();
                            } else if e.updates.is_object() {
                                e.updates["distribution_version"] =
                                    e.local["distribution_version"].clone();
                                e.updates["update_available"] = json!(
                                    e.updates["published_version"].as_u64()
                                        > e.local["distribution_version"].as_u64()
                                );
                            }
                        }
                        e.job["running"] = json!(false);
                        match outcome {
                            Ok(result) => e.job["result"] = result,
                            Err(error) if error.cancelled => {
                                e.job["result"] = json!({"cancelled":true})
                            }
                            Err(error) => {
                                if error.expired {
                                    e.expire();
                                }
                                e.job["error"] = json!(error.message);
                            }
                        }
                    }
                });
            }
            _ => return Err(Error::new("Operación no disponible.")),
        }
        Ok(())
    }
    pub fn authentication_pending(&self) -> bool {
        self.remember_session
            && self.busy()
            && matches!(
                self.job["kind"].as_str(),
                Some("restore_account" | "renew_session" | "check_updates" | "prepare")
            )
    }
    pub fn close(&mut self) {
        self.closed = true;
        self.cancel.store(true, Ordering::Relaxed);
        self.launcher.take();
        self.session = None;
        self.client = None;
        self.image = None;
    }
}
struct Worker {
    installation: Option<Installation>,
    runner: String,
    helper: PathBuf,
    staging: PathBuf,
    prepared: Option<PathBuf>,
    local: Value,
    client: Option<Client>,
    session: Option<Session>,
    tracker: Option<Tracker>,
    launcher: Option<Launcher>,
    cancel: Arc<AtomicBool>,
    new_installation: Option<Installation>,
    new_runner: Option<String>,
    new_updates: Option<Value>,
    new_prepared: Option<PathBuf>,
}
impl Worker {
    fn run(&mut self, shared: &Shared, action: &str, payload: &Value) -> Result<Value> {
        if action == "bootstrap" {
            let runner = text(payload, "runner");
            let installation =
                install::create_prefix(Path::new(&text(payload, "prefix")), &runner)?;
            runtime::prepare(&installation, &runner, &self.helper, |v| {
                progress(shared, v)
            })?;
            auth::profile(&installation)?;
            self.new_installation = Some(installation);
            self.new_runner = Some(runner);
            return Ok(json!({"prefix_created":true}));
        }
        if action == "restore_account" {
            if let Some(saved) = crate::remember::load()? {
                self.session = Some(
                    self.client
                        .as_ref()
                        .ok_or_else(Error::expired)?
                        .restore(&saved)?,
                );
                return Ok(json!({"session_restored":true}));
            }
            return Ok(json!({"session_restored":false}));
        }
        if action == "renew_session" {
            let session = self.session.as_ref().ok_or_else(Error::expired)?;
            self.session = Some(
                self.client
                    .as_ref()
                    .ok_or_else(Error::expired)?
                    .refresh(session)?,
            );
            return Ok(json!({"session_renewed":true}));
        }
        if matches!(action, "stop_game" | "force_stop_game") {
            let tracker = self
                .tracker
                .as_mut()
                .ok_or_else(|| Error::new("Partida no disponible"))?;
            if !tracker.refresh()? {
                if let Some(l) = self.launcher.as_mut() {
                    l.close();
                }
            }
            let result = tracker.stop(action == "force_stop_game")?;
            if result["closed"] == true {
                self.launcher.take();
                self.tracker = None;
            }
            return Ok(result);
        }
        let i = self
            .installation
            .as_ref()
            .ok_or_else(|| Error::new("Selecciona una instalación en Ajustes."))?;
        match action {
            "check_updates" | "prepare" => {
                let installed = self.local["distribution_version"].as_u64().unwrap_or(0);
                let raw = self
                    .client
                    .as_ref()
                    .ok_or_else(Error::expired)?
                    .authenticated(&mut self.session, |c, s| c.published(s, installed))?;
                let published = updates::published(&raw, installed)?;
                let mut public = published.clone();
                public.as_object_mut().unwrap().remove("project_url");
                self.new_updates = Some(public.clone());
                if action == "prepare" {
                    let mut sample = (Instant::now(), 0u64, 0f64, String::new());
                    let result = download::prepare(
                        i,
                        &published,
                        &self.staging,
                        &self.cancel,
                        |mut value| {
                            let key = format!("{}:{}", value["phase"], value["package_index"]);
                            let received = value["downloaded_bytes"].as_u64().unwrap_or(0);
                            if key != sample.3 || received < sample.1 {
                                sample = (Instant::now(), received, 0.0, key);
                            } else if sample.0.elapsed().as_secs_f64() >= 0.25 {
                                sample.2 =
                                    (received - sample.1) as f64 / sample.0.elapsed().as_secs_f64();
                                sample.0 = Instant::now();
                                sample.1 = received;
                            }
                            let speed = if value["phase"] == "download" {
                                sample.2
                            } else {
                                0.0
                            };
                            value["bytes_per_second"] = json!(speed);
                            value["eta_seconds"] = if speed > 0.0 {
                                json!(
                                    value["total_bytes"]
                                        .as_u64()
                                        .unwrap_or(0)
                                        .saturating_sub(received)
                                        as f64
                                        / speed
                                )
                            } else {
                                Value::Null
                            };
                            progress(shared, value);
                        },
                    )?;
                    self.new_prepared = Some(PathBuf::from(text(&result, "prepared_directory")));
                    Ok(result)
                } else {
                    Ok(public)
                }
            }
            "apply_update" => updates::apply(
                i,
                self.prepared
                    .as_deref()
                    .ok_or_else(|| Error::new("Primero prepara la actualización."))?,
            ),
            "recover_update" => updates::recover(i),
            "verify" => {
                let version = self.local["distribution_version"]
                    .as_u64()
                    .ok_or_else(|| Error::new("No se conoce la versión instalada."))?;
                let raw = crate::manifest::fetch(version, None)?;
                let plan = crate::manifest::plan(&i.game(), &raw, version)?;
                let files = plan["entries"].as_array().unwrap();
                Ok(
                    json!({"matching":files.iter().filter(|x|x["kind"]=="F"&&x["status"]=="matching").count(),"files":files.iter().filter(|x|x["kind"]=="F").count(),"download_bytes":plan["download_bytes"],"blocked":plan["blocked"]}),
                )
            }
            "prepare_dependencies" => {
                runtime::prepare(i, &self.runner, &self.helper, |v| progress(shared, v))
            }
            "uninstall" => uninstall(i, &text(payload, "path")),
            _ => Err(Error::new("Operación no disponible.")),
        }
    }
}
fn progress(shared: &Shared, value: Value) {
    if let Ok(mut e) = shared.lock() {
        if e.closed {
            return;
        }
        if value["phase"] == "runtime_download" {
            let downloaded = value["downloaded_bytes"].as_f64().unwrap_or(0.0) / 1048576.0;
            let total = value["total_bytes"].as_f64().unwrap_or(0.0) / 1048576.0;
            e.job["detail"] = json!(if total > 0.0 {
                format!("Descargando WebView2 de Microsoft: {downloaded:.0} / {total:.0} MiB.")
            } else {
                format!("Descargando WebView2 de Microsoft: {downloaded:.0} MiB.")
            });
        } else if value["phase"] == "runtime_install" {
            e.job["detail"] =
                json!("Instalando WebView2 en el entorno seleccionado. Puede tardar unos minutos…");
        } else {
            e.job["progress"] = value;
        }
    }
}
pub fn uninstall(i: &Installation, confirmed: &str) -> Result<Value> {
    let game = i.game();
    require(
        confirmed == game.to_string_lossy(),
        "La instalación seleccionada cambió. Revisa de nuevo la ruta.",
    )?;
    fs::physical(&game)?;
    require(
        game.is_dir()
            && updates::installed(Some(i))?["distribution_version"]
                .as_u64()
                .is_some(),
        "No se pudo verificar la instalación seleccionada.",
    )?;
    i.executable()?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        let parent = game.parent().unwrap().metadata()?;
        let meta = game.metadata()?;
        require(
            parent.dev() == meta.dev() && parent.ino() != meta.ino(),
            "No se puede desinstalar una carpeta que sea un punto de montaje.",
        )?;
    }
    processes::idle(i)?;
    let _lock = fs::Lock::game(&game)?;
    processes::idle(i)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        let current = game.join(".stove-update.lock").symlink_metadata()?;
        let held = _lock.0.metadata()?;
        require(
            current.dev() == held.dev() && current.ino() == held.ino(),
            "La instalación cambió durante la operación.",
        )?;
    }
    if cfg!(target_os = "linux") {
        let executable = which::which("gio").map_err(|_| {
            Error::new("Se necesita gio para enviar el juego a la papelera del sistema.")
        })?;
        let mut c = std::process::Command::new(executable);
        c.arg("trash")
            .arg("--")
            .arg(&game)
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null());
        require(
            runtime::run(&mut c, 30)?.success(),
            "No se pudo enviar el juego a la papelera.",
        )?;
    } else {
        trash::delete(&game)
            .map_err(|_| Error::new("No se pudo enviar el juego a la papelera."))?;
    }
    require(
        std::fs::symlink_metadata(&game).is_err(),
        "No se pudo enviar el juego a la papelera. Revisa la carpeta antes de reintentar.",
    )?;
    Ok(json!({"uninstalled":true,"trashed":true,"path":game}))
}
