//! Private STOVE HTTPS and authentication response contracts.
use base64::{engine::general_purpose::STANDARD, Engine};
use reqwest::{
    blocking::Client,
    header::{HeaderMap, HeaderName, HeaderValue},
    redirect::Policy,
    Method, Url,
};
use serde_json::{json, Value};
use std::{io::Read, time::Duration};
use zeroize::{Zeroize, Zeroizing};
const LIMIT: usize = 1_048_576;

fn error(kind: &str) -> Value {
    json!({"error":kind})
}
fn bounded(value: &Value, min: usize, max: usize) -> bool {
    value
        .as_str()
        .is_some_and(|s| (min..=max).contains(&s.chars().count()))
}
fn wipe(value: &mut Value) {
    match value {
        Value::String(s) => s.zeroize(),
        Value::Array(a) => a.iter_mut().for_each(wipe),
        Value::Object(o) => o.values_mut().for_each(wipe),
        _ => (),
    }
}
fn qr(value: &Value) -> Value {
    if !bounded(&value["session"], 1, 4096) {
        return error("qr_session");
    }
    if !bounded(&value["image"], 0, 262144) {
        return error("qr_image");
    }
    let image = match STANDARD.decode(value["image"].as_str().unwrap()) {
        Ok(v) => v,
        Err(_) => return error("qr_encoding"),
    };
    if !image.starts_with(b"\x89PNG\r\n\x1a\n") {
        return error("qr_png");
    }
    let Some(expires) = value["expires_in"].as_f64() else {
        return error("qr_expiry");
    };
    if !(1e12 < expires && expires < 1e14) {
        return error("qr_expiry");
    }
    json!({"value":{"session":value["session"],"image":value["image"],"expires_at":expires/1000.0}})
}
fn session(value: &Value) -> Value {
    if !bounded(&value["access_token"], 1, 32768) || !bounded(&value["refresh_token"], 1, 32768) {
        return error("tokens");
    }
    for name in ["expires_in", "expire_in"] {
        if !value[name].is_null() && !value[name].as_u64().is_some_and(|n| n > 0) {
            return error("expiry");
        }
    }
    let transaction = value.get("transaction_id").cloned().unwrap_or(json!(""));
    if !bounded(&transaction, 0, 4096) {
        return error("session_metadata");
    }
    let member = value.get("member").cloned().unwrap_or(json!({}));
    if !member.is_object() {
        return error("session_metadata");
    }
    let number = member.get("member_no").cloned().unwrap_or(json!(""));
    let number = match number {
        Value::String(s) => s,
        Value::Number(n) if n.is_i64() || n.is_u64() => n.to_string(),
        _ => return error("account_metadata"),
    };
    json!({"value":{"access_token":value["access_token"],"refresh_token":value["refresh_token"],
        "expires_in":value["expires_in"],"expire_in":value["expire_in"],"transaction_id":transaction,"member_no":number}})
}
fn game(value: &Value) -> Value {
    let mut result = session(value);
    if result.get("error").is_some() {
        return result;
    }
    let user = value.get("user").cloned().unwrap_or(json!({}));
    if !user.is_object() {
        return error("game_metadata");
    }
    let user_id = user.get("user_id").cloned().unwrap_or(json!(""));
    if !bounded(&user_id, 0, 32768) || user_id.as_str().unwrap().contains('\0') {
        return error("game_user");
    }
    result["value"]["user_id"] = user_id;
    result
}
fn envelope(raw: &[u8]) -> Value {
    if raw.len() > LIMIT {
        return error("size");
    }
    let mut result: Value = match serde_json::from_slice(raw) {
        Ok(v) => v,
        Err(_) => return error("json"),
    };
    let output = if !result.is_object() {
        error("envelope")
    } else if result["code"].as_i64() != Some(0) {
        let code = result["code"].as_i64();
        json!({"error":"rejected","code":code})
    } else if !result["value"].is_object() {
        error("value")
    } else {
        json!({"value":result["value"]})
    };
    wipe(&mut result);
    output
}
struct RequestData {
    method: Method,
    url: Url,
    headers: HeaderMap,
    body: Option<Zeroizing<Vec<u8>>>,
}
fn request_data(value: &Value) -> Result<RequestData, Value> {
    let url = Url::parse(value["url"].as_str().ok_or_else(|| error("request"))?)
        .map_err(|_| error("request"))?;
    if url.scheme() != "https"
        || !url.username().is_empty()
        || url.password().is_some()
        || url.fragment().is_some()
        || url.port_or_known_default() != Some(443)
    {
        return Err(error("request"));
    }
    let method = match value["method"].as_str() {
        Some("GET") => Method::GET,
        Some("POST") => Method::POST,
        _ => return Err(error("request")),
    };
    let allowed = match (url.host_str(), url.path(), method.as_str()) {
        (Some("api.onstove.com"), "/auth-secure/v1.0/qr", "POST") => true,
        (Some("api.onstove.com"), "/auth-secure/v1.0/qr/status", "GET") => true,
        (Some("api.onstove.com"), "/dpms/game/v3.1/live_version", "GET") => true,
        (
            Some("s-api.onstove.com"),
            "/sign/v2.1/pc/signin" | "/sign/v2.0/pc/refresh" | "/gc/v1.4/check/STOVE_CHAOSZERO",
            "POST",
        ) => true,
        _ => false,
    };
    if !allowed {
        return Err(error("request"));
    }
    let mut headers = HeaderMap::new();
    let Some(source) = value["headers"].as_object() else {
        return Err(error("headers"));
    };
    for (name, value) in source {
        let Some(text) = value.as_str() else {
            return Err(error("headers"));
        };
        if text.len() > 32775 || text.bytes().any(|c| !(32..=126).contains(&c)) {
            return Err(error("headers"));
        }
        let name = HeaderName::from_bytes(name.as_bytes()).map_err(|_| error("headers"))?;
        // Never accept routing overrides from a caller.
        if ["host", "content-length", "transfer-encoding"].contains(&name.as_str()) {
            return Err(error("headers"));
        }
        let mut header = HeaderValue::from_str(text).map_err(|_| error("headers"))?;
        header.set_sensitive(true);
        headers.insert(name, header);
    }
    let body = if value["payload"].is_null() {
        None
    } else {
        Some(Zeroizing::new(
            serde_json::to_vec(&value["payload"]).map_err(|_| error("request"))?,
        ))
    };
    Ok(RequestData {
        method,
        url,
        headers,
        body,
    })
}
fn client() -> Result<Client, Value> {
    Client::builder()
        .redirect(Policy::none())
        .timeout(Duration::from_secs(10))
        .connect_timeout(Duration::from_secs(10))
        .https_only(true)
        .build()
        .map_err(|_| error("network"))
}
fn send(client: &Client, data: RequestData) -> Value {
    let mut request = client.request(data.method, data.url).headers(data.headers);
    if let Some(body) = data.body {
        request = request.body(body.to_vec());
    }
    let response = match request.send() {
        Ok(r) => r,
        Err(_) => return error("network"),
    };
    let status = response.status().as_u16();
    if !(200..300).contains(&status) {
        return json!({"error":"http","http_status":status});
    }
    let mut raw = Zeroizing::new(Vec::new());
    if response
        .take((LIMIT + 1) as u64)
        .read_to_end(&mut raw)
        .is_err()
    {
        return error("network");
    }
    envelope(&raw)
}
pub fn operation(kind: u32, value: &Value) -> Value {
    match kind {
        0 => match request_data(value).and_then(|r| client().map(|c| (r, c))) {
            Ok((r, c)) => send(&c, r),
            Err(e) => e,
        },
        1 => qr(value),
        2 => session(value),
        3 => game(value),
        5 => match request_data(value) {
            Ok(_) => json!({"value":true}),
            Err(e) => e,
        },
        _ => error("request"),
    }
}
#[no_mangle]
pub extern "C" fn ember_auth_abi() -> u32 {
    1
}
/// # Safety
/// Input/output point to valid non-overlapping ranges during this private call.
/// A request executes once; output capacity must be >= LIMIT+4096 before sending.
#[no_mangle]
pub unsafe extern "C" fn ember_auth_call(
    kind: u32,
    data: *const u8,
    size: usize,
    target: *mut u8,
    capacity: usize,
) -> i32 {
    if data.is_null() || target.is_null() || size > LIMIT || capacity < LIMIT + 4096 {
        return -1;
    }
    std::panic::catch_unwind(|| {
        let raw = std::slice::from_raw_parts(data, size);
        let mut value: Value = match serde_json::from_slice(raw) {
            Ok(v) => v,
            Err(_) => return -1,
        };
        let mut result = if kind == 4 {
            envelope(raw)
        } else {
            operation(kind, &value)
        };
        wipe(&mut value);
        let output = Zeroizing::new(
            serde_json::to_vec(&result).unwrap_or_else(|_| b"{\"error\":\"json\"}".to_vec()),
        );
        wipe(&mut result);
        if output.len() > capacity {
            return -1;
        }
        std::ptr::copy_nonoverlapping(output.as_ptr(), target, output.len());
        output.len() as i32
    })
    .unwrap_or(-1)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn errors_never_forward_remote_details() {
        for raw in [
            br#"{"code":49500,"message":"SECRET","value":"SECRET"}"#.as_slice(),
            br#"{"code":true,"message":"SECRET"}"#,
            br#"{"code":0,"value":"SECRET"}"#,
        ] {
            assert!(!envelope(raw).to_string().contains("SECRET"));
        }
    }
    #[test]
    fn rejects_token_destination_overrides() {
        for url in [
            "http://api.onstove.com/auth-secure/v1.0/qr",
            "https://evil.invalid/auth-secure/v1.0/qr",
            "https://api.onstove.com:444/auth-secure/v1.0/qr",
            "https://user@api.onstove.com/auth-secure/v1.0/qr",
            "https://api.onstove.com/other",
        ] {
            assert!(request_data(&json!({"url":url,"method":"POST","headers":{}})).is_err());
        }
    }
    #[test]
    fn validates_private_session_contract() {
        assert!(
            session(&json!({"access_token":"x","refresh_token":"y","expires_in":true}))
                .get("error")
                .is_some()
        );
        assert_eq!(
            session(&json!({"access_token":"x","refresh_token":"y","member":{"member_no":123}}))
                ["value"]["member_no"],
            "123"
        );
    }
    #[test]
    fn transport_keeps_status_and_does_not_follow_redirect() {
        use std::io::Write;
        use std::net::TcpListener;
        for response in ["HTTP/1.1 401 Unauthorized\r\nContent-Length: 6\r\n\r\nSECRET","HTTP/1.1 302 Found\r\nLocation: https://evil.invalid/SECRET\r\nContent-Length: 0\r\n\r\n"] {
            let listener=TcpListener::bind("127.0.0.1:0").unwrap();
            let address=listener.local_addr().unwrap();
            let worker=std::thread::spawn(move||{let(mut stream,_)=listener.accept().unwrap();let mut bytes=[0u8;2048];stream.read(&mut bytes).unwrap();stream.write_all(response.as_bytes()).unwrap();});
            let client=Client::builder().no_proxy().redirect(Policy::none()).timeout(Duration::from_secs(3)).build().unwrap();
            let data=RequestData{method:Method::GET,url:Url::parse(&format!("http://{address}/")).unwrap(),headers:HeaderMap::new(),body:None};
            let result=send(&client,data);
            assert_eq!(result["error"],"http");
            assert!(!result.to_string().contains("SECRET"));
            assert_eq!(result["http_status"], if response.contains("401"){401}else{302});
            worker.join().unwrap();
        }
    }
}

