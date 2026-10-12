use crate::{
    fs, install::Installation, manifest, processes, require, transaction, Error, Result, GAME, SDK,
};
use rusqlite::{Connection, OpenFlags};
use serde_json::{json, Value};
use std::path::Path;
pub fn installed(i: Option<&Installation>) -> Result<Value> {
    let mut value = json!({"game_id":GAME,"distribution_version":null,"tracked_files":null,"tracked_bytes":null,"remote_checked":false,"update_available":null});
    let Some(i) = i else {
        return Ok(value);
    };
    let folder = i.game().join("combinedata_manifest");
    let upf = folder.join(format!("GameManifest_{GAME}.upf"));
    if upf.is_file() {
        let v: Value = serde_json::from_slice(&fs::read(&upf, 1048576)?)?;
        require(
            v["game_id"] == GAME && v["local_version"].as_u64().is_some(),
            "Versión instalada inesperada.",
        )?;
        value["distribution_version"] = v["local_version"].clone();
    }
    let db = folder.join("cacheii.db");
    if db.is_file() {
        fs::physical(&db)?;
        let connection = Connection::open_with_flags(db, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
        connection.execute_batch("PRAGMA query_only=ON")?;
        let (count,total,invalid):(u64,u64,Option<u64>)=connection.query_row("SELECT COUNT(*),COALESCE(SUM(file_size),0),SUM(CASE WHEN typeof(file_size) != 'integer' OR file_size<0 THEN 1 ELSE 0 END) FROM installed_info",[],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)))?;
        require(invalid.unwrap_or(0) == 0, "Inventario instalado inválido.")?;
        value["tracked_files"] = json!(count);
        value["tracked_bytes"] = json!(total);
    }
    Ok(value)
}
pub fn published(raw: &Value, installed: u64) -> Result<Value> {
    let latest = raw["live_version"]
        .as_u64()
        .ok_or_else(|| Error::new("STOVE no devolvió una versión publicada válida."))?;
    let location = raw["live_project_file_url"]
        .as_str()
        .filter(|s| !s.is_empty())
        .or(raw["live_project_url"].as_str())
        .ok_or_else(|| Error::new("STOVE no devolvió un manifiesto válido."))?;
    let url = manifest::url(location, latest)?;
    Ok(
        json!({"game_id":GAME,"distribution_version":installed,"published_version":latest,"remote_checked":true,"update_available":latest>installed,"remote_older":latest<installed,"project_url":url}),
    )
}
pub fn summary(folder: &Path) -> Result<Value> {
    let raw = fs::read(&folder.join("manifest.json"), manifest::LIMIT)?;
    let receipt = fs::json(&folder.join("prepared.json"))?;
    require(
        receipt["manifest_sha256"].as_str() == Some(fs::sha(&raw).as_str()),
        "El manifiesto cambió.",
    )?;
    let version = receipt["published_version"]
        .as_u64()
        .ok_or_else(|| Error::new("Versión preparada inválida"))?;
    manifest::parse(&raw, version)?;
    let entries = receipt["entries"]
        .as_array()
        .ok_or_else(|| Error::new("Preparación inválida"))?;
    let changes:Vec<_>=entries.iter().filter(|x|matches!(x["status"].as_str(),Some("missing"|"modified"|"remove"))).map(|x|json!({"path":x["path"],"action":if x["kind"]=="R"{"eliminar"}else if x["status"]=="missing"{"crear"}else{"reemplazar"}})).collect();
    Ok(
        json!({"installed_version":receipt["distribution_version"],"published_version":version,"download_bytes":receipt["download_bytes"],"change_count":changes.len(),"changes":changes}),
    )
}
pub fn recovery(i: Option<&Installation>) -> bool {
    i.is_some_and(|i| std::fs::symlink_metadata(i.game().join(".stove-update-transaction")).is_ok())
}
pub fn apply(i: &Installation, folder: &Path) -> Result<Value> {
    processes::idle(i)?;
    let summary = summary(folder)?;
    let receipt = fs::json(&folder.join("prepared.json"))?;
    let raw = fs::read(&folder.join("manifest.json"), manifest::LIMIT)?;
    let version = summary["published_version"].as_u64().unwrap();
    for item in receipt["staged"]
        .as_array()
        .ok_or_else(|| Error::new("Preparación inválida"))?
    {
        if manifest::folded(item["path"].as_str().unwrap_or("")) == "bin/basesdk.dll" {
            let dir = fs::directory(folder, false)?;
            let mut sdk = fs::open(
                &dir,
                item["staged_file"]
                    .as_str()
                    .ok_or_else(|| Error::new("SDK inválido"))?,
                false,
                false,
            )?;
            require(fs::hash(&mut sdk,false)?==SDK,"Esta actualización cambia el SDK de STOVE. Hace falta validar su compatibilidad antes de aplicarla.")?;
        }
    }
    if receipt["fresh_install"] == true {
        return transaction::fresh(i, folder, &raw, version, &receipt);
    }
    require(
        installed(Some(i))?["distribution_version"] == receipt["distribution_version"],
        "La versión instalada cambió.",
    )?;
    let mut tx = transaction::Transaction::new(&i.game(), folder)?;
    tx.apply(&raw, version, &receipt, Some(i), |_| Ok(()))?;
    let archive = tx.finalize()?;
    Ok(
        json!({"applied":true,"version":installed(Some(i))?["distribution_version"],"backup_directory":archive}),
    )
}
pub fn recover(i: &Installation) -> Result<Value> {
    processes::idle(i)?;
    let mut tx = transaction::Transaction::new(&i.game(), &i.game())?;
    let state = tx.load()?;
    if state["phase"] == "applying" {
        tx.rollback()?;
    }
    let archive = tx.finalize()?;
    Ok(
        json!({"recovered":true,"version":installed(Some(i))?["distribution_version"],"backup_directory":archive}),
    )
}
pub fn inventory_schema(connection: &Connection) -> Result<()> {
    let mut statement = connection.prepare("PRAGMA table_info(installed_info)")?;
    let columns = statement
        .query_map([], |r| Ok((r.get::<_, String>(1)?, r.get::<_, String>(2)?)))?
        .collect::<std::result::Result<Vec<_>, _>>()?;
    require(
        columns
            == vec![
                ("file_name".into(), "TEXT".into()),
                ("file_size".into(), "BIGINT".into()),
                ("file_hash".into(), "VARCHAR(32)".into()),
            ],
        "Esquema del inventario no compatible.",
    )
}
pub fn metadata(
    i: &Installation,
    game: &Path,
    work: &Path,
    raw: &[u8],
    version: u64,
    expected: Option<u64>,
) -> Result<Vec<(String, std::path::PathBuf)>> {
    let folder = game.join("combinedata_manifest");
    let mut value;
    let output = work.join("inventory.sqlite");
    fs::physical(&output)?;
    require(!output.exists(), "Inventario temporal ya existente.")?;
    if let Some(expected) = expected {
        let db = folder.join("cacheii.db");
        let upf = folder.join(format!("GameManifest_{GAME}.upf"));
        fs::physical(&db)?;
        value = fs::json(&upf)?;
        require(
            value["game_id"] == GAME && value["local_version"].as_u64() == Some(expected),
            "La versión instalada cambió.",
        )?;
        let connection = Connection::open_with_flags(&db, OpenFlags::SQLITE_OPEN_READ_WRITE)?;
        connection.busy_timeout(std::time::Duration::from_millis(100))?;
        inventory_schema(&connection)?;
        connection.execute_batch("PRAGMA locking_mode=EXCLUSIVE")?;
        let busy: u64 =
            connection.query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |r| r.get(0))?;
        require(busy == 0, "Inventario en uso.")?;
        let mode: String = connection.query_row("PRAGMA journal_mode=DELETE", [], |r| r.get(0))?;
        require(mode == "delete", "No se pudo normalizar el inventario.")?;
        connection.backup(rusqlite::DatabaseName::Main, &output, None)?;
    } else {
        value = json!({"local_version":version,"files":[],"locale":"kr","env":"","game_id":GAME,"install_path":i.windows(&i.game())?,"game_title":"Chaos Zero Nightmare","type_code":1,"grades":[],"project_url":manifest::default_url(version)});
        let connection = Connection::open(&output)?;
        connection.execute_batch(
            "CREATE TABLE installed_info(file_name TEXT,file_size BIGINT,file_hash VARCHAR(32))",
        )?;
    }
    let mut target = Connection::open(&output)?;
    let transaction = target.transaction()?;
    transaction.execute("DELETE FROM installed_info", [])?;
    for item in manifest::parse(raw, version)?
        .iter()
        .filter(|x| x["kind"] == "F")
    {
        transaction.execute(
            "INSERT INTO installed_info(file_name,file_size,file_hash) VALUES(?,?,?)",
            rusqlite::params![
                item["path"].as_str().unwrap(),
                item["size"].as_u64().unwrap(),
                item["md5"].as_str().unwrap().to_uppercase()
            ],
        )?;
    }
    transaction.commit()?;
    let integrity: String = target.query_row("PRAGMA integrity_check", [], |r| r.get(0))?;
    require(integrity == "ok", "Inventario inválido.")?;
    drop(target);
    value["local_version"] = json!(version);
    value["project_url"] = json!(manifest::default_url(version));
    let upf = work.join("version.upf");
    fs::atomic_json(&upf, &value)?;
    for p in [&output, &upf] {
        let dir = fs::directory(work, false)?;
        fs::open(&dir, p.file_name().unwrap().to_str().unwrap(), true, false)?.sync_all()?;
    }
    Ok(vec![
        ("combinedata_manifest/cacheii.db".into(), output),
        (format!("combinedata_manifest/GameManifest_{GAME}.upf"), upf),
    ])
}
