//! Public editorial content. Independent worker/cache; never uses account sessions.
use crate::{fs, install, require, Error, Result};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    io::Read,
    sync::{Arc, Mutex, OnceLock},
    time::{Duration, Instant},
};
const LIMIT: u64 = 2 * 1024 * 1024;
const TTL: Duration = Duration::from_secs(900);
pub const GAME_KEY: &str = "chaos-zero-nightmare";
const CHANNEL: u64 = 11034;
const BOARDS: &[(u64, &str)] = &[
    (142421, "Actualizaciones"),
    (142488, "Incidencias"),
    (139238, "Anuncios"),
    (139239, "Notas del equipo"),
];
#[derive(Default)]
struct Entry {
    value: Option<Value>,
    loading: bool,
    error: Option<String>,
    attempted: Option<Instant>,
}
#[derive(Default)]
struct Store {
    entries: HashMap<String, Entry>,
}
static STORE: OnceLock<Arc<Mutex<Store>>> = OnceLock::new();
fn source(id: u64) -> String {
    format!("https://page.onstove.com/chaoszeronightmare/en/view/{id}")
}
fn public_get(path: &str) -> Result<Value> {
    let response = reqwest::blocking::Client::builder()
        .https_only(true)
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(12))
        .build()
        .map_err(|_| Error::new("No se pudieron consultar las noticias."))?
        .get(format!("https://api.onstove.com{path}"))
        .header("Accept", "application/json")
        .send()
        .map_err(|_| Error::new("No se pudieron consultar las noticias."))?;
    require(
        response.status().is_success(),
        "No se pudo consultar STOVE.",
    )?;
    let mut bytes = Vec::new();
    response.take(LIMIT + 1).read_to_end(&mut bytes)?;
    require(
        bytes.len() as u64 <= LIMIT,
        "La publicación es demasiado grande.",
    )?;
    let envelope: Value = serde_json::from_slice(&bytes)?;
    require(
        envelope["code"].as_i64() == Some(0),
        "STOVE no pudo devolver las noticias.",
    )?;
    Ok(envelope["value"].clone())
}
fn bounded(v: &Value, key: &str, max: usize) -> String {
    v[key].as_str().unwrap_or("").chars().take(max).collect()
}
fn official(v: &Value) -> bool {
    v["channel_seq"].as_u64() == Some(CHANNEL)
        && BOARDS
            .iter()
            .any(|(id, _)| v["board_seq"].as_u64() == Some(*id))
        && v["user_info"]["channel_grade_code"] == "official"
}
pub fn image_url(value: &str) -> Option<String> {
    let absolute = if value.starts_with("//") {
        format!("https:{value}")
    } else {
        value.to_string()
    };
    let url = reqwest::Url::parse(&absolute).ok()?;
    (url.scheme() == "https"
        && url.host_str() == Some("d2x8kymwjom7h7.cloudfront.net")
        && url.username().is_empty()
        && url.password().is_none()
        && url.port().is_none())
    .then_some(absolute)
}
fn number(v: &Value) -> Option<u64> {
    v.as_u64().or_else(|| {
        v.as_str()
            .filter(|s| !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit()))
            .and_then(|s| s.parse().ok())
    })
}
fn item(v: &Value) -> Result<Value> {
    require(official(v), "Publicación no oficial o de otro juego.")?;
    let id = number(&v["article_id"])
        .filter(|v| *v > 0 && *v <= 9_007_199_254_740_991)
        .ok_or_else(|| Error::new("Publicación inválida."))?;
    Ok(
        json!({"id":id,"title":bounded(v,"title",300),"summary":bounded(v,"summary",600),"category":BOARDS.iter().find(|(id,_)|v["board_seq"].as_u64()==Some(*id)).unwrap().1,"published_at":number(&v["create_datetime"]).filter(|v|*v<=253_402_300_799_999).unwrap_or(0),"updated_at":number(&v["update_datetime"]).unwrap_or(0),"image":image_url(v["media_thumbnail_url"].as_str().unwrap_or("")),"url":source(id)}),
    )
}
fn feed() -> Result<Value> {
    let workers:Vec<_>=BOARDS.iter().map(|&(id,_)|std::thread::spawn(move||public_get(&format!("/cwms/v3.0/article_group/BOARD/{id}/article/list?content_yn=Y&summary_yn=Y&sort_type_code=LATEST&translation_yn=N&page=1&size=6")))).collect();
    let mut items = Vec::new();
    let mut failures = 0;
    for worker in workers {
        match worker.join().ok().and_then(|r| r.ok()) {
            Some(v) => {
                if let Some(list) = v["list"].as_array() {
                    items.extend(list.iter().filter_map(|v| item(v).ok()));
                } else {
                    failures += 1;
                }
            }
            None => failures += 1,
        }
    }
    require(
        failures < BOARDS.len(),
        "No se pudieron cargar las noticias. Puedes seguir jugando.",
    )?;
    items.sort_by_key(|v| std::cmp::Reverse(v["published_at"].as_u64().unwrap_or(0)));
    items.dedup_by_key(|v| v["id"].as_u64());
    items.truncate(24);
    Ok(
        json!({"game_id":GAME_KEY,"items":items,"partial":failures>0,"fetched_at":crate::now()*1000.0}),
    )
}
fn article(id: u64) -> Result<Value> {
    let v = public_get(&format!(
        "/cwms/v3.0/article?article_id={id}&translation_yn=N&request_id=CM"
    ))?;
    let mut result = item(&v)?;
    require(result["id"].as_u64() == Some(id), "Publicación inesperada.")?;
    let content = v["content"]
        .as_str()
        .ok_or_else(|| Error::new("Contenido no disponible."))?;
    require(
        content.len() <= 128 * 1024,
        "La publicación es demasiado grande. Consulta el original en STOVE.",
    )?;
    result["content"] = json!(content);
    Ok(json!({"article":result,"fetched_at":crate::now()*1000.0}))
}
fn valid_cache(v: &Value, id: Option<u64>) -> bool {
    let valid_item = |item: &Value| {
        item["id"]
            .as_u64()
            .is_some_and(|id| id > 0 && id <= 9_007_199_254_740_991)
            && item["title"].as_str().is_some_and(|s| s.len() <= 1200)
            && item["summary"].as_str().is_some_and(|s| s.len() <= 2400)
            && item["category"]
                .as_str()
                .is_some_and(|s| BOARDS.iter().any(|(_, label)| s == *label))
            && item["published_at"]
                .as_u64()
                .is_some_and(|v| v <= 253_402_300_799_999)
            && item["url"].as_str() == item["id"].as_u64().map(source).as_deref()
            && (item["image"].is_null()
                || item["image"]
                    .as_str()
                    .is_some_and(|s| image_url(s).is_some()))
    };
    v["fetched_at"]
        .as_f64()
        .is_some_and(|n| n > 0.0 && n <= 253_402_300_799_999.0)
        && if let Some(id) = id {
            v["article"]["id"].as_u64() == Some(id)
                && valid_item(&v["article"])
                && v["article"]["content"]
                    .as_str()
                    .is_some_and(|s| s.len() <= 128 * 1024)
        } else {
            v["game_id"] == GAME_KEY
                && v["items"]
                    .as_array()
                    .is_some_and(|items| items.len() <= 24 && items.iter().all(valid_item))
        }
}
/// Returns immediately. Network operations run outside both engine and store locks.
pub fn request(payload: &Value) -> Value {
    if payload["game_id"].as_str() != Some(GAME_KEY) {
        return json!({"error":"Este juego no tiene noticias configuradas."});
    }
    let id = payload["article_id"].as_u64();
    if !payload["article_id"].is_null() && id.is_none() {
        return json!({"error":"Publicación inválida."});
    }
    let key = id
        .map(|v| format!("article-{v}"))
        .unwrap_or_else(|| "feed".into());
    let store = STORE
        .get_or_init(|| Arc::new(Mutex::new(Store::default())))
        .clone();
    let mut state = match store.lock() {
        Ok(v) => v,
        Err(_) => return json!({"error":"Noticias no disponibles."}),
    };
    if let Some(id) = id {
        let known = state
            .entries
            .get("feed")
            .and_then(|e| e.value.as_ref())
            .and_then(|v| v["items"].as_array())
            .is_some_and(|a| a.iter().any(|v| v["id"].as_u64() == Some(id)));
        if !known {
            return json!({"error":"Selecciona una publicación del tablón."});
        }
    }
    if !state.entries.contains_key(&key) && state.entries.len() >= 50 {
        let oldest = state
            .entries
            .iter()
            .filter(|(k, e)| k.as_str() != "feed" && !e.loading)
            .min_by_key(|(_, e)| e.attempted)
            .map(|(k, _)| k.clone());
        if let Some(oldest) = oldest {
            state.entries.remove(&oldest);
        }
    }
    // Public cache only. Filenames are generated from a validated numeric ID.
    let path = install::cache().join("news").join(format!("{key}.json"));
    let e = state.entries.entry(key.clone()).or_default();
    if e.value.is_none() {
        if let Ok(bytes) = fs::read(&path, 1024 * 1024) {
            if let Ok(v) = serde_json::from_slice::<Value>(&bytes) {
                if valid_cache(&v, id) {
                    e.value = Some(v);
                }
            }
        }
    }
    let refresh = !e.loading
        && e.attempted.is_none_or(|t| {
            t.elapsed() >= TTL
                || ((e.error.is_some() || payload["refresh"] == true)
                    && t.elapsed() >= Duration::from_secs(10))
        });
    if refresh {
        e.loading = true;
        e.attempted = Some(Instant::now());
        e.error = None;
        let store = store.clone();
        std::thread::spawn(move || {
            let result = std::panic::catch_unwind(|| {
                if let Some(id) = id {
                    article(id)
                } else {
                    feed()
                }
            })
            .unwrap_or_else(|_| Err(Error::new("No se pudieron cargar las noticias.")));
            if let Ok(value) = &result {
                if let Some(parent) = path.parent() {
                    if fs::directory(parent, true).is_ok() {
                        let _ = fs::atomic_json(&path, value);
                    }
                }
            }
            if let Ok(mut state) = store.lock() {
                if let Some(e) = state.entries.get_mut(&key) {
                    e.loading = false;
                    match result {
                        Ok(v) => e.value = Some(v),
                        Err(_) => {
                            e.error = Some(
                                "No se pudo actualizar el contenido. Puedes seguir jugando.".into(),
                            )
                        }
                    }
                }
            }
        });
    }
    json!({"news":e.value,"loading":e.loading,"error":e.error,"cached":e.value.is_some() && e.loading})
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn media_rejects_external_credentials_and_schemes() {
        assert!(image_url("//d2x8kymwjom7h7.cloudfront.net/a.png").is_some());
        for url in [
            "javascript:alert(1)",
            "http://d2x8kymwjom7h7.cloudfront.net/a",
            "https://evil.example/a",
            "https://user@d2x8kymwjom7h7.cloudfront.net/a",
        ] {
            assert!(image_url(url).is_none());
        }
    }
    #[test]
    fn only_editorial_boards_and_official_channel_are_accepted() {
        let mut v = json!({"channel_seq":CHANNEL,"board_seq":139238,"article_id":12,"title":"Notice","user_info":{"channel_grade_code":"official"}});
        assert!(item(&v).is_ok());
        v["article_id"] = json!("12");
        assert_eq!(item(&v).unwrap()["id"], 12);
        v["channel_seq"] = json!(123);
        assert!(item(&v).is_err());
        v["channel_seq"] = json!(CHANNEL);
        v["user_info"]["channel_grade_code"] = json!("user");
        assert!(item(&v).is_err());
    }
    #[test]
    fn corrupted_cache_is_rejected() {
        assert!(!valid_cache(
            &json!({"game_id":GAME_KEY,"items":[{"id":1}],"fetched_at":10}),
            None
        ));
        assert!(!valid_cache(
            &json!({"article":{"id":1,"content":"x"},"fetched_at":10}),
            Some(1)
        ));
    }
    #[test]
    fn unknown_games_and_unlisted_articles_never_fetch() {
        assert!(request(&json!({"game_id":"unknown"}))["error"].is_string());
        assert!(request(&json!({"game_id":GAME_KEY,"article_id":999999999}))["error"].is_string());
    }
}
