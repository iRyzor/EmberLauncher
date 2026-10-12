use crate::{fs, require, Error, Result};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
#[derive(Clone)]
pub struct Installation {
    pub root: PathBuf,
    pub prefix: PathBuf,
}
impl Installation {
    pub fn discover(path: &Path) -> Result<Self> {
        let root = fs::physical(path)?;
        if cfg!(windows) {
            return Ok(Self {
                prefix: root.clone(),
                root,
            });
        }
        let prefix = root
            .ancestors()
            .find(|p| p.join("drive_c").is_dir())
            .ok_or_else(|| Error::new("La instalación debe estar dentro de un prefijo Wine."))?
            .to_owned();
        Ok(Self { root, prefix })
    }
    pub fn game(&self) -> PathBuf {
        self.root.join("Games/ChaosZeroNightmare")
    }
    pub fn executable(&self) -> Result<PathBuf> {
        let bin = self.game().join("bin");
        let mut found = Vec::new();
        for entry in std::fs::read_dir(&bin)? {
            let p = entry?.path();
            if p.file_name()
                .unwrap()
                .to_string_lossy()
                .eq_ignore_ascii_case("ucldr_chaoszeronightmare_gl_loader_x64.exe")
            {
                found.push(p);
            }
        }
        require(
            found.len() == 1,
            "No se encontró el loader del juego de forma única.",
        )?;
        Ok(found.remove(0))
    }
    pub fn windows(&self, path: &Path) -> Result<String> {
        let path = fs::physical(path)?;
        if cfg!(windows) {
            return Ok(path.to_string_lossy().trim_start_matches(r"\\?\").into());
        }
        let relative = path
            .strip_prefix(self.prefix.join("drive_c"))
            .map_err(|_| Error::new("La ruta debe estar dentro de drive_c."))?;
        Ok(format!(
            "C:\\{}",
            relative.to_string_lossy().replace('/', "\\")
        ))
    }
    pub fn fresh(path: &Path) -> Result<Self> {
        let result = Self::discover(path)?;
        if !cfg!(windows) {
            require(
                result.root.starts_with(result.prefix.join("drive_c")),
                "Selecciona una carpeta dentro de drive_c.",
            )?;
            require(
                result.prefix.join("system.reg").is_file(),
                "Selecciona un prefijo Wine ya inicializado.",
            )?;
        }
        require(
            std::fs::symlink_metadata(result.game()).is_err(),
            "Ya existe una carpeta del juego en ese destino. Selecciona una nueva.",
        )?;
        Ok(result)
    }
}
pub fn config() -> PathBuf {
    location("config")
}
pub fn cache() -> PathBuf {
    location("cache")
}
pub fn data() -> PathBuf {
    location("data")
}
#[cfg(test)]
pub static TEST_LOCATION: std::sync::Mutex<Option<PathBuf>> = std::sync::Mutex::new(None);
fn location(kind: &str) -> PathBuf {
    if let Some(path) = std::env::var_os(format!("EMBER_{}_DIR", kind.to_uppercase())) {
        return PathBuf::from(path);
    }
    #[cfg(test)]
    if let Some(root) = TEST_LOCATION.lock().unwrap().as_ref() {
        return root.join(kind);
    }
    let home = directories::BaseDirs::new()
        .map(|d| d.home_dir().to_owned())
        .unwrap_or_else(|| PathBuf::from("."));
    if cfg!(target_os = "linux") {
        let (env, fallback) = match kind {
            "config" => ("XDG_CONFIG_HOME", home.join(".config")),
            "cache" => ("XDG_CACHE_HOME", home.join(".cache")),
            _ => ("XDG_DATA_HOME", home.join(".local/share")),
        };
        return std::env::var_os(env)
            .map(PathBuf::from)
            .unwrap_or(fallback)
            .join("ember-launcher");
    }
    if cfg!(windows) {
        return std::env::var_os("LOCALAPPDATA")
            .map(PathBuf::from)
            .unwrap_or(home.join("AppData/Local"))
            .join("EmberLauncher")
            .join(if kind == "cache" { "Cache" } else { "" });
    }
    home.join(if kind == "cache" {
        "Library/Caches"
    } else {
        "Library/Application Support"
    })
    .join("EmberLauncher")
}
pub fn default_root() -> PathBuf {
    data().join(if cfg!(windows) {
        "library/Smilegate"
    } else {
        "prefix/drive_c/Smilegate"
    })
}
pub fn settings() -> Value {
    fs::json(&config().join("settings.json")).unwrap_or(json!({}))
}
pub fn save_settings(installation: &Installation, runner: &str) -> Result<()> {
    let mut value = settings();
    value["root"] = json!(installation.root);
    value["runner"] = json!(runner);
    fs::atomic_json(&config().join("settings.json"), &value)
}
pub fn create_prefix(path: &Path, runner: &str) -> Result<Installation> {
    let prefix = fs::physical(path)?;
    require(
        prefix.parent().is_some()
            && Some(prefix.as_path())
                != directories::BaseDirs::new().as_ref().map(|d| d.home_dir()),
        "Selecciona una carpeta nueva para el prefijo.",
    )?;
    let marker = prefix.join(".ember-prefix.json");
    if prefix.exists() {
        require(
            fs::json(&marker)?["owner"] == "ember-launcher",
            "El destino no pertenece a un prefijo creado por Ember.",
        )?;
    } else {
        fs::directory(&prefix, true)?;
        fs::atomic_json(&marker, &json!({"owner":"ember-launcher","schema":1}))?;
    }
    let dir = fs::directory(&prefix, false)?;
    let _lock = fs::Lock::at(&dir, ".ember-bootstrap.lock")?;
    if cfg!(windows) {
        return Installation::fresh(&prefix.join("Smilegate"));
    }
    if !prefix.join("system.reg").is_file() || !prefix.join("drive_c/windows").is_dir() {
        let mut command = crate::runtime::command(runner, &prefix, "wineboot.exe");
        command
            .arg("--init")
            .env("WINEARCH", "win64")
            .env("WINEDLLOVERRIDES", "mscoree,mshtml=");
        require(
            crate::runtime::run(&mut command, 120)?.success(),
            "Wine no pudo completar el prefijo. Puedes reintentar en el destino de Ember.",
        )?;
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(30);
        while !prefix.join("system.reg").is_file() && std::time::Instant::now() < deadline {
            std::thread::sleep(std::time::Duration::from_millis(200));
        }
    }
    require(
        prefix.join("system.reg").is_file() && prefix.join("drive_c/windows").is_dir(),
        "Wine no completó el prefijo.",
    )?;
    Installation::fresh(&prefix.join("drive_c/Smilegate"))
}
