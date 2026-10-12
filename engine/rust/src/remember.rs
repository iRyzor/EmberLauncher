//! Optional refresh credentials go only to the OS credential store. No file fallback.
use crate::{auth::Session, require, Error, Result};
use serde_json::{json, Value};
use zeroize::Zeroizing;
#[cfg(not(test))]
const SERVICE: &str = "com.emberlauncher.session";
#[cfg(not(test))]
const ACCOUNT: &str = "stove-refresh-v1";
const UNAVAILABLE: &str = "No se pudo acceder al almacén seguro del sistema. La sesión actual sigue disponible en memoria.";
#[cfg(not(test))]
fn entry() -> Result<keyring::Entry> {
    keyring::Entry::new(SERVICE, ACCOUNT).map_err(|_| Error::new(UNAVAILABLE))
}
#[cfg(test)]
static STORE: std::sync::Mutex<Option<Zeroizing<String>>> = std::sync::Mutex::new(None);
#[cfg(test)]
pub static TEST_FAIL: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
fn read() -> Result<Option<Zeroizing<String>>> {
    #[cfg(test)]
    {
        if TEST_FAIL.load(std::sync::atomic::Ordering::Relaxed) {
            return Err(Error::new(UNAVAILABLE));
        }
        return Ok(STORE.lock().unwrap().clone());
    }
    #[cfg(not(test))]
    match entry()?.get_password() {
        Ok(value) => Ok(Some(Zeroizing::new(value))),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(_) => Err(Error::new(UNAVAILABLE)),
    }
}
pub fn delete() -> Result<()> {
    #[cfg(test)]
    {
        if TEST_FAIL.load(std::sync::atomic::Ordering::Relaxed) {
            return Err(Error::new(UNAVAILABLE));
        }
        *STORE.lock().unwrap() = None;
        return Ok(());
    }
    #[cfg(not(test))]
    match entry()?.delete_credential() {
        Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
        Err(_) => Err(Error::new(UNAVAILABLE)),
    }
}
pub fn save(session: &Session) -> Result<()> {
    require(
        session.0["refresh_token"]
            .as_str()
            .is_some_and(|s| !s.is_empty() && s.len() <= 32768)
            && !session.member().is_empty(),
        "La sesión guardada no es válida. Conecta de nuevo mediante QR.",
    )?;
    let mut value = json!({"version":1,"refresh_token":session.0["refresh_token"],"member_no":session.0["member_no"]});
    let serialized = serde_json::to_string(&value);
    crate::auth::wipe(&mut value);
    let secret = Zeroizing::new(serialized?);
    #[cfg(test)]
    {
        if TEST_FAIL.load(std::sync::atomic::Ordering::Relaxed) {
            return Err(Error::new(UNAVAILABLE));
        }
        *STORE.lock().unwrap() = Some(secret);
        return Ok(());
    }
    #[cfg(not(test))]
    entry()?
        .set_password(&secret)
        .map_err(|_| Error::new(UNAVAILABLE))
}
pub fn load() -> Result<Option<Session>> {
    let Some(secret) = read()? else {
        return Ok(None);
    };
    let mut value: Value = serde_json::from_str(&secret).map_err(|_| {
        Error::new("La sesión guardada no es válida. Conecta de nuevo mediante QR.")
    })?;
    let valid = value["version"] == 1
        && value["refresh_token"]
            .as_str()
            .is_some_and(|s| !s.is_empty() && s.len() <= 32768)
        && value["member_no"].as_str().is_some_and(|s| {
            !s.is_empty() && s.len() <= 32 && s.bytes().all(|b| b.is_ascii_digit())
        });
    if !valid {
        crate::auth::wipe(&mut value);
    }
    require(
        valid,
        "La sesión guardada no es válida. Conecta de nuevo mediante QR.",
    )?;
    Ok(Some(Session(value)))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn only_refresh_and_identity_are_persisted() {
        delete().unwrap();
        let s = Session(
            json!({"access_token":"ACCESS-PRIVATE","refresh_token":"RT-PRIVATE","member_no":"42","transaction_id":"not-needed"}),
        );
        save(&s).unwrap();
        let loaded = load().unwrap().unwrap();
        assert_eq!(loaded.0["refresh_token"], "RT-PRIVATE");
        assert!(loaded.0.get("access_token").is_none());
        assert!(loaded.0.get("transaction_id").is_none());
        delete().unwrap();
        assert!(load().unwrap().is_none());
    }
}
