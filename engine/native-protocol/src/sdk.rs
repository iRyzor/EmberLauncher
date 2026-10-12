//! SDK handshake state. No tokens or keys are retained by this state machine.
use serde_json::Value;
use zeroize::Zeroize;
const FIELDS: &[&str] = &[
    "game_id",
    "access_token",
    "refresh_token",
    "expire_in",
    "expire_time",
    "guid",
    "cafe_key",
    "account_type",
    "market_game_id",
    "heartbeat_interval",
    "playtime_interval",
    "playtime_retry_interval",
    "ref_session_id",
    "uuid",
    "service_protocol",
    "ref_source_type",
    "studio_game_yn",
    "dlc_list",
    "game_type",
    "env",
    "gds_isdefault_yn",
    "gds_nation",
    "gds_regulation",
    "gds_timezone",
    "gds_utcoffset",
    "language",
    "member_no",
    "member_nickname",
    "person_verify_yn",
    "email_verify_yn",
    "country_cd",
    "provider_cd",
    "streaming_yn",
    "exit_when_launcher_yn",
];
fn wipe(value: &mut Value) {
    match value {
        Value::String(s) => s.zeroize(),
        Value::Array(a) => a.iter_mut().for_each(wipe),
        Value::Object(o) => o.values_mut().for_each(wipe),
        _ => (),
    }
}
fn info(value: &Value) -> i32 {
    let Some(object) = value.as_object() else {
        return -1;
    };
    if object.keys().any(|key| !FIELDS.contains(&key.as_str())) {
        return -1;
    }
    if object.values().any(|value| {
        !value
            .as_str()
            .is_some_and(|s| s.chars().count() <= 32768 && !s.contains('\0'))
    }) {
        return -2;
    }
    if ["access_token", "game_id"].iter().any(|key| {
        object
            .get(*key)
            .and_then(Value::as_str)
            .unwrap_or("")
            .is_empty()
    }) {
        return -3;
    }
    0
}
struct State {
    phase: u32,
    heartbeats: u64,
}
#[no_mangle]
pub extern "C" fn ember_sdk_abi() -> u32 {
    2
}
/// # Safety
/// Input range must be readable. JSON is bounded and never retained.
#[no_mangle]
pub unsafe extern "C" fn ember_sdk_info(data: *const u8, size: usize) -> i32 {
    if data.is_null() || size > 4 * 1024 * 1024 {
        return -1;
    }
    let Ok(mut value) = serde_json::from_slice(std::slice::from_raw_parts(data, size)) else {
        return -1;
    };
    let result = info(&value);
    wipe(&mut value);
    result
}
#[no_mangle]
pub extern "C" fn ember_sdk_new() -> *mut std::ffi::c_void {
    Box::into_raw(Box::new(State {
        phase: 0,
        heartbeats: 0,
    }))
    .cast()
}
/// # Safety
/// Handle must be live, exclusively owned and freed exactly once.
#[no_mangle]
pub unsafe extern "C" fn ember_sdk_free(handle: *mut std::ffi::c_void) {
    if !handle.is_null() {
        drop(Box::from_raw(handle.cast::<State>()));
    }
}
/// # Safety
/// Live handle and readable input, serialized access; input is never retained.
/// action 0 validates a packet, 1 commits constructed responses, 2 reports phase.
#[no_mangle]
pub unsafe extern "C" fn ember_sdk_step(
    handle: *mut std::ffi::c_void,
    action: u32,
    id: u32,
    data: *const u8,
    size: usize,
) -> i64 {
    if handle.is_null() {
        return -9;
    }
    let state = &mut *handle.cast::<State>();
    if action == 2 {
        return state.phase as i64;
    }
    if action == 3 {
        return state.heartbeats as i64;
    }
    if action == 1 {
        if state.phase != 0 {
            return -4;
        }
        state.phase = 1;
        return 0;
    }
    if action != 0 || data.is_null() || size > 65531 {
        return -9;
    }
    let Ok(mut value) = serde_json::from_slice::<Value>(std::slice::from_raw_parts(data, size))
    else {
        return -1;
    };
    let result = if !value.is_object() || value["code"].as_i64() != Some(0) {
        -2
    } else if id == 1000 {
        if state.phase != 0 {
            -4
        } else if !value["value"].is_object() {
            -5
        } else {
            let pid = value["value"]["processId"].as_str().unwrap_or("");
            if pid.is_empty()
                || pid.len() > 10
                || !pid.bytes().all(|b| b.is_ascii_digit())
                || !pid.parse::<u32>().is_ok_and(|p| p > 0)
            {
                -6
            } else {
                1
            }
        }
    } else if id == 1001 {
        if state.phase != 1 || value["value"].as_str() != Some("") {
            -7
        } else if let Some(count) = state
            .heartbeats
            .checked_add(1)
            .filter(|n| *n <= i64::MAX as u64)
        {
            state.heartbeats = count;
            2
        } else {
            -9
        }
    } else {
        -8
    };
    wipe(&mut value);
    result
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn only_known_bounded_information_is_accepted() {
        assert_eq!(
            info(&serde_json::json!({"game_id":"game","access_token":"test"})),
            0
        );
        assert_eq!(
            info(&serde_json::json!({"game_id":"game","access_token":"test","unknown":"x"})),
            -1
        );
        assert_eq!(
            info(&serde_json::json!({"game_id":"game","access_token":true})),
            -2
        );
        assert_eq!(info(&serde_json::json!({"game_id":"game"})), -3);
    }
    #[test]
    fn heartbeat_requires_committed_information() {
        unsafe {
            let handle = ember_sdk_new();
            let raw = br#"{"code":0,"value":""}"#;
            assert_eq!(ember_sdk_step(handle, 0, 1001, raw.as_ptr(), raw.len()), -7);
            assert_eq!(ember_sdk_step(handle, 1, 0, std::ptr::null(), 0), 0);
            assert_eq!(ember_sdk_step(handle, 0, 1001, raw.as_ptr(), raw.len()), 2);
            assert_eq!(ember_sdk_step(handle, 3, 0, std::ptr::null(), 0), 1);
            assert_eq!(ember_sdk_step(handle, 1, 0, std::ptr::null(), 0), -4);
            ember_sdk_free(handle);
        }
    }
}

