//! Existing PCSDK cipher suite, confined to the private SDK pipe.
//! No RSA private-key operations or new cryptographic protocol are exposed.
use aes::cipher::{block_padding::Pkcs7, BlockDecryptMut, BlockEncryptMut, KeyIvInit};
use rand::rngs::OsRng;
use rsa::{
    pkcs1::DecodeRsaPublicKey, pkcs8::DecodePublicKey, traits::PublicKeyParts, Pkcs1v15Encrypt,
    RsaPublicKey,
};
use zeroize::Zeroizing;

const MAX_INPUT: usize = 1_048_576;
const MAX_DER: usize = 12_288;

fn public_key(der: &[u8]) -> Result<RsaPublicKey, i32> {
    let key = RsaPublicKey::from_public_key_der(der)
        .or_else(|_| RsaPublicKey::from_pkcs1_der(der))
        .map_err(|_| -2)?;
    if !(1024..=4096).contains(&key.n().bits()) {
        return Err(-2);
    }
    Ok(key)
}

// All exported pointer ranges must be valid and non-overlapping for the call.
// A zero-sized input may be null. No caller pointer or key is retained.
unsafe fn input<'a>(pointer: *const u8, size: usize, limit: usize) -> Result<&'a [u8], i32> {
    if size > limit || (pointer.is_null() && size != 0) {
        return Err(-1);
    }
    Ok(if size == 0 {
        &[]
    } else {
        std::slice::from_raw_parts(pointer, size)
    })
}
unsafe fn output(pointer: *mut u8, capacity: usize, bytes: &[u8]) -> Result<i32, i32> {
    if pointer.is_null() || capacity < bytes.len() {
        return Err(-3);
    }
    std::ptr::copy_nonoverlapping(bytes.as_ptr(), pointer, bytes.len());
    Ok(bytes.len() as i32)
}
fn guarded(action: impl FnOnce() -> Result<i32, i32>) -> i32 {
    // Never unwind through ctypes, including an unavailable OS entropy source.
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(action))
        .unwrap_or(Err(-4))
        .unwrap_or_else(|code| code)
}
#[no_mangle]
pub extern "C" fn ember_crypto_abi() -> u32 {
    1
}

/// # Safety
/// DER must reference a readable range of `size` bytes for this call.
#[no_mangle]
pub unsafe extern "C" fn ember_rsa_public_size(der: *const u8, size: usize) -> i32 {
    guarded(|| Ok(public_key(input(der, size, MAX_DER)?)?.size() as i32))
}

/// # Safety
/// Key is 32 readable bytes. Input and output ranges are valid/non-overlapping.
#[no_mangle]
pub unsafe extern "C" fn ember_aes_encrypt(
    key: *const u8,
    key_size: usize,
    data: *const u8,
    size: usize,
    target: *mut u8,
    capacity: usize,
) -> i32 {
    guarded(|| {
        let key = input(key, key_size, 32)?;
        if key.len() != 32 || key.iter().any(|c| !(32..=126).contains(c)) {
            return Err(-1);
        }
        let plain = input(data, size, MAX_INPUT)?;
        let length = (size / 16 + 1) * 16;
        if target.is_null() || capacity < length {
            return Err(-3);
        }
        let mut buffer = Zeroizing::new(vec![0u8; length]);
        buffer[..size].copy_from_slice(plain);
        let cipher =
            cbc::Encryptor::<aes::Aes256>::new_from_slices(key, &key[..16]).map_err(|_| -1)?;
        let encrypted = cipher
            .encrypt_padded_mut::<Pkcs7>(&mut buffer, size)
            .map_err(|_| -2)?;
        output(target, capacity, encrypted)
    })
}

