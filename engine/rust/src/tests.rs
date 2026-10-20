use super::*;
use md5::{Digest, Md5};
use serde_json::json;
use std::{
    io::Write,
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, AtomicUsize, Ordering},
        Arc, Mutex,
    },
};
fn record(path: &str, data: &[u8], packed: &[u8]) -> String {
    format!(
        "1|F|/{path}|55|1|gz|{}|{}|{:x}|{:x}|a|",
        data.len(),
        packed.len(),
        Md5::digest(data),
        Md5::digest(packed)
    )
}
fn manifest_raw(records: Vec<String>, size: u64, packed: u64) -> Vec<u8> {
    serde_json::to_vec(&json!({"service_code":GAME,"version_no":55,"files":records,"extract_size":size.to_string(),"packed_size":packed.to_string(),"startpos":0})).unwrap()
}
struct Fixture {
    temp: tempfile::TempDir,
    i: install::Installation,
    work: PathBuf,
    raw: Vec<u8>,
    receipt: serde_json::Value,
}
fn fixture() -> Fixture {
    let temp = tempfile::tempdir().unwrap();
    let prefix = temp.path().join("prefix");
    std::fs::create_dir_all(prefix.join("drive_c/Smilegate/Games/ChaosZeroNightmare/bin")).unwrap();
    std::fs::write(prefix.join("system.reg"), b"test").unwrap();
    let i = install::Installation::discover(&prefix.join("drive_c/Smilegate")).unwrap();
    let work = temp.path().join("work");
    std::fs::create_dir_all(&work).unwrap();
    let raw = manifest_raw(vec![record("bin/data.txt", b"new", b"packed")], 3, 6);
    let mut receipt = manifest::plan(&i.game(), &raw, 55).unwrap();
    fs::atomic(&work.join("payload"), b"new").unwrap();
    receipt["distribution_version"] = json!(54);
    receipt["published_version"] = json!(55);
    receipt["staged"] = json!([{"path":"bin/data.txt","staged_file":"payload"}]);
    receipt["installation_root"] = json!(i.game());
    receipt["fresh_install"] = json!(false);
    fs::atomic(&work.join("manifest.json"), &raw).unwrap();
    fs::atomic_json(&work.join("prepared.json"), &receipt).unwrap();
    Fixture {
        temp,
        i,
        work,
        raw,
        receipt,
    }
}
fn metadata(f: &Fixture) {
    let folder = f.i.game().join("combinedata_manifest");
    std::fs::create_dir(&folder).unwrap();
    let connection = rusqlite::Connection::open(folder.join("cacheii.db")).unwrap();
    connection.execute_batch("CREATE TABLE installed_info(file_name TEXT,file_size BIGINT,file_hash VARCHAR(32)); INSERT INTO installed_info VALUES('old',3,'OLD')").unwrap();
    fs::atomic_json(
        &folder.join(format!("GameManifest_{GAME}.upf")),
        &json!({"game_id":GAME,"local_version":54,"unknown_public_field":"preserve"}),
    )
    .unwrap();
}
#[test]
fn manifest_records_and_plan() {
    let f = fixture();
    let plan = manifest::plan(&f.i.game(), &f.raw, 55).unwrap();
    assert_eq!(plan["entries"][0]["status"], "missing");
    assert_eq!(plan["download_bytes"], 6);
    std::fs::write(f.i.game().join("bin/data.txt"), b"new").unwrap();
    assert_eq!(
        manifest::plan(&f.i.game(), &f.raw, 55).unwrap()["entries"][0]["status"],
        "matching"
    );
    std::fs::write(f.i.game().join("bin/data.txt"), b"bad").unwrap();
    assert_eq!(
        manifest::plan(&f.i.game(), &f.raw, 55).unwrap()["entries"][0]["status"],
        "modified"
    );
}
#[test]
fn manifest_rejects_unsafe_paths() {
    for path in [
        "../outside",
        "/absolute",
        "bin//file",
        "bin/CON.txt",
        "bin/a.",
        "bin/a /child",
        "bin/a:b",
        "bin/a\\b",
        "combinedata_manifest/x",
        "bin/.stove-update/x",
        "bin/a\0b",
    ] {
        let raw = manifest_raw(vec![record(path, b"new", b"packed")], 3, 6);
        assert!(manifest::parse(&raw, 55).is_err(), "{path}");
    }
}
#[test]
fn manifest_rejects_identity_sizes_and_duplicates() {
    let f = fixture();
    for (key, value) in [
        ("version_no", json!(true)),
        ("version_no", json!(56)),
        ("service_code", json!("OTHER")),
        ("extract_size", json!(4)),
        ("packed_size", json!(true)),
    ] {
        let mut v: serde_json::Value = serde_json::from_slice(&f.raw).unwrap();
        v[key] = value;
        assert!(manifest::parse(&serde_json::to_vec(&v).unwrap(), 55).is_err());
    }
    let raw = manifest_raw(
        vec![
            record("bin/a", b"new", b"packed"),
            record("bin/a", b"new", b"packed"),
        ],
        6,
        12,
    );
    assert!(manifest::parse(&raw, 55).is_err());
}
#[test]
fn manifest_url_only_exact_https_cdn() {
    let path = format!("/game/dpms_{GAME}/{GAME}_55.json");
    assert_eq!(
        manifest::url(&format!("http://{}{}?x=1", manifest::CDN, path), 55).unwrap(),
        format!("https://{}{}?x=1", manifest::CDN, path)
    );
    for url in [
        format!("https://evil.invalid{path}"),
        format!("https://{}:444{path}", manifest::CDN),
        format!("https://u@{}{path}", manifest::CDN),
        format!("https://{}{path}#x", manifest::CDN),
    ] {
        assert!(manifest::url(&url, 55).is_err());
    }
}
#[test]
fn package_coordinates_and_version_summary() {
    assert!(download::package_url(&json!({"build":true,"chunk":1,"startpos":0})).is_err());
    assert!(
        download::package_url(&json!({"build":55,"chunk":2,"startpos":56}))
            .unwrap()
            .ends_with("v56/55_2.gz")
    );
    let v = updates::published(
        &json!({"live_version":55,"live_project_url":manifest::default_url(55)}),
        55,
    )
    .unwrap();
    assert_eq!(v["update_available"], false);
    assert_eq!(v["remote_older"], false);
}
#[test]
fn transaction_apply_and_archive_retains_backup() {
    let f = fixture();
    std::fs::write(f.i.game().join("bin/data.txt"), b"old").unwrap();
    let mut tx = transaction::Transaction::new(&f.i.game(), &f.work).unwrap();
    tx.apply(&f.raw, 55, &f.receipt, None, |_| Ok(())).unwrap();
    let archive = tx.finalize().unwrap();
    assert_eq!(
        std::fs::read(f.i.game().join("bin/data.txt")).unwrap(),
        b"new"
    );
    assert_eq!(std::fs::read(archive.join("0")).unwrap(), b"old");
    assert!(!updates::recovery(Some(&f.i)));
}
#[test]
fn transaction_error_rolls_back_original() {
    let f = fixture();
    std::fs::write(f.i.game().join("bin/data.txt"), b"old").unwrap();
    let mut tx = transaction::Transaction::new(&f.i.game(), &f.work).unwrap();
    assert!(tx
        .apply(&f.raw, 55, &f.receipt, None, |_| Err(Error::new(
            "synthetic failure"
        )))
        .is_err());
    assert_eq!(
        std::fs::read(f.i.game().join("bin/data.txt")).unwrap(),
        b"old"
    );
    assert_eq!(tx.load().unwrap()["phase"], "rolled_back");
}
#[test]
fn interrupted_transaction_can_recover() {
    let f = fixture();
    std::fs::write(f.i.game().join("bin/data.txt"), b"old").unwrap();
    let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let mut tx = transaction::Transaction::new(&f.i.game(), &f.work).unwrap();
        tx.apply(&f.raw, 55, &f.receipt, None, |_| {
            panic!("synthetic interruption")
        })
        .unwrap();
    }));
    assert!(outcome.is_err());
    assert_eq!(
        std::fs::read(f.i.game().join("bin/data.txt")).unwrap(),
        b"new"
    );
    let mut tx = transaction::Transaction::new(&f.i.game(), &f.work).unwrap();
    tx.rollback().unwrap();
    tx.finalize().unwrap();
    assert_eq!(
        std::fs::read(f.i.game().join("bin/data.txt")).unwrap(),
        b"old"
    );
}
#[test]
fn corrupt_backup_blocks_recovery_before_any_write() {
    let f = fixture();
    std::fs::write(f.i.game().join("bin/data.txt"), b"old").unwrap();
    let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let mut tx = transaction::Transaction::new(&f.i.game(), &f.work).unwrap();
        let _ = tx.apply(&f.raw, 55, &f.receipt, None, |_| panic!("interrupted"));
    }));
    std::fs::write(f.i.game().join(".stove-update-transaction/0"), b"corrupt").unwrap();
    let mut tx = transaction::Transaction::new(&f.i.game(), &f.work).unwrap();
    assert!(tx.rollback().is_err());
    assert_eq!(
        std::fs::read(f.i.game().join("bin/data.txt")).unwrap(),
        b"new"
    );
}
#[test]
fn staged_corruption_rejected_before_journal() {
    let f = fixture();
    std::fs::write(f.work.join("payload"), b"bad").unwrap();
    let mut tx = transaction::Transaction::new(&f.i.game(), &f.work).unwrap();
    assert!(tx.apply(&f.raw, 55, &f.receipt, None, |_| Ok(())).is_err());
    assert!(!updates::recovery(Some(&f.i)));
}
#[test]
fn manifest_tamper_rejected() {
    let f = fixture();
    std::fs::write(f.work.join("manifest.json"), b"{}").unwrap();
    assert!(updates::summary(&f.work).is_err());
}
#[test]
fn metadata_commit_preserves_public_fields_and_schema() {
    let f = fixture();
    metadata(&f);
    let mut tx = transaction::Transaction::new(&f.i.game(), &f.work).unwrap();
    tx.apply(&f.raw, 55, &f.receipt, Some(&f.i), |_| Ok(()))
        .unwrap();
    tx.finalize().unwrap();
    let state = updates::installed(Some(&f.i)).unwrap();
    assert_eq!(state["distribution_version"], 55);
    assert_eq!(state["tracked_files"], 1);
    assert_eq!(state["tracked_bytes"], 3);
    let value = fs::json(
        &f.i.game()
            .join(format!("combinedata_manifest/GameManifest_{GAME}.upf")),
    )
    .unwrap();
    assert_eq!(value["unknown_public_field"], "preserve");
}
#[test]
fn metadata_failure_rolls_back_database_and_version() {
    let f = fixture();
    metadata(&f);
    std::fs::write(f.i.game().join("bin/data.txt"), b"old").unwrap();
    let mut tx = transaction::Transaction::new(&f.i.game(), &f.work).unwrap();
    assert!(tx
        .apply(&f.raw, 55, &f.receipt, Some(&f.i), |item| {
            if item["kind"] == "metadata" {
                Err(Error::new("interrupted"))
            } else {
                Ok(())
            }
        })
        .is_err());
    assert_eq!(
        updates::installed(Some(&f.i)).unwrap()["distribution_version"],
        54
    );
    assert_eq!(
        std::fs::read(f.i.game().join("bin/data.txt")).unwrap(),
        b"old"
    );
}
#[test]
fn fresh_destination_and_publication_never_replace() {
    let f = fixture();
    assert!(install::Installation::fresh(&f.i.root).is_err());
    let a = f.temp.path().join("new");
    let b = f.temp.path().join("existing");
    std::fs::create_dir(&a).unwrap();
    std::fs::create_dir(&b).unwrap();
    std::fs::write(b.join("keep"), b"original").unwrap();
    assert!(fs::publish(&a, &b).is_err());
    assert_eq!(std::fs::read(b.join("keep")).unwrap(), b"original");
}
#[test]
fn fresh_install_off_tree_complete_metadata() {
    let f = fixture();
    std::fs::remove_dir_all(f.i.game()).unwrap();
    let records = vec![
        "0|D|/bin".to_string(),
        record("bin/BaseSDK.dll", b"synthetic-sdk", b"packed"),
        record(
            "bin/ucldr_chaoszeronightmare_gl_loader_x64.exe",
            b"loader",
            b"packed",
        )
        .replacen("1|", "2|", 1),
        record("bin/ssr-stove-shield.exe", b"shield", b"packed").replacen("1|", "3|", 1),
    ];
    let raw = manifest_raw(records, 25, 18);
    let empty = tempfile::tempdir().unwrap();
    let mut receipt = manifest::plan(empty.path(), &raw, 55).unwrap();
    receipt["distribution_version"] = json!(0);
    receipt["published_version"] = json!(55);
    receipt["installation_root"] = json!(f.i.game());
    receipt["staged"] = json!([{ "path":"bin/BaseSDK.dll","staged_file":"sdk"},{"path":"bin/ucldr_chaoszeronightmare_gl_loader_x64.exe","staged_file":"loader"},{"path":"bin/ssr-stove-shield.exe","staged_file":"shield"}]);
    for (p, data) in [
        ("sdk", b"synthetic-sdk".as_slice()),
        ("loader", b"loader"),
        ("shield", b"shield"),
    ] {
        fs::atomic(&f.work.join(p), data).unwrap();
    }
    let result = transaction::fresh_checked(
        &f.i,
        &f.work,
        &raw,
        55,
        &receipt,
        &fs::sha(b"synthetic-sdk"),
    )
    .unwrap();
    assert_eq!(result["verified_files"], 3);
    assert_eq!(
        updates::installed(Some(&f.i)).unwrap()["distribution_version"],
        55
    );
    assert!(f.i.executable().is_ok());
    assert!(transaction::fresh_checked(
        &f.i,
        &f.work,
        &raw,
        55,
        &receipt,
        &fs::sha(b"synthetic-sdk")
    )
    .is_err());
}
#[test]
fn prefix_lock_prevents_second_writer() {
    let f = fixture();
    let dir = fs::directory(&f.i.game(), false).unwrap();
    let lock = fs::Lock::at(&dir, "test.lock").unwrap();
    assert!(fs::Lock::at(&dir, "test.lock").is_err());
    drop(lock);
    assert!(fs::Lock::at(&dir, "test.lock").is_ok());
}
#[cfg(unix)]
#[test]
fn links_and_parent_traversal_never_touch_outside() {
    use std::os::unix::fs::symlink;
    let f = fixture();
    let outside = f.temp.path().join("outside");
    std::fs::create_dir(&outside).unwrap();
    std::fs::write(outside.join("data.txt"), b"keep").unwrap();
    std::fs::remove_dir(f.i.game().join("bin")).unwrap();
    symlink(&outside, f.i.game().join("bin")).unwrap();
    assert!(fs::directory(&f.i.game().join("bin"), false).is_err());
    let plan = manifest::plan(&f.i.game(), &f.raw, 55).unwrap();
    assert_eq!(plan["blocked"], true);
    let mut tx = transaction::Transaction::new(&f.i.game(), &f.work).unwrap();
    assert!(tx.apply(&f.raw, 55, &f.receipt, None, |_| Ok(())).is_err());
    assert_eq!(std::fs::read(outside.join("data.txt")).unwrap(), b"keep");
    let root = fs::directory(f.temp.path(), false).unwrap();
    assert!(fs::open(&root, "../outside", false, false).is_err());
}
#[test]
fn gzip_cached_stage_validates_compressed_and_expanded() {
    let f = fixture();
    let content = b"verified content";
    let mut enc = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::default());
    enc.write_all(content).unwrap();
    let packed = enc.finish().unwrap();
    let raw = manifest_raw(
        vec![record("file", content, &packed)],
        content.len() as u64,
        packed.len() as u64,
    );
    let item = manifest::parse(&raw, 55).unwrap().remove(0);
    let folder = f.temp.path().join("package");
    fs::directory(&folder, true).unwrap();
    fs::atomic(&folder.join("download.gz"), &packed).unwrap();
    let flag = AtomicBool::new(false);
    let output = download::stage(&item, &folder, &flag, |_, _| {}).unwrap();
    assert_eq!(std::fs::read(output).unwrap(), content);
    std::fs::write(folder.join("verified.bin"), b"corrupt").unwrap();
    let mut item = item;
    item["md5"] = json!("a".repeat(32));
    assert!(download::stage(&item, &folder, &flag, |_, _| {}).is_err());
    assert!(!folder.join("verified.bin").exists());
    assert!(!folder.join("expanded.tmp").exists());
}
#[test]
fn cancelled_stage_never_modifies_game() {
    let f = fixture();
    let item = manifest::parse(&f.raw, 55).unwrap().remove(0);
    let flag = AtomicBool::new(true);
    assert!(
        download::stage(&item, &f.temp.path().join("package"), &flag, |_, _| {})
            .unwrap_err()
            .cancelled
    );
    assert!(!f.i.game().join("bin/data.txt").exists());
}
#[test]
fn runtime_files_require_complete_versioned_tree() {
    let f = fixture();
    let folder =
        f.i.prefix
            .join("drive_c/Program Files (x86)/Microsoft/EdgeWebView/Application/155.0.4283.45");
    std::fs::create_dir_all(&folder).unwrap();
    for p in ["msedgewebview2.exe", "msedge.dll", "icudtl.dat"] {
        std::fs::write(folder.join(p), b"runtime").unwrap();
    }
    assert!(runtime::files_in(&[folder.parent().unwrap().to_path_buf()]));
    std::fs::remove_file(folder.join("msedge.dll")).unwrap();
    assert!(!runtime::files_in(&[folder
        .parent()
        .unwrap()
        .to_path_buf()]));
}
#[test]
fn authentication_rotation_retries_once_and_retains_identity() {
    let calls = Arc::new(AtomicUsize::new(0));
    let count = calls.clone();
    let client = auth::Client::testing(move |_, url, _, payload| {
        assert!(url.ends_with("refresh"));
        assert_eq!(payload["refresh_token"], "old-refresh");
        count.fetch_add(1, Ordering::Relaxed);
        Ok(json!({"access_token":"new-access","refresh_token":"new-refresh"}))
    });
    let mut session = Some(auth::Session(
        json!({"access_token":"old-access","refresh_token":"old-refresh","member_no":"123"}),
    ));
    let result = client
        .authenticated(&mut session, |_, s| {
            if s.token() == "old-access" {
                Err(Error {
                    http: Some(401),
                    ..Error::new("HTTP")
                })
            } else {
                Ok(json!({"ok":true}))
            }
        })
        .unwrap();
    assert_eq!(result["ok"], true);
    assert_eq!(calls.load(Ordering::Relaxed), 1);
    assert_eq!(session.as_ref().unwrap().member(), "123");
}
#[test]
fn authentication_http_policy_and_identity_change() {
    let client = auth::Client::testing(|_, _, _, _| {
        Ok(json!({"access_token":"new","refresh_token":"refresh","member":{"member_no":"456"}}))
    });
    let mut session = Some(auth::Session(
        json!({"access_token":"old","refresh_token":"refresh","member_no":"123"}),
    ));
    let error = client
        .authenticated(&mut session, |_, _| {
            Err(Error {
                http: Some(401),
                ..Error::new("HTTP")
            })
        })
        .unwrap_err();
    assert!(error.expired);
    assert_eq!(session.as_ref().unwrap().member(), "123");
}
#[test]
fn qr_begin_poll_exchange_preserves_native_headers() {
    let routes = Arc::new(Mutex::new(Vec::new()));
    let captured = routes.clone();
    let mut client = auth::Client::testing(move |_, url, headers, payload| {
        captured.lock().unwrap().push(url.to_owned());
        if url.ends_with("/qr") {
            assert_eq!(payload["login_type"], "QR_LOGIN_LAUNCHER");
            Ok(
                json!({"session":"test-qr","image":"iVBORw0KGgo=","expires_in":(now()*1000.0+60000.0) as u64}),
            )
        } else if url.contains("qr/status") {
            Ok(json!({"session":"test-qr","status":"success"}))
        } else {
            assert_eq!(headers["User-Agent"], "STOVEAPIClient/3.0");
            assert_eq!(headers["Caller-Detail"], "a".repeat(40));
            assert_eq!(
                headers["X-Utc-Offset"],
                payload["gds_info"]["utc_offset"]
                    .as_i64()
                    .unwrap()
                    .to_string()
            );
            assert_eq!(payload["provider_data"]["qr_login_session"], "test-qr");
            Ok(
                json!({"access_token":"synthetic-token","refresh_token":"synthetic-refresh","member":{"member_no":"123"}}),
            )
        }
    });
    assert!(!client.begin().unwrap().is_empty());
    let (status, session) = client.poll().unwrap();
    assert_eq!(status, 2);
    assert_eq!(session.unwrap().member(), "123");
    assert!(client.poll().is_err());
}
#[test]
fn rpc_snapshot_never_exposes_session_or_challenge() {
    let f = fixture();
    *install::TEST_LOCATION.lock().unwrap() = Some(f.temp.path().join("private"));
    let shared = Arc::new(Mutex::new(engine::Engine::new(
        Some(f.i.root.clone()),
        f.temp.path().join("pipe-bridge.exe"),
    )));
    shared.lock().unwrap().session = Some(auth::Session(
        json!({"access_token":"PRIVATE_ACCESS","refresh_token":"PRIVATE_REFRESH","member_no":"PRIVATE_MEMBER"}),
    ));
    let state = engine::Engine::dispatch(&shared, "state", &json!({}));
    let raw = state.to_string();
    for secret in ["PRIVATE_ACCESS", "PRIVATE_REFRESH", "PRIVATE_MEMBER"] {
        assert!(!raw.contains(secret));
    }
    assert_eq!(state["connected"], true);
    assert!(engine::Engine::dispatch(
        &shared,
        "settings",
        &json!({"root":f.i.root,"runner":"wine"})
    )["error"]
        .is_string());
    let shutdown = engine::Engine::dispatch(&shared, "shutdown", &json!({}));
    assert_eq!(shutdown["closed"], true);
    assert!(shared.lock().unwrap().session.is_none());
    *install::TEST_LOCATION.lock().unwrap() = None;
}
#[test]
fn rpc_unknown_action_and_no_session_have_no_effect() {
    let f = fixture();
    *install::TEST_LOCATION.lock().unwrap() = Some(f.temp.path().join("private"));
    let shared = Arc::new(Mutex::new(engine::Engine::new(
        Some(f.i.root.clone()),
        f.temp.path().join("pipe-bridge.exe"),
    )));
    let state = engine::Engine::dispatch(&shared, "unknown", &json!({}));
    assert_eq!(state["error"], "Operación no disponible.");
    assert_eq!(
        engine::Engine::dispatch(&shared, "launch", &json!({}))["connected"],
        false
    );
    assert!(!f.i.game().join("bin/data.txt").exists());
    *install::TEST_LOCATION.lock().unwrap() = None;
}
#[test]
fn uninstall_confirmation_rejected_before_trash() {
    let f = fixture();
    assert!(engine::uninstall(&f.i, "wrong path").is_err());
    assert!(f.i.game().exists());
}
#[test]
fn native_http_never_sends_tokens_to_unapproved_destinations() {
    let value = json!({"method":"POST","url":"https://evil.invalid/qr","headers":{"Authorization":"PRIVATE_TOKEN"},"payload":{}});
    let error = auth::checked(0, &value).unwrap_err();
    assert!(!error.message.contains("PRIVATE_TOKEN"));
}
#[cfg(target_os = "linux")]
#[test]
fn pidfd_close_only_signals_owned_process() {
    use std::process::{Command, Stdio};
    let mut owned = Command::new("sleep")
        .arg("30")
        .stdout(Stdio::null())
        .spawn()
        .unwrap();
    let mut other = Command::new("sleep")
        .arg("30")
        .stdout(Stdio::null())
        .spawn()
        .unwrap();
    let pid = owned.id();
    let start = processes::identity(pid).unwrap();
    processes::signal(pid, start + 1, false).unwrap();
    assert!(owned.try_wait().unwrap().is_none());
    processes::signal(pid, start, false).unwrap();
    assert!(!owned.wait().unwrap().success());
    assert!(other.try_wait().unwrap().is_none());
    let _ = other.kill();
    let _ = other.wait();
}

