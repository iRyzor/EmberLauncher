pub mod auth;
pub mod download;
pub mod engine;
pub mod fs;
pub mod install;
pub mod launch;
pub mod manifest;
pub mod news;
pub mod processes;
pub mod region;
pub mod remember;
pub mod runtime;
pub mod transaction;
pub mod updates;
use serde_json::Value;
pub type Result<T> = std::result::Result<T, Error>;
#[derive(Clone)]
pub struct Error {
    pub message: String,
    pub http: Option<i32>,
    pub expired: bool,
    pub cancelled: bool,
}
impl Error {
    pub fn new(s: &str) -> Self {
        Self {
            message: s.into(),
            http: None,
            expired: false,
            cancelled: false,
        }
    }
    pub fn expired() -> Self {
        Self { expired:true, ..Self::new("La sesión necesita un nuevo acceso por QR. Si hay una partida abierta, ciérrala antes de conectar de nuevo.") }
    }
    pub fn cancelled() -> Self {
        Self {
            cancelled: true,
            ..Self::new("Descarga cancelada.")
        }
    }
}
impl std::fmt::Debug for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}
impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}
impl std::error::Error for Error {}
impl From<std::io::Error> for Error {
    fn from(_: std::io::Error) -> Self {
        Self::new("No se pudo completar la operación con los archivos.")
    }
}
impl From<serde_json::Error> for Error {
    fn from(_: serde_json::Error) -> Self {
        Self::new("Los datos no tienen el formato esperado.")
    }
}
impl From<rusqlite::Error> for Error {
    fn from(_: rusqlite::Error) -> Self {
        Self::new("No se pudo verificar el inventario del juego.")
    }
}
pub fn require(condition: bool, s: &str) -> Result<()> {
    if condition {
        Ok(())
    } else {
        Err(Error::new(s))
    }
}
pub fn now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}
pub fn text(v: &Value, k: &str) -> String {
    v[k].as_str().unwrap_or("").into()
}
pub const GAME: &str = "STOVE_CHAOSZERO";
pub const SDK: &str = "ab7a50b44ca863dea1b4f0e9186175a53dddc5f48fac2b701031a8be0bd1a121";

#[cfg(test)]
mod tests;
