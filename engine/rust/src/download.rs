use crate::{fs, install::Installation, manifest, require, Error, Result, GAME};
use cap_fs_ext::{FollowSymlinks, OpenOptionsFollowExt};
use md5::{Digest, Md5};
use serde_json::{json, Value};
use std::{
    io::{Read, Write},
    path::{Path, PathBuf},
    sync::atomic::{AtomicBool, Ordering},
    time::Duration,
};
pub fn cancel(flag: &AtomicBool) -> Result<()> {
    if flag.load(Ordering::Relaxed) {
        Err(Error::cancelled())
    } else {
        Ok(())
    }
}
pub fn package_url(item: &Value) -> Result<String> {
    let build = item["build"]
        .as_u64()
        .ok_or_else(|| Error::new("Coordenada inválida"))?;
    let chunk = item["chunk"]
        .as_u64()
        .ok_or_else(|| Error::new("Coordenada inválida"))?;
    let start = item["startpos"]
        .as_u64()
        .ok_or_else(|| Error::new("Coordenada inválida"))?;
    let suffix = if start > build {
        format!("v{start}/{build}_{chunk}.gz")
    } else {
        format!("v{build}/{chunk}.gz")
    };
    Ok(format!(
        "https://{}/game/dpms_{GAME}/{suffix}",
        manifest::CDN
    ))
}
pub fn stage(
    item: &Value,
    folder: &Path,
    flag: &AtomicBool,
    mut progress: impl FnMut(u64, u64),
) -> Result<PathBuf> {
    let dir = fs::directory(folder, true)?;
    let packed = folder.join("download.gz");
    let output = folder.join("verified.bin");
    let size = item["size"]
        .as_u64()
        .ok_or_else(|| Error::new("Tamaño inválido"))?;
    let total = item["packed_size"]
        .as_u64()
        .ok_or_else(|| Error::new("Tamaño inválido"))?;
    cancel(flag)?;
    if manifest::verified(&output, size, item["md5"].as_str().unwrap_or(""))? {
        progress(total, total);
        return Ok(output);
    }
    for name in ["download.gz", "verified.bin", "expanded.tmp"] {
        if let Ok(m) = dir.symlink_metadata(name) {
            require(
                m.is_file() && !m.file_type().is_symlink(),
                "Archivo de caché inseguro.",
            )?;
        }
    }
    let _ = dir.remove_file("verified.bin");
    let client = reqwest::blocking::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(10))
        .build()
        .map_err(|_| Error::new("No se pudo descargar el paquete."))?;
    for attempt in 0..3 {
        cancel(flag)?;
        let mut offset = dir
            .symlink_metadata("download.gz")
            .map(|m| m.len())
            .unwrap_or(0);
        if offset > total {
            dir.remove_file("download.gz")?;
            offset = 0;
        }
        if offset == total {
            break;
        }
        progress(offset, total);
        let mut request = client
            .get(package_url(item)?)
            .header("Accept-Encoding", "identity");
        if offset > 0 {
            request = request.header("Range", format!("bytes={offset}-"));
        }
        let action = (|| -> Result<()> {
            let mut response = request
                .send()
                .map_err(|_| Error::new("Descarga interrumpida."))?;
            let append = match response.status().as_u16() {
                206 => {
                    require(
                        response
                            .headers()
                            .get("Content-Range")
                            .and_then(|v| v.to_str().ok())
                            == Some(format!("bytes {offset}-{}/{total}", total - 1).as_str()),
                        "Respuesta parcial inesperada.",
                    )?;
                    true
                }
                200 => {
                    offset = 0;
                    false
                }
                _ => return Err(Error::new("Respuesta de paquete inesperada.")),
            };
            let mut opts = cap_std::fs::OpenOptions::new();
            opts.write(true)
                .create(true)
                .append(append)
                .truncate(!append)
                .follow(FollowSymlinks::No);
            let mut target = dir.open_with("download.gz", &opts)?.into_std();
            let mut buffer = [0u8; 262144];
            let result = (|| {
                loop {
                    cancel(flag)?;
                    let bound = buffer
                        .len()
                        .min(total.saturating_sub(offset).saturating_add(1) as usize);
                    let n = response.read(&mut buffer[..bound])?;
                    if n == 0 {
                        break;
                    }
                    offset += n as u64;
                    require(offset <= total, "Paquete mayor que el manifiesto.")?;
                    target.write_all(&buffer[..n])?;
                    progress(offset, total);
                }
                require(offset == total, "Descarga interrumpida.")
            })();
            target.sync_all()?;
            result
        })();
        match action {
            Ok(()) => break,
            Err(e) if e.cancelled => return Err(e),
            Err(e) if attempt == 2 => return Err(e),
            Err(_) => {
                for _ in 0..5 * (attempt + 1) {
                    cancel(flag)?;
                    std::thread::sleep(Duration::from_millis(100));
                }
            }
        }
    }
    cancel(flag)?;
    if !manifest::verified(&packed, total, item["packed_md5"].as_str().unwrap_or(""))? {
        let _ = dir.remove_file("download.gz");
        return Err(Error::new("La integridad del paquete no coincide."));
    }
    let _ = dir.remove_file("expanded.tmp");
    let input = fs::open(&dir, "download.gz", false, false)?;
    let mut gz = flate2::read::MultiGzDecoder::new(input);
    let mut target = fs::open(&dir, "expanded.tmp", true, true)?;
    let result = (|| {
        let mut digest = Md5::new();
        let mut count = 0u64;
        let mut buffer = [0u8; 262144];
        loop {
            cancel(flag)?;
            let bound = buffer
                .len()
                .min(size.saturating_sub(count).saturating_add(1) as usize);
            let n = gz.read(&mut buffer[..bound])?;
            if n == 0 {
                break;
            }
            count += n as u64;
            require(
                count <= size,
                "Contenido expandido mayor que el manifiesto.",
            )?;
            digest.update(&buffer[..n]);
            target.write_all(&buffer[..n])?;
        }
        require(
            count == size
                && Some(format!("{:x}", digest.finalize()).as_str()) == item["md5"].as_str(),
            "Integridad del contenido expandido inválida.",
        )?;
        target.sync_all()?;
        drop(target);
        dir.rename("expanded.tmp", &dir, "verified.bin")?;
        fs::sync(&dir)?;
        Ok(output)
    })();
    let _ = dir.remove_file("expanded.tmp");
    result
}
pub fn pending(installation: Option<&Installation>, staging: &Path) -> Option<Value> {
    let game = installation?.game();
    for folder in std::fs::read_dir(staging).ok()?.flatten() {
        if !folder.file_name().to_string_lossy().starts_with("update-") {
            continue;
        }
        if let Ok(v) = fs::json(&folder.path().join("downloading.json")) {
            if v["root"].as_str() == Some(game.to_string_lossy().as_ref())
                && v["published_version"].as_u64().is_some()
            {
                return Some(json!({"published_version":v["published_version"]}));
            }
        }
    }
    None
}
pub fn prepare(
    i: &Installation,
    published: &Value,
    destination: &Path,
    flag: &AtomicBool,
    mut progress: impl FnMut(Value),
) -> Result<Value> {
    let version = published["published_version"]
        .as_u64()
        .ok_or_else(|| Error::new("Versión inválida"))?;
    let installed = published["distribution_version"].as_u64().unwrap_or(0);
    require(
        version >= installed,
        "La versión publicada es anterior: no se preparará una degradación.",
    )?;
    require(
        !destination.starts_with(i.game()),
        "La carpeta temporal debe estar fuera del juego.",
    )?;
    cancel(flag)?;
    progress(json!({"phase":"manifest","downloaded_bytes":0,"total_bytes":0}));
    let raw = manifest::fetch(version, published["project_url"].as_str())?;
    cancel(flag)?;
    let fresh = !i.game().exists();
    let mut plan = if fresh {
        let dir = fs::directory(destination, true)?;
        drop(dir);
        let empty = tempfile::Builder::new()
            .prefix("empty-")
            .tempdir_in(destination)?;
        manifest::plan(empty.path(), &raw, version)?
    } else {
        manifest::plan(&i.game(), &raw, version)?
    };
    require(plan["blocked"] == false, "Hay archivos bloqueados.")?;
    let game = i.game();
    let key = fs::sha(&[game.to_string_lossy().as_bytes(), b"\0", raw.as_slice()].concat());
    let work = destination.join(format!("update-{key}"));
    let dir = fs::directory(&work, true)?;
    let _lock = fs::Lock::at(&dir, ".lock")?;
    fs::atomic_json(
        &work.join("downloading.json"),
        &json!({"root":game,"published_version":version}),
    )?;
    plan["distribution_version"] = json!(installed);
    plan["published_version"] = json!(version);
    plan["remote_latest_checked"] = json!(true);
    plan["installation_root"] = json!(game);
    plan["fresh_install"] = json!(fresh);
    plan["staged"] = json!([]);
    let items: Vec<_> = plan["entries"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|x| {
            x["kind"] == "F" && matches!(x["status"].as_str(), Some("missing" | "modified"))
        })
        .cloned()
        .collect();
    let mut completed = 0u64;
    for (index, item) in items.iter().enumerate() {
        cancel(flag)?;
        let metadata=format!("{{\"build\": {}, \"chunk\": {}, \"md5\": \"{}\", \"packed_md5\": \"{}\", \"packed_size\": {}, \"size\": {}, \"startpos\": {}}}",item["build"],item["chunk"],item["md5"].as_str().unwrap(),item["packed_md5"].as_str().unwrap(),item["packed_size"],item["size"],item["startpos"]);
        let key = fs::sha(metadata.as_bytes());
        let package = work.join(format!("package-{key}"));
        let output = stage(item, &package, flag, |received, _| {
            progress(
                json!({"phase":"download","downloaded_bytes":completed+received,"total_bytes":plan["download_bytes"],"package_index":index+1,"package_count":items.len(),"path":item["path"]}),
            )
        })?;
        plan["staged"].as_array_mut().unwrap().push(json!({"path":item["path"],"staged_file":output.strip_prefix(&work).unwrap().to_string_lossy().replace('\\',"/")}));
        completed += item["packed_size"].as_u64().unwrap();
    }
    cancel(flag)?;
    fs::atomic(&work.join("manifest.json"), &raw)?;
    fs::atomic_json(&work.join("prepared.json"), &plan)?;
    dir.remove_file("downloading.json")?;
    progress(
        json!({"phase":"ready","downloaded_bytes":completed,"total_bytes":plan["download_bytes"],"package_count":items.len()}),
    );
    let mut result = published.clone();
    result.as_object_mut().unwrap().remove("project_url");
    result["prepared"] = json!(true);
    result["prepared_directory"] = json!(work);
    result["download_bytes"] = plan["download_bytes"].clone();
    result["staged_packages"] = json!(items.len());
    result["installed_files_changed"] = json!(false);
    Ok(result)
}
