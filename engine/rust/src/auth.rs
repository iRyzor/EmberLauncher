use crate::{
    fs,
    install::{config, Installation},
    now, require, text, Error, Result, GAME,
};
use hmac::{Hmac, Mac};
use rand::RngCore;
use serde_json::{json, Value};
use sha2::Sha256;
use std::{
    io::{BufRead, BufReader},
    path::{Path, PathBuf},
};
use zeroize::{Zeroize, Zeroizing};
const PROFILE: &str = "e67af969b3a96e15f529bca26114978c4e089e1e99e4f2c2661b9129bcb48345";
const CLIENT: &str = "5faa0926311687ccc34a598d9640a48909a86a0e93afdf586ab088a3d01a93d3";
pub fn wipe(v: &mut Value) {
    match v {
        Value::String(s) => s.zeroize(),
        Value::Array(a) => a.iter_mut().for_each(wipe),
        Value::Object(o) => o.values_mut().for_each(wipe),
        _ => (),
    }
}
#[derive(Clone)]
pub struct Session(pub Value);
impl Drop for Session {
    fn drop(&mut self) {
        wipe(&mut self.0)
    }
}
impl Session {
    pub fn token(&self) -> String {
        text(&self.0, "access_token")
    }
    pub fn member(&self) -> String {
        text(&self.0, "member_no")
    }
}
pub fn checked(kind: u32, value: &Value) -> Result<Value> {
    let mut result = ember_protocol::auth::operation(kind, value);
    if let Some(kind) = result["error"].as_str() {
        let error = match kind {
            "http" => Error {
                http: result["http_status"].as_i64().map(|s| s as i32),
                ..Error::new(&format!("STOVE respondió HTTP {}.", result["http_status"]))
            },
            "code" => Error::new(&format!(
                "STOVE rechazó la autenticación (código {}).",
                result["code"]
            )),
            "network" => Error::new("No se pudo contactar al servicio de autenticación de STOVE."),
            "tokens" => Error::new("STOVE no devolvió los tokens esperados."),
            _ => Error::new(
                "STOVE devolvió una respuesta inesperada o la petición no está permitida.",
            ),
        };
        wipe(&mut result);
        return Err(error);
    }
    Ok(result["value"].take())
}
#[derive(Clone)]
pub struct Profile {
    pub caller: String,
    pub suffix: String,
}
fn profile_path(root: &Path) -> PathBuf {
    let bytes = root.to_string_lossy();
    config().join(format!(
        "protocol-{}.json",
        &fs::sha(bytes.as_bytes())[..16]
    ))
}
pub fn profile(installation: &Installation) -> Result<Profile> {
    let path = profile_path(&installation.root);
    let value = if path.exists() {
        fs::json(&path)?
    } else {
        let bootstrap: Value =
            serde_json::from_str(include_str!("../../runtime/bootstrap-profile.json"))?;
        let mut caller = String::new();
        let mut suffix = text(&bootstrap, "version_suffix");
        let exe = installation.root.join("STOVE/STOVE.exe");
        let registry = installation.prefix.join("user.reg");
        if exe.is_file() && registry.is_file() {
            require(
                fs::hash_path(&exe, false)? == PROFILE,
                "El perfil HTTP del launcher requiere una nueva validación.",
            )?;
            let reg = fs::directory(&installation.prefix, false)?;
            let file = fs::open(&reg, "user.reg", false, false)?;
            let mut section = String::new();
            let mut matches = Vec::new();
            for line in BufReader::new(file).lines() {
                let mut line = line?;
                if let Some(stripped) = line.strip_prefix('[') {
                    section = stripped
                        .split(']')
                        .next()
                        .unwrap_or("")
                        .replace("\\\\", "\\")
                        .to_lowercase();
                }
                if section == r"software\sgup" && line.starts_with("\"CallerDetail\"=") {
                    let val = line
                        .trim()
                        .strip_prefix("\"CallerDetail\"=\"")
                        .and_then(|v| v.strip_suffix('"'))
                        .unwrap_or("");
                    require(
                        val.len() == 40
                            && val
                                .bytes()
                                .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()),
                        "CallerDetail no tiene el formato esperado.",
                    )?;
                    matches.push(val.to_owned());
                }
                line.zeroize();
            }
            require(matches.len() == 1, "No se encontró un CallerDetail único.")?;
            caller = matches.remove(0);
            let data = fs::read(&exe, 512 * 1024 * 1024)?;
            fn number(data: &[u8], offset: usize, len: usize) -> Result<usize> {
                let slice = data
                    .get(offset..offset + len)
                    .ok_or_else(|| Error::new("Perfil PE inválido"))?;
                Ok(slice
                    .iter()
                    .enumerate()
                    .fold(0usize, |v, (i, b)| v | ((*b as usize) << (i * 8))))
            }
            let pe = number(&data, 0x3c, 4)?;
            let count = number(&data, pe + 6, 2)?;
            let sections = pe + 24 + number(&data, pe + 20, 2)?;
            let mut chunks = Vec::new();
            for rva in [0x34e7a80, 0x34e7c10, 0x34e78b0, 0x34e78a0] {
                let mut found = false;
                for i in 0..count {
                    let e = sections + 40 * i;
                    let address = number(&data, e + 12, 4)?;
                    let size = number(&data, e + 16, 4)?;
                    let offset = number(&data, e + 20, 4)?;
                    if address <= rva && rva + 16 <= address + size {
                        chunks.extend_from_slice(
                            data.get(offset + rva - address..offset + rva - address + 16)
                                .ok_or_else(|| Error::new("Perfil PE inválido"))?,
                        );
                        found = true;
                        break;
                    }
                }
                require(found, "No se encontró la constante de versión.")?;
            }
            suffix =
                String::from_utf8(chunks).map_err(|_| Error::new("Perfil de versión inválido"))?;
        }
        if caller.is_empty() {
            let mut random = [0u8; 20];
            rand::rngs::OsRng.fill_bytes(&mut random);
            caller = random.iter().map(|b| format!("{b:02x}")).collect();
        }
        let value = json!({"profile_hash":PROFILE,"caller_detail":caller,"version_suffix":suffix});
        fs::atomic_json(&path, &value)?;
        value
    };
    require(value["profile_hash"] == PROFILE, "Perfil no compatible.")?;
    let result = Profile {
        caller: text(&value, "caller_detail"),
        suffix: text(&value, "version_suffix"),
    };
    require(
        result.caller.len() == 40
            && result
                .caller
                .bytes()
                .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
            && result.suffix.len() == 64
            && result.suffix.bytes().all(|c| c.is_ascii_hexdigit()),
        "Perfil no válido.",
    )?;
    Ok(result)
}
pub fn transfer(source: &Installation, dest: &Installation) -> Result<()> {
    let p = profile(source)?;
    fs::atomic_json(
        &profile_path(&dest.root),
        &json!({"profile_hash":PROFILE,"caller_detail":p.caller,"version_suffix":p.suffix}),
    )
}
#[cfg(test)]
type TestTransport =
    std::sync::Arc<dyn Fn(&str, &str, &Value, &Value) -> Result<Value> + Send + Sync>;
