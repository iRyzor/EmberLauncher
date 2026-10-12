#![cfg_attr(windows, windows_subsystem = "windows")]
use ember_engine::{engine::Engine, require, Error, Result};
use serde_json::{json, Value};
use std::{
    io::{BufRead, Read, Write},
    path::PathBuf,
    sync::{Arc, Mutex},
};
fn main() {
    std::panic::set_hook(Box::new(|_| {}));
    if run().is_err() {
        std::process::exit(1);
    }
}
fn run() -> Result<()> {
    if std::env::args().nth(1).as_deref() == Some("--version") {
        println!("ember-engine {} rust", env!("CARGO_PKG_VERSION"));
        return Ok(());
    }
    if std::env::args().nth(1).as_deref() == Some("--inspect-plan") {
        let args: Vec<_> = std::env::args_os().skip(2).collect();
        require(args.len() == 3, "Argumentos inválidos")?;
        let version = args[2]
            .to_string_lossy()
            .parse::<u64>()
            .map_err(|_| Error::new("Versión inválida"))?;
        let raw = ember_engine::fs::read(
            std::path::Path::new(&args[1]),
            ember_engine::manifest::LIMIT,
        )?;
        let plan = ember_engine::manifest::plan(std::path::Path::new(&args[0]), &raw, version)?;
        println!("{}", plan);
        return Ok(());
    }
    let mut root = None;
    let mut helper = None;
    let mut http = None;
    let mut key = None;
    let mut args = std::env::args_os().skip(1);
    while let Some(arg) = args.next() {
        let name = arg.to_string_lossy();
        let value = args
            .next()
            .ok_or_else(|| Error::new("Argumentos inválidos"))?;
        match name.as_ref() {
            "--root" => root = Some(PathBuf::from(value)),
            "--helper" => helper = Some(PathBuf::from(value)),
            "--http" => {
                http = Some(
                    value
                        .to_string_lossy()
                        .parse::<u16>()
                        .map_err(|_| Error::new("Puerto inválido"))?,
                )
            }
            "--key" => key = Some(value.to_string_lossy().to_string()),
            _ => return Err(Error::new("Argumento no permitido")),
        }
    }
    let helper = helper.unwrap_or_else(|| {
        std::env::current_exe()
            .unwrap_or_default()
            .parent()
            .unwrap_or(std::path::Path::new("."))
            .join("../native/pipe-bridge.exe")
    });
    let engine = Arc::new(Mutex::new(Engine::new(root, helper)));
    if let Some(port) = http {
        let key = key.unwrap_or_else(|| uuid::Uuid::new_v4().simple().to_string());
        require(
            !key.is_empty()
                && key.len() <= 256
                && key
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || c == b'-' || c == b'_'),
            "Clave de vista previa inválida",
        )?;
        let server = tiny_http::Server::http(("127.0.0.1", port))
            .map_err(|_| Error::new("No se pudo abrir la vista previa"))?;
        for mut request in server.incoming_requests() {
            let host = request
                .headers()
                .iter()
                .find(|h| h.field.equiv("Host"))
                .map(|h| h.value.as_str())
                .unwrap_or("");
            let origin = request
                .headers()
                .iter()
                .find(|h| h.field.equiv("Origin"))
                .map(|h| h.value.as_str())
                .unwrap_or("");
            let valid = request.method() == &tiny_http::Method::Post
                && matches!(origin, "http://127.0.0.1:5178" | "http://localhost:5178")
                && (host == format!("127.0.0.1:{port}") || host == "127.0.0.1:5178")
                && request.url() == format!("/{key}");
            if !valid {
                let _ = request.respond(tiny_http::Response::empty(403));
                continue;
            }
            if !request.body_length().is_some_and(|s| s > 0 && s < 16384) {
                let _ = request.respond(tiny_http::Response::empty(400));
                continue;
            }
            let mut bytes = Vec::new();
            let result = if request
                .as_reader()
                .take(16384)
                .read_to_end(&mut bytes)
                .is_ok()
            {
                dispatch(&engine, &bytes)
            } else {
                json!({"error":"Petición inválida."})
            };
            let raw = serde_json::to_vec(&result)?;
            let response = tiny_http::Response::from_data(raw)
                .with_header(
                    tiny_http::Header::from_bytes("Content-Type", "application/json").unwrap(),
                )
                .with_header(tiny_http::Header::from_bytes("Cache-Control", "no-store").unwrap());
            let _ = request.respond(response);
        }
    } else {
        let input = std::io::stdin();
        let mut input = input.lock();
        let output = std::io::stdout();
        let mut output = output.lock();
        loop {
            let mut line = Vec::new();
            let size = Read::by_ref(&mut input)
                .take(16385)
                .read_until(b'\n', &mut line)?;
            if size == 0 {
                break;
            }
            if size > 16384 {
                break;
            }
            let shutdown = serde_json::from_slice::<Value>(&line)
                .ok()
                .is_some_and(|v| v["action"] == "shutdown");
            let result = dispatch(&engine, &line);
            serde_json::to_writer(&mut output, &result)?;
            output.write_all(b"\n")?;
            output.flush()?;
            if shutdown {
                break;
            }
        }
    }
    if let Ok(mut engine) = engine.lock() {
        engine.close();
    }
    // Allow an in-flight remembered-session rotation to reach the OS vault before exit.
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(24);
    while std::time::Instant::now() < deadline {
        if engine.try_lock().is_ok_and(|e| !e.authentication_pending()) {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(50));
    }
    Ok(())
}
fn dispatch(engine: &ember_engine::engine::Shared, bytes: &[u8]) -> Value {
    let Ok(value) = serde_json::from_slice::<Value>(bytes) else {
        return json!({"error":"Petición inválida."});
    };
    let Some(action) = value["action"].as_str() else {
        return json!({"error":"Petición inválida."});
    };
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        Engine::dispatch(engine, action, &value["payload"])
    }))
    .unwrap_or_else(|_| json!({"error":"No se pudo completar la operación."}))
}
