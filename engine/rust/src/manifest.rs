use crate::{fs, require, Error, Result, GAME};
use cap_fs_ext::DirExt;
use serde_json::{json, Value};
use std::{collections::HashSet, io::Read};
use unicode_casefold::UnicodeCaseFold;
pub const LIMIT: usize = 8 * 1024 * 1024;
pub const CDN: &str = "chaoszero-dl.game.onstove.com";
pub fn url(raw: &str, version: u64) -> Result<String> {
    require(
        raw.len() <= 8192 && !raw.contains('\\') && !raw.chars().any(|c| c <= ' ' || c == '\x7f'),
        "STOVE no devolvió un manifiesto de versión válido.",
    )?;
    let mut u = reqwest::Url::parse(raw)
        .map_err(|_| Error::new("STOVE devolvió una dirección de manifiesto inesperada."))?;
    require(
        matches!(u.scheme(), "http" | "https")
            && u.host_str() == Some(CDN)
            && u.username().is_empty()
            && u.password().is_none()
            && u.fragment().is_none()
            && u.port_or_known_default() == Some(if u.scheme() == "https" { 443 } else { 80 })
            && u.path() == format!("/game/dpms_{GAME}/{GAME}_{version}.json"),
        "STOVE devolvió una dirección de manifiesto inesperada.",
    )?;
    u.set_scheme("https")
        .map_err(|_| Error::new("Manifiesto inválido"))?;
    Ok(u.to_string())
}
pub fn default_url(version: u64) -> String {
    format!("https://{CDN}/game/dpms_{GAME}/{GAME}_{version}.json")
}
pub fn fetch(version: u64, location: Option<&str>) -> Result<Vec<u8>> {
    let url = url(location.unwrap_or(&default_url(version)), version)?;
    let client = reqwest::blocking::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(std::time::Duration::from_secs(30))
        .build()
        .map_err(|_| Error::new("No se pudo descargar el manifiesto."))?;
    let response = client
        .get(url)
        .send()
        .map_err(|_| Error::new("No se pudo descargar el manifiesto."))?;
    require(
        response.status().as_u16() == 200,
        "No se pudo descargar el manifiesto.",
    )?;
    let mut raw = Vec::new();
    response.take(LIMIT as u64 + 1).read_to_end(&mut raw)?;
    require(raw.len() <= LIMIT, "Manifiesto demasiado grande.")?;
    Ok(raw)
}
pub fn folded(s: &str) -> String {
    s.case_fold().collect()
}
pub fn safe_relative(path: &str, reserved: bool) -> Result<()> {
    let parts: Vec<_> = path.split('/').collect();
    require(
        !path.is_empty()
            && !parts.iter().any(|p| {
                p.is_empty()
                    || matches!(*p, "." | "..")
                    || p.ends_with([' ', '.'])
                    || p.chars()
                        .any(|c| c <= '\x1f' || c == '\x7f' || "\\:<>\"?*".contains(c))
            }),
        "Ruta de manifiesto insegura.",
    )?;
    for p in &parts {
        let stem = p.split('.').next().unwrap_or("").to_uppercase();
        require(
            !matches!(stem.as_str(), "CON" | "PRN" | "AUX" | "NUL")
                && !(stem.len() == 4
                    && (stem.starts_with("COM") || stem.starts_with("LPT"))
                    && matches!(stem.as_bytes()[3], b'1'..=b'9')),
            "Ruta de manifiesto reservada.",
        )?;
    }
    if reserved {
        require(
            folded(parts[0]) != "combinedata_manifest"
                && !parts.iter().any(|p| folded(p).starts_with(".stove-")),
            "Ruta reservada del launcher.",
        )?;
    }
    Ok(())
}
fn integer(v: &Value) -> Result<u64> {
    v.as_u64()
        .or_else(|| {
            v.as_str()
                .filter(|s| !s.is_empty() && s.bytes().all(|c| c.is_ascii_digit()))
                .and_then(|s| s.parse().ok())
        })
        .ok_or_else(|| Error::new("Metadatos de manifiesto inválidos."))
}
pub fn parse(raw: &[u8], version: u64) -> Result<Vec<Value>> {
    require(raw.len() <= LIMIT, "Manifiesto demasiado grande.")?;
    let value: Value = serde_json::from_slice(raw)?;
    require(
        value["service_code"] == GAME && value["version_no"].as_u64() == Some(version),
        "Identidad o versión de manifiesto inesperada.",
    )?;
    let files = value["files"]
        .as_array()
        .ok_or_else(|| Error::new("Manifiesto sin archivos."))?;
    require(files.len() <= 100000, "Demasiados archivos.")?;
    let mut ids = HashSet::new();
    let mut seen = HashSet::new();
    let mut result = Vec::new();
    let mut size = 0u64;
    let mut packed = 0u64;
    for record in files {
        let record = record
            .as_str()
            .ok_or_else(|| Error::new("Registro inválido"))?;
        require(record.chars().count() <= 8192, "Registro demasiado largo")?;
        let f: Vec<_> = record.split('|').map(str::trim).collect();
        require(
            f.len() >= 3
                && !f[0].is_empty()
                && f[0].bytes().all(|c| c.is_ascii_digit())
                && ids.insert(f[0]),
            "Identificador duplicado o inválido.",
        )?;
        let kind = f[1];
        let path = f[2].strip_prefix('/').unwrap_or(f[2]);
        safe_relative(path, true)?;
        require(seen.insert(folded(path)), "Ruta duplicada en manifiesto.")?;
        let mut item = json!({"kind":kind,"path":path});
        match kind {
            "F" => {
                require(
                    matches!(f.len(), 12 | 13 | 16) && f[5] == "gz",
                    "Codificación de archivo no compatible.",
                )?;
                require(
                    match f.len() {
                        12 => f[10..] == ["a", ""],
                        13 => f[10..] == ["a", "", ""],
                        16 => f[10..] == ["a", "", "", "", "0", ""],
                        _ => false,
                    },
                    "Perfil de hash no compatible.",
                )?;
                for i in [3, 4, 6, 7] {
                    require(
                        !f[i].is_empty() && f[i].bytes().all(|c| c.is_ascii_digit()),
                        "Coordenada de paquete inválida.",
                    )?;
                }
                for i in [8, 9] {
                    require(
                        f[i].len() == 32 && f[i].bytes().all(|c| c.is_ascii_hexdigit()),
                        "Hash de paquete inválido.",
                    )?;
                }
                for (name, index) in [("build", 3), ("chunk", 4), ("size", 6), ("packed_size", 7)] {
                    item[name] = json!(f[index]
                        .parse::<u64>()
                        .map_err(|_| Error::new("Coordenada fuera de rango."))?);
                }
                item["md5"] = json!(f[8].to_lowercase());
                item["packed_md5"] = json!(f[9].to_lowercase());
                item["startpos"] = json!(integer(value.get("startpos").unwrap_or(&json!(0)))?);
                size = size
                    .checked_add(item["size"].as_u64().unwrap())
                    .ok_or_else(|| Error::new("Tamaño fuera de rango"))?;
                packed = packed
                    .checked_add(item["packed_size"].as_u64().unwrap())
                    .ok_or_else(|| Error::new("Tamaño fuera de rango"))?;
            }
            "D" => require(f.len() == 3, "Directorio inválido")?,
            "G" => require(
                f.len() == 3
                    || (f.len() == 16
                        && f[3..14].iter().all(|s| s.is_empty())
                        && f[14..] == ["0", ""]),
                "Archivo generado inválido",
            )?,
            "R" => require(
                f.len() == 3 || (f.len() == 4 && f[3].is_empty()),
                "Eliminación inválida",
            )?,
            _ => return Err(Error::new("Instrucción de manifiesto no compatible.")),
        }
        result.push(item);
    }
    require(
        integer(&value["extract_size"])? == size && integer(&value["packed_size"])? == packed,
        "Tamaño de manifiesto inconsistente.",
    )?;
    Ok(result)
}
fn inspect(root: &cap_std::fs::Dir, item: &Value) -> Result<&'static str> {
    let kind = item["kind"].as_str().unwrap_or("");
    let absent = if kind == "R" { "absent" } else { "missing" };
    let parts: Vec<_> = item["path"].as_str().unwrap_or("").split('/').collect();
    let mut dir = root.try_clone()?;
    for p in &parts[..parts.len() - 1] {
        dir = match dir.open_dir_nofollow(p) {
            Ok(d) => d,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(absent),
            Err(_) => return Ok("blocked"),
        };
    }
    let name = parts.last().unwrap();
    let info = match dir.symlink_metadata(name) {
        Ok(m) => m,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(absent),
        Err(_) => return Ok("blocked"),
    };
    if info.file_type().is_symlink() {
        return Ok("blocked");
    }
    if kind == "D" {
        return Ok(if info.is_dir() { "matching" } else { "blocked" });
    }
    if !info.is_file() {
        return Ok("blocked");
    }
    if kind == "R" {
        return Ok("remove");
    }
    if kind == "G" {
        return Ok("generated_present");
    }
    let mut file = match fs::open(&dir, name, false, false) {
        Ok(f) => f,
        Err(_) => return Ok("blocked"),
    };
    if info.len() != item["size"].as_u64().unwrap_or(u64::MAX) {
        return Ok("modified");
    }
    match fs::hash(&mut file, true) {
        Ok(hash) => Ok(if Some(hash.as_str()) == item["md5"].as_str() {
            "matching"
        } else {
            "modified"
        }),
        Err(_) => Ok("changed_during_check"),
    }
}
pub fn plan(game: &std::path::Path, raw: &[u8], version: u64) -> Result<Value> {
    let mut entries = parse(raw, version)?;
    let dir = fs::directory(game, false)?;
    let mut download = 0u64;
    let mut staging = 0u64;
    let mut blocked = false;
    for item in &mut entries {
        let status = inspect(&dir, item)?;
        item["status"] = json!(status);
        if matches!(status, "blocked" | "changed_during_check") {
            blocked = true;
        }
        if item["kind"] == "F" && matches!(status, "missing" | "modified") {
            download = download
                .checked_add(item["packed_size"].as_u64().unwrap())
                .ok_or_else(|| Error::new("Tamaño fuera de rango"))?;
            staging = staging
                .checked_add(item["size"].as_u64().unwrap())
                .and_then(|v| v.checked_add(item["packed_size"].as_u64().unwrap()))
                .ok_or_else(|| Error::new("Tamaño fuera de rango"))?;
        }
    }
    Ok(
        json!({"game_id":GAME,"reference_version":version,"remote_latest_checked":false,"read_only":true,"coverage":"DPMS distribution files only","manifest_sha256":fs::sha(raw),"download_bytes":download,"staging_bytes":staging,"blocked":blocked,"entries":entries}),
    )
}
pub fn verified(path: &std::path::Path, size: u64, hash: &str) -> Result<bool> {
    let dir = fs::directory(
        path.parent().ok_or_else(|| Error::new("Ruta inválida"))?,
        false,
    )?;
    let name = path
        .file_name()
        .unwrap()
        .to_str()
        .ok_or_else(|| Error::new("Ruta inválida"))?;
    match dir.symlink_metadata(name) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(false),
        Err(e) => return Err(e.into()),
        Ok(m) => {
            require(
                m.is_file() && !m.file_type().is_symlink(),
                "Archivo de caché inseguro.",
            )?;
            if m.len() != size {
                return Ok(false);
            }
        }
    }
    Ok(fs::hash(&mut fs::open(&dir, name, false, false)?, true)? == hash)
}
