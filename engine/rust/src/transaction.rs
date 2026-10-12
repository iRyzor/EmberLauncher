//! Durable write-ahead journal; verified backups precede every game mutation.
use crate::{fs, install::Installation, manifest, processes, require, updates, Error, Result, SDK};
use cap_std::fs::Dir;
use serde_json::{json, Value};
use std::{
    collections::{HashMap, HashSet},
    path::{Path, PathBuf},
};
pub struct Transaction {
    game: PathBuf,
    prepared: PathBuf,
    root: Dir,
    _lock: fs::Lock,
    journal: PathBuf,
    pub state: Value,
}
impl Transaction {
    pub fn new(game: &Path, prepared: &Path) -> Result<Self> {
        let root = fs::directory(game, false)?;
        let lock = fs::Lock::at(&root, ".stove-update.lock")?;
        Ok(Self {
            game: game.into(),
            prepared: prepared.into(),
            journal: game.join(".stove-update-transaction"),
            root,
            _lock: lock,
            state: Value::Null,
        })
    }
    fn save(&self) -> Result<()> {
        fs::atomic_json(&self.journal.join("journal.json"), &self.state)
    }
    fn record(&mut self, path: &str, directory: bool) -> Result<()> {
        let (parent, name) = fs::parent(&self.root, path)?;
        let info = match parent.symlink_metadata(&name) {
            Ok(m) => {
                require(
                    m.is_file() && !m.file_type().is_symlink(),
                    "El destino de la transacción no es un archivo regular.",
                )?;
                Some(m)
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => None,
            Err(e) => return Err(e.into()),
        };
        let index = self.state["operations"]
            .as_array()
            .ok_or_else(|| Error::new("Diario inválido"))?
            .len();
        let backup = if info.is_some() {
            Some(index.to_string())
        } else {
            None
        };
        let mut hash = None;
        if let Some(ref backup) = backup {
            let mut source = fs::open(&parent, &name, false, false)?;
            let journal = fs::directory(&self.journal, false)?;
            let mut output = fs::open(&journal, backup, true, true)?;
            std::io::copy(&mut source, &mut output)?;
            output.set_permissions(source.metadata()?.permissions())?;
            output.sync_all()?;
            hash = Some(fs::hash_path(&self.journal.join(backup), false)?);
        }
        self.state["operations"].as_array_mut().unwrap().push(json!({"path":path,"backup":backup,"backup_sha256":hash,"directory":directory,"temporary":if directory{None}else{Some(format!(".stove-new-{}",index+1))}}));
        self.save()
    }
    pub fn load(&self) -> Result<Value> {
        let state = fs::json(&self.journal.join("journal.json"))?;
        require(
            matches!(
                state["phase"].as_str(),
                Some("applying" | "files_verified" | "committed" | "rolled_back")
            ),
            "Diario de actualización inválido.",
        )?;
        let operations = state["operations"]
            .as_array()
            .ok_or_else(|| Error::new("Operaciones de diario inválidas"))?;
        require(operations.len() <= 100000, "Demasiadas operaciones.")?;
        let mut seen = HashSet::new();
        for (index, op) in operations.iter().enumerate() {
            let path = op["path"]
                .as_str()
                .ok_or_else(|| Error::new("Ruta de diario inválida"))?;
            manifest::safe_relative(path, false)?;
            require(
                !path.starts_with(".stove-") && seen.insert(path),
                "Ruta de diario inválida.",
            )?;
            let directory = op["directory"]
                .as_bool()
                .ok_or_else(|| Error::new("Operación de diario inválida"))?;
            let temporary = if directory {
                Value::Null
            } else {
                json!(format!(".stove-new-{}", index + 1))
            };
            require(
                op["temporary"] == temporary,
                "Archivo temporal del diario inválido.",
            )?;
            if !op["backup"].is_null() {
                require(
                    op["backup"].as_str() == Some(index.to_string().as_str()),
                    "Índice de backup inválido.",
                )?;
                let path = self.journal.join(index.to_string());
                require(
                    fs::hash_path(&path, false).ok().as_deref() == op["backup_sha256"].as_str(),
                    "La integridad del backup no coincide.",
                )?;
            }
        }
        Ok(state)
    }
    pub fn finalize(&self) -> Result<PathBuf> {
        let state = self.load()?;
        require(
            state["phase"] != "applying",
            "Recupera la actualización interrumpida primero.",
        )?;
        let name = format!(".stove-update-history-{}", uuid::Uuid::new_v4().simple());
        self.root
            .rename(".stove-update-transaction", &self.root, &name)?;
        fs::sync(&self.root)?;
        Ok(self.game.join(name))
    }
    pub fn rollback(&mut self) -> Result<()> {
        self.state = self.load()?;
        require(
            self.state["phase"] == "applying",
            "La transacción no es recuperable.",
        )?;
        let operations = self.state["operations"].as_array().unwrap().clone();
        for op in operations.iter().rev() {
            let path = op["path"].as_str().unwrap();
            let (parent, name) = match fs::parent(&self.root, path) {
                Ok(p) => p,
                Err(_) if op["backup"].is_null() => continue,
                Err(e) => return Err(e),
            };
            if let Some(backup) = op["backup"].as_str() {
                let journal = fs::directory(&self.journal, false)?;
                let mut source = fs::open(&journal, backup, false, false)?;
                let temp = format!(".stove-restore-{backup}");
                let _ = parent.remove_file(&temp);
                let mut output = fs::open(&parent, &temp, true, true)?;
                std::io::copy(&mut source, &mut output)?;
                output.set_permissions(source.metadata()?.permissions())?;
                output.sync_all()?;
                drop(output);
                parent.rename(&temp, &parent, &name)?;
            } else {
                let result = if op["directory"] == true {
                    parent.remove_dir(&name)
                } else {
                    parent.remove_file(&name)
                };
                if let Err(e) = result {
                    if e.kind() != std::io::ErrorKind::NotFound {
                        return Err(e.into());
                    }
                }
            }
            if let Some(temp) = op["temporary"].as_str() {
                match parent.remove_file(temp) {
                    Ok(()) => (),
                    Err(e) if e.kind() == std::io::ErrorKind::NotFound => (),
                    Err(e) => return Err(e.into()),
                }
            }
            fs::sync(&parent)?;
        }
        self.state["phase"] = json!("rolled_back");
        self.save()
    }
    fn replace(&self, path: &str, source: Option<&Path>) -> Result<()> {
        let (parent, name) = fs::parent(&self.root, path)?;
        let temp = format!(
            ".stove-new-{}",
            self.state["operations"].as_array().unwrap().len()
        );
        let mut output = fs::open(&parent, &temp, true, true)?;
        let result = (|| {
            if let Some(source) = source {
                let dir = fs::directory(
                    source
                        .parent()
                        .ok_or_else(|| Error::new("Fuente inválida"))?,
                    false,
                )?;
                let mut input = fs::open(
                    &dir,
                    source
                        .file_name()
                        .unwrap()
                        .to_str()
                        .ok_or_else(|| Error::new("Fuente inválida"))?,
                    false,
                    false,
                )?;
                std::io::copy(&mut input, &mut output)?;
            }
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                output.set_permissions(std::fs::Permissions::from_mode(0o644))?;
            }
            output.sync_all()?;
            drop(output);
            parent.rename(&temp, &parent, &name)?;
            fs::sync(&parent)
        })();
        let _ = parent.remove_file(temp);
        result
    }
    pub fn apply(
        &mut self,
        raw: &[u8],
        version: u64,
        receipt: &Value,
        installation: Option<&Installation>,
        mut after: impl FnMut(&Value) -> Result<()>,
    ) -> Result<Value> {
        manifest::parse(raw, version)?;
        require(
            receipt["manifest_sha256"].as_str() == Some(fs::sha(raw).as_str()),
            "El manifiesto cambió.",
        )?;
        if let Some(i) = installation {
            processes::idle(i)?;
            require(i.game() == self.game, "Instalación incorrecta.")?;
            require(
                receipt["published_version"].as_u64() == Some(version)
                    && receipt["distribution_version"]
                        .as_u64()
                        .is_some_and(|v| v <= version),
                "Transición de versión inválida.",
            )?;
        }
        let plan = manifest::plan(&self.game, raw, version)?;
        require(plan["blocked"] == false, "Hay destinos bloqueados.")?;
        let staged = receipt["staged"]
            .as_array()
            .ok_or_else(|| Error::new("Preparación inválida"))?;
        let mut sources = HashMap::new();
        let prepared = fs::directory(&self.prepared, false)?;
        for item in plan["entries"].as_array().unwrap().iter().filter(|x| {
            x["kind"] == "F" && matches!(x["status"].as_str(), Some("missing" | "modified"))
        }) {
            let record = staged
                .iter()
                .find(|x| x["path"] == item["path"])
                .ok_or_else(|| Error::new("Falta un paquete preparado."))?;
            let relative = record["staged_file"]
                .as_str()
                .ok_or_else(|| Error::new("Ruta preparada inválida"))?;
            let mut input = fs::open(&prepared, relative, false, false)?;
            require(
                input.metadata()?.len() == item["size"].as_u64().unwrap()
                    && Some(fs::hash(&mut input, true)?.as_str()) == item["md5"].as_str(),
                "El paquete preparado cambió.",
            )?;
            sources.insert(
                item["path"].as_str().unwrap().to_owned(),
                self.prepared.join(relative),
            );
        }
        self.root.create_dir(".stove-update-transaction")?;
        self.state = json!({"phase":"applying","version":version,"operations":[]});
        self.save()?;
        let result = (|| {
            let metadata = if let Some(i) = installation {
                updates::metadata(
                    i,
                    &self.game,
                    &self.journal,
                    raw,
                    version,
                    receipt["distribution_version"].as_u64(),
                )?
            } else {
                vec![]
            };
            let mut entries = plan["entries"].as_array().unwrap().clone();
            entries.sort_by_key(|x| {
                (
                    x["kind"] != "D",
                    x["path"].as_str().unwrap().matches('/').count(),
                )
            });
            for item in entries.iter().filter(|x| {
                matches!(
                    x["status"].as_str(),
                    Some("missing" | "modified" | "remove")
                )
            }) {
                if let Some(i) = installation {
                    processes::idle(i)?;
                }
                let path = item["path"].as_str().unwrap();
                let kind = item["kind"].as_str().unwrap();
                self.record(path, kind == "D")?;
                match kind {
                    "D" => {
                        let (p, n) = fs::parent(&self.root, path)?;
                        p.create_dir(n)?;
                        fs::sync(&p)?;
                    }
                    "R" => {
                        let (p, n) = fs::parent(&self.root, path)?;
                        p.remove_file(n)?;
                        fs::sync(&p)?;
                    }
                    "F" => self.replace(path, Some(sources.get(path).unwrap()))?,
                    "G" => self.replace(path, None)?,
                    _ => return Err(Error::new("Operación inválida")),
                }
                after(item)?;
            }
            let checked = manifest::plan(&self.game, raw, version)?;
            require(
                checked["blocked"] == false
                    && checked["entries"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .all(|x| x["kind"] != "F" || x["status"] == "matching"),
                "La verificación posterior falló.",
            )?;
            for (relative, source) in metadata {
                if let Some(i) = installation {
                    processes::idle(i)?;
                }
                self.record(&relative, false)?;
                self.replace(&relative, Some(&source))?;
                after(&json!({"kind":"metadata","path":relative}))?;
            }
            self.state["phase"] = json!(if installation.is_some() {
                "committed"
            } else {
                "files_verified"
            });
            self.save()?;
            Ok(checked)
        })();
        if result.is_err() {
            self.rollback()?;
        }
        result
    }
}
pub fn fresh(
    i: &Installation,
    prepared: &Path,
    raw: &[u8],
    version: u64,
    receipt: &Value,
) -> Result<Value> {
    fresh_checked(i, prepared, raw, version, receipt, SDK)
}
pub(crate) fn fresh_checked(
    i: &Installation,
    prepared: &Path,
    raw: &[u8],
    version: u64,
    receipt: &Value,
    sdk_hash: &str,
) -> Result<Value> {
    Installation::fresh(&i.root)?;
    processes::idle(i)?;
    require(
        receipt["distribution_version"] == 0
            && receipt["installation_root"].as_str() == Some(i.game().to_string_lossy().as_ref()),
        "La preparación no pertenece a este destino nuevo.",
    )?;
    let records = manifest::parse(raw, version)?;
    let files: HashSet<_> = records
        .iter()
        .filter(|x| x["kind"] == "F")
        .map(|x| manifest::folded(x["path"].as_str().unwrap()))
        .collect();
    require(
        [
            "bin/basesdk.dll",
            "bin/ucldr_chaoszeronightmare_gl_loader_x64.exe",
            "bin/ssr-stove-shield.exe",
        ]
        .iter()
        .all(|x| files.contains(*x)),
        "Faltan archivos de arranque.",
    )?;
    let sdk = receipt["staged"]
        .as_array()
        .unwrap()
        .iter()
        .find(|x| manifest::folded(x["path"].as_str().unwrap_or("")) == "bin/basesdk.dll")
        .ok_or_else(|| Error::new("Falta SDK"))?;
    let dir = fs::directory(prepared, false)?;
    let mut file = fs::open(
        &dir,
        sdk["staged_file"].as_str().unwrap_or(""),
        false,
        false,
    )?;
    require(
        fs::hash(&mut file, false)? == sdk_hash,
        "El SDK publicado requiere validar un nuevo perfil antes de instalarlo.",
    )?;
    let parent = i.game().parent().unwrap().to_owned();
    let parent_dir = fs::directory(&parent, true)?;
    let _lock = fs::Lock::at(&parent_dir, ".ember-install.lock")?;
    Installation::fresh(&i.root)?;
    let build = tempfile::Builder::new()
        .prefix(".ember-install-")
        .tempdir_in(&parent)?;
    let mut tx = Transaction::new(build.path(), prepared)?;
    tx.apply(raw, version, receipt, None, |_| Ok(()))?;
    tx.finalize()?;
    drop(tx);
    fs::directory(&build.path().join("combinedata_manifest"), true)?;
    for (relative, source) in updates::metadata(
        i,
        build.path(),
        &build.path().join("combinedata_manifest"),
        raw,
        version,
        None,
    )? {
        let target = build.path().join(relative);
        let bytes = fs::read(&source, 512 * 1024 * 1024)?;
        fs::atomic(&target, &bytes)?;
        std::fs::remove_file(source)?;
    }
    let checked = manifest::plan(build.path(), raw, version)?;
    require(
        checked["blocked"] == false
            && checked["entries"]
                .as_array()
                .unwrap()
                .iter()
                .all(|x| x["kind"] != "F" || x["status"] == "matching"),
        "La instalación nueva no superó la verificación.",
    )?;
    processes::idle(i)?;
    Installation::fresh(&i.root)?;
    fs::publish(build.path(), &i.game())?;
    Ok(
        json!({"installed":true,"version":version,"verified_files":records.iter().filter(|x|x["kind"]=="F").count()}),
    )
}
