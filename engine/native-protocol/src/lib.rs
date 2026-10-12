//! PCSDK framing and bounded stream codec; no account/session state is retained.
pub mod auth;
pub mod codec;
pub mod crypto;
pub mod process;
pub mod qr;
pub mod sdk;
pub mod session;
pub fn header(bytes: &[u8]) -> Result<Option<(u16, u16)>, ()> {
    if bytes.len() < 4 {
        return Ok(None);
    }
    let length = u16::from_le_bytes([bytes[0], bytes[1]]);
    if length < 4 {
        return Err(());
    }
    Ok(Some((length, u16::from_le_bytes([bytes[2], bytes[3]]))))
}
/// -1 malformed, 0 incomplete header, otherwise total frame length.
/// Caller supplies a readable byte range; no pointer is retained.
#[no_mangle]
pub unsafe extern "C" fn ember_frame_header(data: *const u8, size: usize) -> i32 {
    if data.is_null() {
        return if size == 0 { 0 } else { -1 };
    }
    let bytes = std::slice::from_raw_parts(data, size.min(4));
    match header(bytes) {
        Ok(Some((length, _))) => i32::from(length),
        Ok(None) => 0,
        Err(()) => -1,
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn validates_every_length() {
        for length in 0..=u16::MAX {
            let [a, b] = length.to_le_bytes();
            assert_eq!(header(&[a, b, 0xe8, 3]).is_ok(), length >= 4);
        }
    }
    #[test]
    fn fragmented_header() {
        for length in 0..4 {
            assert_eq!(header(&[4, 0, 0xe8, 3][..length]), Ok(None));
        }
        assert_eq!(header(&[4, 0, 0xe8, 3]), Ok(Some((4, 1000))));
    }
}