pub struct Client {
    pub profile: Profile,
    pub transaction: String,
    pub region: crate::region::Region,
    gate: *mut ember_protocol::qr::Gate,
    pub challenge: Option<Value>,
    #[cfg(test)]
    transport: Option<TestTransport>,
}
// The handle has unique ownership; all access requires &mut Client and is serialized by Engine.
unsafe impl Send for Client {}
impl Drop for Client {
    fn drop(&mut self) {
        unsafe { ember_protocol::qr::ember_qr_free(self.gate) };
        if let Some(v) = self.challenge.as_mut() {
            wipe(v);
        }
    }
}
impl Client {
    #[cfg(test)]
    pub fn testing(
        transport: impl Fn(&str, &str, &Value, &Value) -> Result<Value> + Send + Sync + 'static,
    ) -> Self {
        Self {
            profile: Profile {
                caller: "a".repeat(40),
                suffix: "b".repeat(64),
            },
            transaction: uuid::Uuid::new_v4().to_string(),
            region: crate::region::Region {
                country: "DO".into(),
                language: "en-US".into(),
            },
            gate: ember_protocol::qr::ember_qr_new(),
            challenge: None,
            transport: Some(std::sync::Arc::new(transport)),
        }
    }
    pub fn requests_clone(&self) -> Self {
        Self {
            profile: self.profile.clone(),
            transaction: self.transaction.clone(),
            region: self.region.clone(),
            gate: ember_protocol::qr::ember_qr_new(),
            challenge: None,
            #[cfg(test)]
            transport: self.transport.clone(),
        }
    }
    pub fn new(installation: &Installation) -> Result<Self> {
        Ok(Self {
            profile: profile(installation)?,
            transaction: uuid::Uuid::new_v4().to_string(),
            region: crate::region::Region::load(),
            gate: ember_protocol::qr::ember_qr_new(),
            challenge: None,
            #[cfg(test)]
            transport: None,
        })
    }
    pub fn request(
        &self,
        method: &str,
        url: &str,
        payload: Value,
        native: bool,
        extras: Value,
    ) -> Result<Value> {
        let gds = if payload["gds_info"].is_object() {
            payload["gds_info"].clone()
        } else {
            self.region.gds()?
        };
        let mut headers = json!({"Content-Type":"application/json","Accept":"application/json","User-Agent":"StoveLauncher/3.0","Accept-Language":self.region.language});
        if native {
            for (k,v) in json!({"User-Agent":"STOVEAPIClient/3.0","Transaction-ID":self.transaction,"Caller-ID":"STOVE_LAUNCHER_VER.3.2.29.736","Caller-Detail":self.profile.caller,"X-Lang":gds["lang"],"X-Nation":gds["nation"],"X-Timezone":gds["timezone"],"X-Utc-Offset":gds["utc_offset"].as_i64().unwrap().to_string()}).as_object().unwrap(){headers[k]=v.clone();}
        }
        if let Some(values) = extras.as_object() {
            for (k, v) in values {
                headers[k] = v.clone();
            }
        }
        let mut value = json!({"method":method,"url":url,"headers":headers,"payload":payload});
        #[cfg(test)]
        if let Some(transport) = &self.transport {
            let result = transport(method, url, &value["headers"], &value["payload"]);
            wipe(&mut value);
            return result;
        }
        let result = checked(0, &value);
        wipe(&mut value);
        result
    }
    pub fn begin(&mut self) -> Result<String> {
        unsafe { ember_protocol::qr::ember_qr_free(self.gate) };
        self.gate = ember_protocol::qr::ember_qr_new();
        self.challenge = None;
        let mut value = self.request(
            "POST",
            "https://api.onstove.com/auth-secure/v1.0/qr",
            json!({"login_type":"QR_LOGIN_LAUNCHER","width":180,"height":180,"gds_info":self.region.gds()?}),
            false,
            Value::Null,
        )?;
        let result = checked(1, &value);
        wipe(&mut value);
        let challenge = result?;
        let session = text(&challenge, "session");
        let expires = challenge["expires_at"]
            .as_f64()
            .ok_or_else(|| Error::new("QR inválido"))?;
        require(
            unsafe {
                ember_protocol::qr::ember_qr_reset(
                    self.gate,
                    session.as_ptr(),
                    session.len(),
                    expires,
                )
            } == 0,
            "QR inválido",
        )?;
        let image = text(&challenge, "image");
        self.challenge = Some(challenge);
        Ok(image)
    }
    fn qr_step(&mut self, action: u32, value: &Value) -> Result<i32> {
        let challenge = self
            .challenge
            .as_ref()
            .ok_or_else(|| Error::new("Genera un QR nuevo."))?;
        let session = text(challenge, "session");
        let raw = Zeroizing::new(serde_json::to_vec(value)?);
        let result = unsafe {
            ember_protocol::qr::ember_qr_step(
                self.gate,
                action,
                session.as_ptr(),
                session.len(),
                now(),
                raw.as_ptr(),
                raw.len(),
            )
        };
        require(
            result >= 0,
            "La respuesta QR no coincide o requiere un código nuevo.",
        )?;
        Ok(result)
    }
    pub fn poll(&mut self) -> Result<(i32, Option<Session>)> {
        if self.qr_step(0, &Value::Null)? == 4 {
            return Ok((4, None));
        }
        let challenge = self.challenge.as_ref().unwrap();
        let url = reqwest::Url::parse_with_params(
            "https://api.onstove.com/auth-secure/v1.0/qr/status",
            &[("session", text(challenge, "session"))],
        )
        .map_err(|_| Error::new("QR inválido"))?;
        let mut value = self.request("GET", url.as_str(), Value::Null, false, Value::Null)?;
        let step = self.qr_step(1, &value);
        wipe(&mut value);
        let status = step?;
        if status != 2 {
            return Ok((status, None));
        }
        self.qr_step(2, &Value::Null)?;
        let session = text(self.challenge.as_ref().unwrap(), "session");
        let mut value=self.request("POST","https://s-api.onstove.com/sign/v2.1/pc/signin",json!({"client_id":CLIENT,"service_id":"Launcher","provider_cd":"QR","provider_data":{"qr_login_session":session},"gds_info":self.region.gds()?}),true,Value::Null)?;
        let result = checked(2, &value);
        wipe(&mut value);
        Ok((2, Some(Session(result?))))
    }
    pub fn refresh(&self, old: &Session) -> Result<Session> {
        let result=self.request("POST","https://s-api.onstove.com/sign/v2.0/pc/refresh",json!({"client_id":CLIENT,"service_id":"Launcher","refresh_token":old.0["refresh_token"],"gds_info":self.region.gds()?}),true,Value::Null);
        let mut value = match result {
            Err(e) if matches!(e.http, Some(401 | 403)) => return Err(Error::expired()),
            other => other?,
        };
        let checked = checked(2, &value);
        wipe(&mut value);
        let mut new = Session(checked?);
        let previous = Zeroizing::new(old.member());
        let current = Zeroizing::new(new.member());
        require(
            previous.is_empty() || current.is_empty() || *previous == *current,
            "La sesión necesita un nuevo acceso por QR.",
        )
        .map_err(|_| Error::expired())?;
        if current.is_empty() {
            new.0["member_no"] = Value::String(previous.to_string());
        }
        Ok(new)
    }
    /// Reopening uses the observed refresh-token sign-in, not another QR approval.
    pub fn restore(&self, old: &Session) -> Result<Session> {
        let response = self.request("POST", "https://s-api.onstove.com/sign/v2.1/pc/signin", json!({"client_id":CLIENT,"service_id":"Launcher","provider_cd":"RT","provider_data":{"refresh_token":old.0["refresh_token"]},"gds_info":self.region.gds()?}), true, Value::Null);
        let mut value = match response {
            Err(e) if e.http == Some(401) => return Err(Error::expired()),
            other => other?,
        };
        let parsed = checked(2, &value);
        wipe(&mut value);
        let new = Session(parsed?);
        require(
            !old.member().is_empty() && old.member() == new.member(),
            "La sesión guardada corresponde a otra cuenta.",
        )
        .map_err(|_| Error::expired())?;
        Ok(new)
    }
    pub fn authenticated(
        &self,
        session: &mut Option<Session>,
        operation: impl Fn(&Self, &Session) -> Result<Value>,
    ) -> Result<Value> {
        let old = session.as_ref().ok_or_else(Error::expired)?;
        match operation(self, old) {
            Err(e) if e.http == Some(401) => (),
            other => return other,
        }
        let renewed = self.refresh(old)?;
        *session = Some(renewed);
        match operation(self, session.as_ref().unwrap()) {
            Err(e) if e.http == Some(401) => Err(Error::expired()),
            other => other,
        }
    }
    pub fn game(&self, s: &Session) -> Result<Value> {
        let mut value=self.request("POST",&format!("https://s-api.onstove.com/gc/v1.4/check/{GAME}"),json!({"device_info":{"device_key":""},"gds_info":self.region.gds()?,"skip_provider_check":false,"skip_session_check":false}),true,json!({"Authorization":format!("bearer {}",s.token()),"Captcha-Token":"","market-name":"PC_MARKET"}))?;
        let result = checked(3, &value);
        wipe(&mut value);
        let mut result = result?;
        result["_received_at_ms"] = json!((now() * 1000.0) as u64);
        Ok(result)
    }
    pub fn published(&self, s: &Session, installed: u64) -> Result<Value> {
        let member = Zeroizing::new(s.member());
        require(
            !member.is_empty() && member.len() <= 32 && member.bytes().all(|c| c.is_ascii_digit()),
            "La sesión no contiene el identificador necesario para consultar versiones.",
        )?;
        let key = Zeroizing::new(format!("{}:{}", *member, self.profile.suffix));
        let mut mac = Hmac::<Sha256>::new_from_slice(key.as_bytes())
            .map_err(|_| Error::new("Perfil inválido"))?;
        mac.update(GAME.as_bytes());
        let ticket = format!("{:x}", mac.finalize().into_bytes());
        self.request("GET",&format!("https://api.onstove.com/dpms/game/v3.1/live_version?game_id={GAME}&local_version={installed}&pc_room=false"),Value::Null,true,json!({"Authorization":format!("bearer {}",s.token()),"ticket":ticket}))
    }
}
