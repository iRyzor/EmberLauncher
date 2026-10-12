//! Renewal policy. Status HTTP and service policy codes stay separate.
#[no_mangle]
pub extern "C" fn ember_session_abi() -> u32 {
    1
}
/// 0 propagate original error, 1 renew, 2 require QR; -1 invalid stage.
/// Stages: initial operation, refresh operation, retried operation.
#[no_mangle]
pub extern "C" fn ember_session_decision(stage: u32, status: i32, terminal: u32) -> i32 {
    match stage {
        0 => {
            if status == 401 {
                1
            } else {
                0
            }
        }
        1 => {
            if terminal != 0 || status == 401 || status == 403 {
                2
            } else {
                0
            }
        }
        2 => {
            if status == 401 {
                2
            } else {
                0
            }
        }
        _ => -1,
    }
}
/// # Safety
/// Input/output ranges must be valid and non-overlapping for this call.
/// Returns identity byte length, -2 account change, -1 invalid buffer/input.
#[no_mangle]
pub unsafe extern "C" fn ember_session_identity(
    previous: *const u8,
    previous_size: usize,
    new: *const u8,
    new_size: usize,
    target: *mut u8,
    capacity: usize,
) -> i32 {
    if previous_size > 1048576
        || new_size > 1048576
        || target.is_null()
        || (previous.is_null() && previous_size != 0)
        || (new.is_null() && new_size != 0)
    {
        return -1;
    }
    let previous = if previous_size == 0 {
        &[]
    } else {
        std::slice::from_raw_parts(previous, previous_size)
    };
    let new = if new_size == 0 {
        &[]
    } else {
        std::slice::from_raw_parts(new, new_size)
    };
    if std::str::from_utf8(previous).is_err() || std::str::from_utf8(new).is_err() {
        return -1;
    }
    if !previous.is_empty() && !new.is_empty() && previous != new {
        return -2;
    }
    let selected = if new.is_empty() { previous } else { new };
    if capacity < selected.len() {
        return -1;
    }
    std::ptr::copy_nonoverlapping(selected.as_ptr(), target, selected.len());
    selected.len() as i32
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn only_initial_401_renews_and_second_401_requires_qr() {
        for status in 0..=599 {
            assert_eq!(
                ember_session_decision(0, status, 0),
                if status == 401 { 1 } else { 0 }
            );
            assert_eq!(
                ember_session_decision(1, status, 0),
                if status == 401 || status == 403 { 2 } else { 0 }
            );
            assert_eq!(
                ember_session_decision(2, status, 0),
                if status == 401 { 2 } else { 0 }
            );
        }
        assert_eq!(ember_session_decision(1, 0, 1), 2);
        assert_eq!(ember_session_decision(3, 401, 0), -1);
    }
    #[test]
    fn identity_change_never_writes_result() {
        let mut target = [99u8; 8];
        unsafe {
            assert_eq!(
                ember_session_identity(
                    b"123".as_ptr(),
                    3,
                    b"999".as_ptr(),
                    3,
                    target.as_mut_ptr(),
                    8
                ),
                -2
            );
            assert_eq!(target, [99u8; 8]);
            assert_eq!(
                ember_session_identity(
                    b"123".as_ptr(),
                    3,
                    std::ptr::null(),
                    0,
                    target.as_mut_ptr(),
                    8
                ),
                3
            );
            assert_eq!(&target[..3], b"123");
        }
    }
}