#[cfg(test)]
mod response_tests {
    use super::*;
    use std::{io::Write, net::TcpListener};
    #[test]
    fn bounded_transport_accepts_json_and_rejects_oversized_body() {
        for oversized in [false, true] {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let address = listener.local_addr().unwrap();
            let body = if oversized {
                vec![b'x'; LIMIT + 1]
            } else {
                br#"{"code":0,"value":{"status":"init"}}"#.to_vec()
            };
            let worker = std::thread::spawn(move || {
                let (mut stream, _) = listener.accept().unwrap();
                let mut bytes = [0u8; 2048];
                stream.read(&mut bytes).unwrap();
                write!(
                    stream,
                    "HTTP/1.1 200 OK\r\nContent-Length: {}\r\n\r\n",
                    body.len()
                )
                .unwrap();
                stream.write_all(&body).unwrap();
            });
            let client = Client::builder()
                .no_proxy()
                .timeout(Duration::from_secs(3))
                .build()
                .unwrap();
            let result = send(
                &client,
                RequestData {
                    method: Method::GET,
                    url: Url::parse(&format!("http://{address}/")).unwrap(),
                    headers: HeaderMap::new(),
                    body: None,
                },
            );
            if oversized {
                assert_eq!(result["error"], "size");
            } else {
                assert_eq!(result["value"]["status"], "init");
            }
            worker.join().unwrap();
        }
    }
}