/// # Safety
/// Key is 32 readable bytes. Input and output ranges are valid/non-overlapping.
#[no_mangle]
pub unsafe extern "C" fn ember_aes_decrypt(
    key: *const u8,
    key_size: usize,
    data: *const u8,
    size: usize,
    target: *mut u8,
    capacity: usize,
) -> i32 {
    guarded(|| {
        let key = input(key, key_size, 32)?;
        if key.len() != 32 || key.iter().any(|c| !(32..=126).contains(c)) {
            return Err(-1);
        }
        let encrypted = input(data, size, MAX_INPUT + 16)?;
        if size == 0 || size % 16 != 0 {
            return Err(-2);
        }
        let mut buffer = Zeroizing::new(encrypted.to_vec());
        let cipher =
            cbc::Decryptor::<aes::Aes256>::new_from_slices(key, &key[..16]).map_err(|_| -1)?;
        let plain = cipher
            .decrypt_padded_mut::<Pkcs7>(&mut buffer)
            .map_err(|_| -2)?;
        output(target, capacity, plain)
    })
}

/// # Safety
/// DER, plaintext and output ranges are valid/non-overlapping for this call.
#[no_mangle]
pub unsafe extern "C" fn ember_rsa_encrypt(
    der: *const u8,
    der_size: usize,
    data: *const u8,
    size: usize,
    target: *mut u8,
    capacity: usize,
) -> i32 {
    guarded(|| {
        let key = public_key(input(der, der_size, MAX_DER)?)?;
        let plain = input(data, size, 501)?;
        if size > key.size() - 11 {
            return Err(-2);
        }
        if target.is_null() || capacity < key.size() {
            return Err(-3);
        }
        let encrypted = key
            .encrypt(&mut OsRng, Pkcs1v15Encrypt, plain)
            .map_err(|_| -2)?;
        output(target, capacity, &encrypted)
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    const KEY: &[u8; 32] = b"0123456789ABCDEF0123456789ABCDEF";
    #[test]
    fn accepted_sdk_vector() {
        let mut encrypted = [0u8; 32];
        let plain = b"STOVE-LINUX-CRYPTO-TEST";
        unsafe {
            assert_eq!(
                ember_aes_encrypt(
                    KEY.as_ptr(),
                    32,
                    plain.as_ptr(),
                    plain.len(),
                    encrypted.as_mut_ptr(),
                    32
                ),
                32
            );
        }
        assert_eq!(
            encrypted,
            [
                0x00, 0x63, 0xa0, 0x60, 0x23, 0x09, 0xa1, 0x3a, 0x45, 0x7a, 0xfe, 0x0c, 0xac, 0x46,
                0x05, 0x48, 0xe7, 0xde, 0x44, 0x49, 0xa7, 0x80, 0x5b, 0xcf, 0x92, 0x24, 0x3d, 0x07,
                0xf3, 0x3a, 0xc5, 0xb0
            ]
        );
        let mut restored = [0u8; 32];
        unsafe {
            assert_eq!(
                ember_aes_decrypt(
                    KEY.as_ptr(),
                    32,
                    encrypted.as_ptr(),
                    32,
                    restored.as_mut_ptr(),
                    32
                ),
                plain.len() as i32
            );
        }
        assert_eq!(&restored[..plain.len()], plain);
    }
    #[test]
    fn invalid_inputs_never_write_output() {
        let mut target = [99u8; 32];
        unsafe {
            assert_eq!(
                ember_aes_encrypt(
                    std::ptr::null(),
                    32,
                    std::ptr::null(),
                    0,
                    target.as_mut_ptr(),
                    32
                ),
                -1
            );
            assert_eq!(
                ember_aes_encrypt(
                    KEY.as_ptr(),
                    32,
                    std::ptr::null(),
                    0,
                    target.as_mut_ptr(),
                    15
                ),
                -3
            );
            assert_eq!(
                ember_aes_decrypt(
                    KEY.as_ptr(),
                    32,
                    [0u8; 16].as_ptr(),
                    16,
                    target.as_mut_ptr(),
                    32
                ),
                -2
            );
            assert_eq!(ember_rsa_public_size([0u8; 3].as_ptr(), 3), -2);
        }
        assert_eq!(target, [99u8; 32]);
    }
}