/// # Safety
/// Input ranges are readable, output writable and non-overlapping for this call.
/// Both response frames are built before any output write. No input is retained.
#[no_mangle]
pub unsafe extern "C" fn ember_sdk_responses(
    packet: *const u8,
    packet_size: usize,
    key: *const u8,
    key_size: usize,
    information: *const u8,
    information_size: usize,
    target: *mut u8,
    capacity: usize,
) -> i32 {
    use base64::{engine::general_purpose::STANDARD, Engine};
    use zeroize::Zeroizing;
    let action = || -> Result<i32, ()> {
        if packet.is_null()
            || key.is_null()
            || information.is_null()
            || target.is_null()
            || packet_size > 65531
            || key_size != 32
            || information_size > 1048576
            || capacity < 2 * 65535
        {
            return Err(());
        }
        let packet = std::slice::from_raw_parts(packet, packet_size);
        let key = std::slice::from_raw_parts(key, key_size);
        let raw_info = std::slice::from_raw_parts(information, information_size);
        if key.iter().any(|b| !(32..=126).contains(b)) {
            return Err(());
        }
        let mut parsed_info: Value = serde_json::from_slice(raw_info).map_err(|_| ())?;
        let valid = info(&parsed_info) == 0;
        wipe(&mut parsed_info);
        if !valid {
            return Err(());
        }
        let mut envelope: Value = serde_json::from_slice(packet).map_err(|_| ())?;
        let pid = envelope["value"]["processId"].as_str().unwrap_or("");
        let valid_packet = envelope.is_object()
            && envelope["code"].as_i64() == Some(0)
            && envelope["value"].is_object()
            && !pid.is_empty()
            && pid.len() <= 10
            && pid.bytes().all(|b| b.is_ascii_digit())
            && pid.parse::<u32>().is_ok_and(|p| p > 0);
        let der_result = envelope["value"]["publicKey"]
            .as_str()
            .filter(|s| s.len() <= 16384)
            .and_then(|s| STANDARD.decode(s).ok());
        wipe(&mut envelope);
        if !valid_packet {
            return Err(());
        }
        let der = der_result.ok_or(())?;
        let key_text = std::str::from_utf8(key).map_err(|_| ())?;
        // serde escapes quotes/backslashes exactly as compact JSON does.
        let encoded_key = Zeroizing::new(serde_json::to_string(key_text).map_err(|_| ())?);
        let key_plain = Zeroizing::new(format!("{{\"aesKey\":{}}}", &*encoded_key));
        let mut rsa = vec![0u8; 512];
        let rsa_size = crate::crypto::ember_rsa_encrypt(
            der.as_ptr(),
            der.len(),
            key_plain.as_ptr(),
            key_plain.len(),
            rsa.as_mut_ptr(),
            rsa.len(),
        );
        if rsa_size < 0 {
            return Err(());
        }
        rsa.truncate(rsa_size as usize);
        let mut aes = Zeroizing::new(vec![0u8; (raw_info.len() / 16 + 1) * 16]);
        let aes_size = crate::crypto::ember_aes_encrypt(
            key.as_ptr(),
            key.len(),
            raw_info.as_ptr(),
            raw_info.len(),
            aes.as_mut_ptr(),
            aes.len(),
        );
        if aes_size < 0 {
            return Err(());
        }
        const HEX: &[u8] = b"0123456789ABCDEF";
        let mut hex = String::with_capacity(aes_size as usize * 2);
        for byte in &aes[..aes_size as usize] {
            hex.push(HEX[(byte >> 4) as usize] as char);
            hex.push(HEX[(byte & 15) as usize] as char);
        }
        let payloads = [
            format!(
                "{{\"code\":0,\"message\":\"Success\",\"value\":\"{}\"}}",
                STANDARD.encode(&rsa)
            ),
            format!(
                "{{\"code\":0,\"message\":\"Success\",\"value\":\"{}\"}}",
                hex
            ),
        ];
        let mut output = Vec::new();
        for (id, payload) in [2000u32, 2001].into_iter().zip(payloads.iter()) {
            let mut frame = vec![0u8; 65535];
            let size = crate::codec::ember_packet_encode(
                id,
                payload.as_ptr(),
                payload.len(),
                frame.as_mut_ptr(),
                frame.len(),
            );
            if size < 0 {
                return Err(());
            }
            output.extend_from_slice(&frame[..size as usize]);
        }
        std::ptr::copy_nonoverlapping(output.as_ptr(), target, output.len());
        Ok(output.len() as i32)
    };
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(action))
        .unwrap_or(Err(()))
        .unwrap_or(-1)
}
