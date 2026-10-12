//! Bounded PCSDK stream codec. Callbacks borrow payloads only during delivery.
use std::ffi::c_void;
use std::sync::atomic::{compiler_fence, Ordering};

const MAX_FRAME: usize = u16::MAX as usize;
pub type Sink = unsafe extern "C" fn(*mut c_void, u16, *const u8, usize) -> i32;

pub struct Decoder {
    bytes: [u8; MAX_FRAME],
    used: usize,
}
impl Decoder {
    pub fn new() -> Self {
        Self {
            bytes: [0; MAX_FRAME],
            used: 0,
        }
    }
    fn clear(&mut self) {
        for byte in &mut self.bytes[..self.used] {
            // Do not let optimization discard cleanup of buffered payload bytes.
            unsafe {
                std::ptr::write_volatile(byte, 0);
            }
        }
        compiler_fence(Ordering::SeqCst);
        self.used = 0;
    }
    /// -1 malformed header, -2 delivery failure. No input pointer is retained.
    pub unsafe fn feed(&mut self, mut input: &[u8], sink: Sink, context: *mut c_void) -> i32 {
        while !input.is_empty() {
            let target = if self.used < 4 {
                4
            } else {
                u16::from_le_bytes([self.bytes[0], self.bytes[1]]) as usize
            };
            let take = (target - self.used).min(input.len());
            self.bytes[self.used..self.used + take].copy_from_slice(&input[..take]);
            self.used += take;
            input = &input[take..];
            if self.used >= 4 {
                let length = u16::from_le_bytes([self.bytes[0], self.bytes[1]]) as usize;
                if length < 4 {
                    self.clear();
                    return -1;
                }
                if self.used == length {
                    let id = u16::from_le_bytes([self.bytes[2], self.bytes[3]]);
                    let status = sink(context, id, self.bytes[4..].as_ptr(), length - 4);
                    self.clear();
                    if status != 0 {
                        return -2;
                    }
                }
            }
        }
        0
    }
    pub fn finish(&self) -> i32 {
        if self.used == 0 {
            0
        } else {
            -1
        }
    }
}
impl Drop for Decoder {
    fn drop(&mut self) {
        self.clear();
    }
}

#[no_mangle]
pub extern "C" fn ember_codec_abi() -> u32 {
    1
}
#[no_mangle]
pub extern "C" fn ember_decoder_new() -> *mut Decoder {
    Box::into_raw(Box::new(Decoder::new()))
}
/// Requires a live decoder owned by this caller; no concurrent calls are allowed.
#[no_mangle]
pub unsafe extern "C" fn ember_decoder_free(decoder: *mut Decoder) {
    if !decoder.is_null() {
        drop(Box::from_raw(decoder));
    }
}
/// Input is a readable byte range; sink must copy payload before returning.
#[no_mangle]
pub unsafe extern "C" fn ember_decoder_feed(
    decoder: *mut Decoder,
    data: *const u8,
    size: usize,
    sink: Option<Sink>,
    context: *mut c_void,
) -> i32 {
    let (Some(decoder), Some(sink)) = (decoder.as_mut(), sink) else {
        return -3;
    };
    if data.is_null() && size != 0 {
        return -3;
    }
    let input = if size == 0 {
        &[]
    } else {
        std::slice::from_raw_parts(data, size)
    };
    decoder.feed(input, sink, context)
}
#[no_mangle]
pub unsafe extern "C" fn ember_decoder_finish(decoder: *const Decoder) -> i32 {
    decoder.as_ref().map_or(-3, Decoder::finish)
}
/// Caller supplies non-overlapping readable payload and writable output ranges.
#[no_mangle]
pub unsafe extern "C" fn ember_packet_encode(
    id: u32,
    payload: *const u8,
    size: usize,
    output: *mut u8,
    capacity: usize,
) -> i32 {
    if id > u16::MAX as u32 {
        return -2;
    }
    let Some(length) = size.checked_add(4).filter(|n| *n <= MAX_FRAME) else {
        return -1;
    };
    if output.is_null() || capacity < length || (payload.is_null() && size != 0) {
        return -3;
    }
    let target = std::slice::from_raw_parts_mut(output, length);
    target[..2].copy_from_slice(&(length as u16).to_le_bytes());
    target[2..4].copy_from_slice(&(id as u16).to_le_bytes());
    if size != 0 {
        target[4..].copy_from_slice(std::slice::from_raw_parts(payload, size));
    }
    length as i32
}

#[cfg(test)]
mod tests {
    use super::*;
    unsafe extern "C" fn collect(
        context: *mut c_void,
        id: u16,
        data: *const u8,
        size: usize,
    ) -> i32 {
        (*(context as *mut Vec<(u16, Vec<u8>)>))
            .push((id, std::slice::from_raw_parts(data, size).to_vec()));
        0
    }
    #[test]
    fn fragmented_and_coalesced_empty_frames() {
        let mut decoder = Decoder::new();
        let mut output: Vec<(u16, Vec<u8>)> = vec![];
        let context = &mut output as *mut _ as *mut c_void;
        unsafe {
            assert_eq!(decoder.feed(&[4], collect, context), 0);
            assert_eq!(decoder.finish(), -1);
            assert_eq!(
                decoder.feed(&[0, 9, 0, 5, 0, 10, 0, 42], collect, context),
                0
            );
        }
        assert_eq!(output, vec![(9, vec![]), (10, vec![42])]);
        assert_eq!(decoder.finish(), 0);
        assert!(decoder.bytes[..5].iter().all(|b| *b == 0));
    }
    #[test]
    fn malformed_frame_clears_pending_data() {
        let mut decoder = Decoder::new();
        let mut output: Vec<(u16, Vec<u8>)> = vec![];
        unsafe {
            assert_eq!(
                decoder.feed(&[3, 0, 1, 0], collect, &mut output as *mut _ as *mut c_void),
                -1
            );
        }
        assert_eq!(decoder.used, 0);
        assert!(decoder.bytes[..4].iter().all(|b| *b == 0));
    }
    #[test]
    fn encode_checks_capacity_and_bounds_before_writing() {
        let mut output = [99u8; 4];
        unsafe {
            assert_eq!(
                ember_packet_encode(65536, std::ptr::null(), 0, output.as_mut_ptr(), 4),
                -2
            );
            assert_eq!(
                ember_packet_encode(1, std::ptr::null(), 65532, output.as_mut_ptr(), 4),
                -1
            );
            assert_eq!(
                ember_packet_encode(1, std::ptr::null(), 0, output.as_mut_ptr(), 3),
                -3
            );
            assert_eq!(output, [99; 4]);
            assert_eq!(
                ember_packet_encode(1001, std::ptr::null(), 0, output.as_mut_ptr(), 4),
                4
            );
        }
        assert_eq!(output, [4, 0, 233, 3]);
    }
}