fn wait_job(shared: &engine::Shared) -> serde_json::Value {
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    loop {
        let state = engine::Engine::dispatch(shared, "state", &json!({}));
        if state["job"]["running"] != true {
            return state;
        }
        assert!(std::time::Instant::now() < deadline);
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
}
#[test]
fn rpc_background_version_rotation_and_privacy() {
    let f = fixture();
    *install::TEST_LOCATION.lock().unwrap() = Some(f.temp.path().join("private"));
    let shared = Arc::new(Mutex::new(engine::Engine::new(
        Some(f.i.root.clone()),
        f.temp.path().join("helper"),
    )));
    let count = Arc::new(AtomicUsize::new(0));
    let seen = count.clone();
    let client = auth::Client::testing(move |_, url, headers, _| {
        if url.ends_with("refresh") {
            return Ok(
                json!({"access_token":"PRIVATE_NEW","refresh_token":"PRIVATE_REFRESH","member":{"member_no":"123"}}),
            );
        }
        assert_eq!(headers["ticket"].as_str().unwrap().len(), 64);
        if seen.fetch_add(1, Ordering::Relaxed) == 0 {
            return Err(Error {
                http: Some(401),
                ..Error::new("HTTP")
            });
        }
        assert_eq!(headers["Authorization"], "bearer PRIVATE_NEW");
        Ok(json!({"live_version":55,"live_project_url":manifest::default_url(55)}))
    });
    {
        let mut e = shared.lock().unwrap();
        e.client = Some(client);
        e.session = Some(auth::Session(
            json!({"access_token":"PRIVATE_OLD","refresh_token":"PRIVATE_OLD_REFRESH","member_no":"123"}),
        ));
    }
    assert!(engine::Engine::dispatch(&shared, "check_updates", &json!({}))["error"].is_null());
    let state = wait_job(&shared);
    assert_eq!(state["job"]["result"]["published_version"], 55);
    assert_eq!(count.load(Ordering::Relaxed), 2);
    assert!(!state.to_string().contains("PRIVATE"));
    assert_eq!(
        shared.lock().unwrap().session.as_ref().unwrap().token(),
        "PRIVATE_NEW"
    );
    *install::TEST_LOCATION.lock().unwrap() = None;
}
#[test]
fn rpc_expired_refresh_preserves_open_game_control() {
    let f = fixture();
    *install::TEST_LOCATION.lock().unwrap() = Some(f.temp.path().join("private"));
    let shared = Arc::new(Mutex::new(engine::Engine::new(
        Some(f.i.root.clone()),
        f.temp.path().join("helper"),
    )));
    {
        let mut e = shared.lock().unwrap();
        e.client = Some(auth::Client::testing(|_, _, _, _| {
            Err(Error {
                http: Some(403),
                ..Error::new("HTTP")
            })
        }));
        e.session = Some(auth::Session(
            json!({"access_token":"test","refresh_token":"test"}),
        ));
        e.launcher = Some(launch::Launcher::testing());
    }
    engine::Engine::dispatch(&shared, "renew_session", &json!({}));
    let state = wait_job(&shared);
    assert_eq!(state["reauth_required"], true);
    assert_eq!(state["connected"], false);
    assert!(shared.lock().unwrap().launcher.is_some());
    *install::TEST_LOCATION.lock().unwrap() = None;
}
#[test]
fn rpc_apply_update_background_commits_and_clears_prepared() {
    let f = fixture();
    metadata(&f);
    *install::TEST_LOCATION.lock().unwrap() = Some(f.temp.path().join("private"));
    let shared = Arc::new(Mutex::new(engine::Engine::new(
        Some(f.i.root.clone()),
        f.temp.path().join("helper"),
    )));
    shared.lock().unwrap().prepared = Some(f.work.clone());
    engine::Engine::dispatch(&shared, "apply_update", &json!({}));
    let state = wait_job(&shared);
    assert!(state["job"]["error"].is_null());
    assert_eq!(state["version"], 55);
    assert_eq!(state["prepared"], false);
    assert_eq!(state["job"]["result"]["applied"], true);
    *install::TEST_LOCATION.lock().unwrap() = None;
}
#[cfg(target_os = "linux")]
#[test]
fn rpc_stop_retains_button_and_only_closes_owned_child() {
    let f = fixture();
    *install::TEST_LOCATION.lock().unwrap() = Some(f.temp.path().join("private"));
    let shared = Arc::new(Mutex::new(engine::Engine::new(
        Some(f.i.root.clone()),
        f.temp.path().join("helper"),
    )));
    let mut tracker = processes::Tracker::new(f.i.clone()).unwrap();
    let mut child = std::process::Command::new("sleep")
        .arg("30")
        .spawn()
        .unwrap();
    let mut other = std::process::Command::new("sleep")
        .arg("30")
        .spawn()
        .unwrap();
    tracker
        .owned
        .insert(child.id(), processes::identity(child.id()).unwrap());
    tracker.seen = true;
    {
        let mut e = shared.lock().unwrap();
        e.tracker = Some(tracker);
        e.launcher = Some(launch::Launcher::testing());
    }
    let started = engine::Engine::dispatch(&shared, "stop_game", &json!({}));
    assert_eq!(started["game_running"], true);
    let state = wait_job(&shared);
    assert_eq!(state["job"]["result"]["closed"], true);
    assert_eq!(state["game_running"], false);
    assert!(!child.wait().unwrap().success());
    assert!(other.try_wait().unwrap().is_none());
    let _ = other.kill();
    let _ = other.wait();
    *install::TEST_LOCATION.lock().unwrap() = None;
}

#[test]
fn regional_headers_are_real_context_and_not_country_from_language() {
    let mut client = auth::Client::testing(|_, _, headers, payload| {
        assert_eq!(headers["Accept-Language"], "es-ES");
        assert_eq!(headers["X-Lang"], "es");
        assert_eq!(headers["X-Nation"], "CA");
        assert_eq!(payload["gds_info"]["nation"], "CA");
        assert_eq!(headers["X-Timezone"], payload["gds_info"]["timezone"]);
        assert_eq!(
            headers["X-Utc-Offset"],
            payload["gds_info"]["utc_offset"]
                .as_i64()
                .unwrap()
                .to_string()
        );
        Ok(json!({"ok":true}))
    });
    client.region = region::Region {
        country: "CA".into(),
        language: "es-ES".into(),
    };
    client
        .request(
            "POST",
            "https://s-api.onstove.com/sign/v2.1/pc/signin",
            json!({"gds_info":client.region.gds().unwrap()}),
            true,
            serde_json::Value::Null,
        )
        .unwrap();
    client.region.country.clear();
    assert!(client.region.gds().is_err());
    assert_eq!(region::language("es_DO.UTF-8"), "es-DO");
    assert_eq!(region::language("en-US\r\nAuthorization: bad"), "en");
}

#[test]
fn remembered_restore_uses_rt_and_rejects_identity_change_without_retry() {
    let calls = Arc::new(AtomicUsize::new(0));
    let count = calls.clone();
    let client = auth::Client::testing(move |_, url, _, payload| {
        assert!(url.ends_with("/sign/v2.1/pc/signin"));
        assert_eq!(payload["provider_cd"], "RT");
        assert_eq!(payload["provider_data"]["refresh_token"], "saved-refresh");
        assert!(payload["provider_data"].get("qr_login_session").is_none());
        count.fetch_add(1, Ordering::Relaxed);
        Ok(json!({"access_token":"new","refresh_token":"rotated","member":{"member_no":"456"}}))
    });
    let old = auth::Session(json!({"refresh_token":"saved-refresh","member_no":"123"}));
    assert!(client.restore(&old).err().unwrap().expired);
    assert_eq!(calls.load(Ordering::Relaxed), 1);
}

#[test]
fn rpc_remember_restore_rotation_logout_and_configuration_privacy() {
    let f = fixture();
    *install::TEST_LOCATION.lock().unwrap() = Some(f.temp.path().join("private"));
    remember::delete().unwrap();
    let make = || {
        Arc::new(Mutex::new(engine::Engine::new(
            Some(f.i.root.clone()),
            f.temp.path().join("helper"),
        )))
    };
    let shared = make();
    assert_eq!(
        engine::Engine::dispatch(&shared, "state", &json!({}))["remember_session"],
        false
    );
    {
        let mut e = shared.lock().unwrap();
        e.session = Some(auth::Session(
            json!({"access_token":"PRIVATE_ACCESS","refresh_token":"PRIVATE_REFRESH","member_no":"123"}),
        ));
    }
    let enabled = engine::Engine::dispatch(
        &shared,
        "account_settings",
        &json!({"remember_session":true}),
    );
    assert_eq!(enabled["remember_session"], true);
    assert!(enabled["error"].is_null());
    install::save_settings(&f.i, "wine").unwrap();
    assert_eq!(install::settings()["remember_session"], true);
    assert!(!install::settings().to_string().contains("PRIVATE"));
    shared.lock().unwrap().close();
    let reopened = make();
    let calls = Arc::new(AtomicUsize::new(0));
    let count = calls.clone();
    reopened.lock().unwrap().client = Some(auth::Client::testing(move |_, _, _, payload| {
        assert_eq!(payload["provider_cd"], "RT");
        assert_eq!(payload["provider_data"]["refresh_token"], "PRIVATE_REFRESH");
        count.fetch_add(1, Ordering::Relaxed);
        Ok(
            json!({"access_token":"PRIVATE_ROTATED_ACCESS","refresh_token":"PRIVATE_ROTATED_REFRESH","member":{"member_no":"123"}}),
        )
    }));
    engine::Engine::dispatch(&reopened, "restore_session", &json!({}));
    let state = wait_job(&reopened);
    assert_eq!(state["connected"], true);
    assert!(!state.to_string().contains("PRIVATE"));
    assert_eq!(
        remember::load().unwrap().unwrap().0["refresh_token"],
        "PRIVATE_ROTATED_REFRESH"
    );
    for _ in 0..5 {
        engine::Engine::dispatch(&reopened, "restore_session", &json!({}));
    }
    assert_eq!(calls.load(Ordering::Relaxed), 1);
    let state = engine::Engine::dispatch(&reopened, "logout", &json!({}));
    assert_eq!(state["connected"], false);
    assert_eq!(state["remember_session"], false);
    assert!(remember::load().unwrap().is_none());
    *install::TEST_LOCATION.lock().unwrap() = None;
}

#[test]
fn vault_failure_keeps_current_session_and_never_saves_plaintext() {
    let f = fixture();
    *install::TEST_LOCATION.lock().unwrap() = Some(f.temp.path().join("private"));
    let shared = Arc::new(Mutex::new(engine::Engine::new(
        Some(f.i.root.clone()),
        f.temp.path().join("helper"),
    )));
    shared.lock().unwrap().session = Some(auth::Session(
        json!({"access_token":"PRIVATE_ACCESS","refresh_token":"PRIVATE_REFRESH","member_no":"123"}),
    ));
    remember::TEST_FAIL.store(true, Ordering::Relaxed);
    let state = engine::Engine::dispatch(
        &shared,
        "account_settings",
        &json!({"remember_session":true}),
    );
    remember::TEST_FAIL.store(false, Ordering::Relaxed);
    assert_eq!(state["connected"], true);
    assert_eq!(state["remember_session"], false);
    assert!(state["account_warning"].is_string());
    assert!(!install::settings().to_string().contains("PRIVATE"));
    remember::delete().unwrap();
    *install::TEST_LOCATION.lock().unwrap() = None;
}

#[test]
fn restore_network_failure_is_not_retried_by_polling_or_interpreted_as_expiration() {
    let f = fixture();
    *install::TEST_LOCATION.lock().unwrap() = Some(f.temp.path().join("private"));
    remember::save(&auth::Session(
        json!({"refresh_token":"PRIVATE_REFRESH","member_no":"123"}),
    ))
    .unwrap();
    fs::atomic_json(
        &install::config().join("settings.json"),
        &json!({"remember_session":true}),
    )
    .unwrap();
    let shared = Arc::new(Mutex::new(engine::Engine::new(
        Some(f.i.root.clone()),
        f.temp.path().join("helper"),
    )));
    let calls = Arc::new(AtomicUsize::new(0));
    let count = calls.clone();
    shared.lock().unwrap().client = Some(auth::Client::testing(move |_, _, _, _| {
        count.fetch_add(1, Ordering::Relaxed);
        Err(Error::new("network"))
    }));
    engine::Engine::dispatch(&shared, "restore_session", &json!({}));
    let state = wait_job(&shared);
    assert_eq!(state["remember_session"], true);
    assert_eq!(state["reauth_required"], false);
    for _ in 0..5 {
        engine::Engine::dispatch(&shared, "poll", &json!({}));
        engine::Engine::dispatch(&shared, "restore_session", &json!({}));
    }
    assert_eq!(calls.load(Ordering::Relaxed), 1);
    assert!(remember::load().unwrap().is_some());
    remember::delete().unwrap();
    *install::TEST_LOCATION.lock().unwrap() = None;
}

#[test]
fn closing_during_restore_retains_rotated_refresh_without_retaining_access() {
    let f = fixture();
    *install::TEST_LOCATION.lock().unwrap() = Some(f.temp.path().join("private"));
    remember::save(&auth::Session(
        json!({"refresh_token":"PRIVATE_OLD","member_no":"123"}),
    ))
    .unwrap();
    fs::atomic_json(
        &install::config().join("settings.json"),
        &json!({"remember_session":true}),
    )
    .unwrap();
    let shared = Arc::new(Mutex::new(engine::Engine::new(
        Some(f.i.root.clone()),
        f.temp.path().join("helper"),
    )));
    let barrier = Arc::new(std::sync::Barrier::new(2));
    let gate = barrier.clone();
    shared.lock().unwrap().client = Some(auth::Client::testing(move |_, _, _, _| {
        gate.wait();
        Ok(
            json!({"access_token":"PRIVATE_NEW_ACCESS","refresh_token":"PRIVATE_NEW_REFRESH","member":{"member_no":"123"}}),
        )
    }));
    engine::Engine::dispatch(&shared, "restore_session", &json!({}));
    shared.lock().unwrap().close();
    barrier.wait();
    wait_job(&shared);
    assert!(shared.lock().unwrap().session.is_none());
    assert_eq!(
        remember::load().unwrap().unwrap().0["refresh_token"],
        "PRIVATE_NEW_REFRESH"
    );
    remember::delete().unwrap();
    *install::TEST_LOCATION.lock().unwrap() = None;
}

#[test]
fn country_is_automatic_and_never_inferred_from_utc_offset_or_language_alone() {
    assert_eq!(
        region::country(Some("America/Santo_Domingo"), "en-US"),
        "DO"
    );
    assert_eq!(region::country(Some("America/Toronto"), "es-ES"), "CA");
    assert_eq!(region::country(Some("Europe/Madrid"), "en-US"), "ES");
    assert_eq!(region::country(Some("Etc/UTC"), "es_MX.UTF-8"), "MX");
    assert_eq!(region::country(None, "zh-Hant-TW"), "TW");
    assert_eq!(region::country(Some("Etc/UTC"), "en_us.UTF-8"), "US");
    assert_eq!(region::country(Some("Etc/GMT+4"), "en"), "");
    assert_eq!(region::country(None, "C.UTF-8"), "");
}

#[test]
fn extra_environment_parses_and_rejects_prefix_override() {
    let vars =
        crate::install::parse_env("# note\n\n__GLX_VENDOR_LIBRARY_NAME=mesa\nDXVK_HUD = fps\n")
            .unwrap();
    assert_eq!(
        vars,
        vec![
            ("__GLX_VENDOR_LIBRARY_NAME".to_string(), "mesa".to_string()),
            ("DXVK_HUD".to_string(), "fps".to_string())
        ]
    );
    assert!(crate::install::parse_env("NOEQUALS").is_err());
    assert!(crate::install::parse_env("1BAD=x").is_err());
    assert!(crate::install::parse_env("WINEPREFIX=/tmp/x").is_err());
}
