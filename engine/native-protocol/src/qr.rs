//! One-use QR approval owned by the private launcher client.
use serde_json::Value;
use zeroize::Zeroizing;

pub struct Gate {
    session: Zeroizing<Vec<u8>>,
    expires: f64,
    approved: bool,
    consumed: bool,
}
impl Gate {
    fn reset(&mut self, session: &[u8], expires: f64) -> i32 {
        self.approved = false;
        self.consumed = false;
        self.session = Zeroizing::new(Vec::new());
        self.expires = 0.0;
        let Ok(text) = std::str::from_utf8(session) else {
            return -1;
        };
        if !(1..=4096).contains(&text.chars().count()) || !expires.is_finite() || expires <= 0.0 {
            return -1;
        }
        self.session = Zeroizing::new(session.to_vec());
        self.expires = expires;
        self.approved = false;
        self.consumed = false;
        0
    }
    fn check(&self, session: &[u8], now: f64) -> i32 {
        if self.session.is_empty() || self.session.as_slice() != session || !now.is_finite() {
            return -2;
        }
        if now >= self.expires {
            return 4;
        }
        0
    }
    fn observe(&mut self, session: &[u8], now: f64, value: &Value) -> i32 {
        let checked = self.check(session, now);
        if checked != 0 {
            self.approved = false;
            return checked;
        }
        if self.consumed {
            return -3;
        }
        self.approved = false;
        if value["session"].as_str().map(str::as_bytes) != Some(session) {
            return -2;
        }
        match value["status"].as_str() {
            Some("init") => 0,
            Some("ongoing") => 1,
            Some("success") => {
                self.approved = true;
                2
            }
            Some("close") => {
                self.approved = false;
                self.consumed = true;
                3
            }
            _ => -1,
        }
    }
    fn consume(&mut self, session: &[u8], now: f64) -> i32 {
        if self.check(session, now) != 0 || !self.approved || self.consumed {
            return -3;
        }
        self.approved = false;
        self.consumed = true;
        0
    }
}
#[no_mangle]
pub extern "C" fn ember_qr_abi() -> u32 {
    1
}
#[no_mangle]
pub extern "C" fn ember_qr_new() -> *mut Gate {
    Box::into_raw(Box::new(Gate {
        session: Zeroizing::new(Vec::new()),
        expires: 0.0,
        approved: false,
        consumed: false,
    }))
}
/// # Safety
/// Gate is owned, live, and has no concurrent users. Null is accepted.
#[no_mangle]
pub unsafe extern "C" fn ember_qr_free(gate: *mut Gate) {
    if !gate.is_null() {
        drop(Box::from_raw(gate));
    }
}
/// # Safety
/// Gate is exclusively owned; data points to a readable UTF-8 session.
#[no_mangle]
pub unsafe extern "C" fn ember_qr_reset(
    gate: *mut Gate,
    data: *const u8,
    size: usize,
    expires: f64,
) -> i32 {
    if gate.is_null() || data.is_null() || size > 16384 {
        return -1;
    }
    (*gate).reset(std::slice::from_raw_parts(data, size), expires)
}
/// # Safety
/// Gate is exclusively owned; session/response point to readable byte ranges.
/// Action 0 checks expiry, 1 observes status JSON, 2 consumes approval once.
#[no_mangle]
pub unsafe extern "C" fn ember_qr_step(
    gate: *mut Gate,
    action: u32,
    data: *const u8,
    size: usize,
    now: f64,
    response: *const u8,
    response_size: usize,
) -> i32 {
    if gate.is_null() || data.is_null() || size > 16384 || response_size > 1048576 {
        return -1;
    }
    let session = std::slice::from_raw_parts(data, size);
    match action {
        0 => (*gate).check(session, now),
        2 => (*gate).consume(session, now),
        1 => {
            if response.is_null() {
                return -1;
            }
            let value: Value =
                match serde_json::from_slice(std::slice::from_raw_parts(response, response_size)) {
                    Ok(v) => v,
                    Err(_) => return -1,
                };
            (*gate).observe(session, now, &value)
        }
        _ => -1,
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    fn gate() -> Gate {
        let mut g = Gate {
            session: Zeroizing::new(Vec::new()),
            expires: 0.0,
            approved: false,
            consumed: false,
        };
        assert_eq!(g.reset(b"synthetic", 100.0), 0);
        g
    }
    #[test]
    fn approval_is_consumed_once_even_if_server_repeats_success() {
        let mut g = gate();
        let success = json!({"session":"synthetic","status":"success"});
        assert_eq!(g.consume(b"synthetic", 1.0), -3);
        assert_eq!(g.observe(b"synthetic", 1.0, &success), 2);
        assert_eq!(g.consume(b"synthetic", 1.0), 0);
        assert_eq!(g.consume(b"synthetic", 1.0), -3);
        assert_eq!(g.observe(b"synthetic", 1.0, &success), -3);
    }
    #[test]
    fn expiry_close_and_session_replacement_block_exchange() {
        let mut g = gate();
        let success = json!({"session":"synthetic","status":"success"});
        assert_eq!(g.observe(b"synthetic", 1.0, &success), 2);
        assert_eq!(g.consume(b"synthetic", 100.0), -3);
        assert_eq!(g.reset(b"new", 200.0), 0);
        assert_eq!(g.check(b"synthetic", 1.0), -2);
        assert_eq!(
            g.observe(b"new", 1.0, &json!({"session":"new","status":"close"})),
            3
        );
        assert_eq!(g.consume(b"new", 1.0), -3);
    }
    #[test]
    fn wrong_server_session_does_not_approve() {
        let mut g = gate();
        assert_eq!(
            g.observe(
                b"synthetic",
                1.0,
                &json!({"session":"other","status":"success"})
            ),
            -2
        );
        assert_eq!(g.consume(b"synthetic", 1.0), -3);
    }
}
