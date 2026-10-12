//! Pure matching rules. No process enumeration, environment access or signals.
const EXCLUDED: [&[u8]; 5] = [
    b"wineserver",
    b"winedevice",
    b"explorer.exe",
    b"services.exe",
    b"crashpad_handler.exe",
];
fn excluded(name: &[u8]) -> bool {
    EXCLUDED.iter().any(|entry| {
        name.windows(entry.len())
            .any(|part| part.eq_ignore_ascii_case(entry))
    })
}
fn path_match(game: &[u8], windows: &[u8], candidate: &[u8]) -> bool {
    if candidate
        .split(|b| *b == b'/')
        .any(|part| part == b".." || part == b".")
    {
        return false;
    }
    let name = candidate.rsplit(|b| *b == b'/').next().unwrap_or(candidate);
    !excluded(name)
        && candidate.ends_with(b".exe")
        && ((!game.is_empty() && candidate.starts_with(game))
            || (!windows.is_empty() && candidate.starts_with(windows)))
}
fn mapping_match(game: &[u8], maps: &[u8]) -> bool {
    if game.is_empty() {
        return false;
    }
    for line in maps.split(|b| *b == b'\n') {
        let mut rest = line;
        let mut executable = false;
        let mut valid = true;
        for field in 0..5 {
            rest = rest.trim_ascii_start();
            let Some(end) = rest.iter().position(|b| b.is_ascii_whitespace()) else {
                valid = false;
                break;
            };
            if field == 1 {
                executable = rest[..end].contains(&b'x');
            }
            rest = &rest[end..];
        }
        if !valid || !executable {
            continue;
        }
        let mut path = rest.trim_ascii_start();
        const DELETED: &[u8] = b" (deleted)";
        if path.len() >= DELETED.len()
            && path[path.len() - DELETED.len()..].eq_ignore_ascii_case(DELETED)
        {
            path = &path[..path.len() - DELETED.len()];
        }
        if path.starts_with(game) && path.len() >= 4 {
            let suffix = &path[path.len() - 4..];
            if suffix.eq_ignore_ascii_case(b".exe") || suffix.eq_ignore_ascii_case(b".dll") {
                return true;
            }
        }
    }
    false
}
#[no_mangle]
pub extern "C" fn ember_process_abi() -> u32 {
    2
}
/// # Safety
/// All ranges are readable/non-overlapping for this call; null requires zero size.
/// Paths for kind 0 are normalized/casefolded by the host. Kind 1 uses raw proc bytes.
#[no_mangle]
pub unsafe extern "C" fn ember_process_check(
    kind: u32,
    game: *const u8,
    game_size: usize,
    windows: *const u8,
    windows_size: usize,
    value: *const u8,
    value_size: usize,
) -> i32 {
    unsafe fn bytes<'a>(p: *const u8, n: usize) -> Option<&'a [u8]> {
        if n > isize::MAX as usize || (p.is_null() && n != 0) {
            return None;
        }
        Some(if n == 0 {
            &[]
        } else {
            std::slice::from_raw_parts(p, n)
        })
    }
    let (Some(game), Some(windows), Some(value)) = (
        bytes(game, game_size),
        bytes(windows, windows_size),
        bytes(value, value_size),
    ) else {
        return -1;
    };
    match kind {
        0 => i32::from(path_match(game, windows, value)),
        1 => i32::from(mapping_match(game, value)),
        2 => i32::from(excluded(value)),
        _ => -1,
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn exact_directory_and_executable_are_required() {
        assert!(path_match(b"/game/", b"c:/game/", b"c:/game/client.exe"));
        assert!(!path_match(
            b"/game/",
            b"c:/game/",
            b"c:/game-other/client.exe"
        ));
        assert!(!path_match(b"/game/", b"", b"/other/client.exe"));
        assert!(!path_match(b"/game/", b"", b"/game/client.dll"));
        assert!(!path_match(b"/game/", b"", b"/game/crashpad_handler.exe"));
    }
    #[test]
    fn mappings_handle_spaces_deleted_files_and_permissions() {
        assert!(mapping_match(
            b"/game dir/",
            b"1000-2000 r-xp 0000 00:00 1 /game dir/bin/client.EXE (deleted)"
        ));
        assert!(!mapping_match(
            b"/game/",
            b"1000-2000 r--p 0000 00:00 1 /game/client.exe"
        ));
        assert!(!mapping_match(
            b"/game/",
            b"1000-2000 r-xp 0000 00:00 1 /other/game/client.exe"
        ));
        assert!(!mapping_match(
            b"/game/",
            b"1000-2000 r-xp 0000 00:00 1 /game-other/client.exe"
        ));
    }
    #[test]
    fn wine_infrastructure_is_excluded() {
        for name in EXCLUDED {
            assert!(excluded(name));
        }
        assert!(excluded(b"WINESERVER"));
        assert!(!excluded(b"ssr-stove-shield.exe"));
    }
}

pub fn lifecycle(raw: &[u8]) -> Option<Vec<u8>> {
    use serde_json::{json, Value};
    let input: Value = serde_json::from_slice(raw).ok()?;
    fn identities(value: &Value) -> Option<serde_json::Map<String, Value>> {
        let map = value.as_object()?;
        for (pid, start) in map {
            if pid.parse::<u32>().ok()? == 0 || !start.is_number() || start.as_f64()? < 0.0 {
                return None;
            }
        }
        Some(map.clone())
    }
    fn same(a: &Value, b: &Value) -> bool {
        if let (Some(a), Some(b)) = (a.as_u64(), b.as_u64()) {
            a == b
        } else {
            a.as_f64() == b.as_f64()
        }
    }
    let baseline = identities(&input["baseline"])?;
    let current = identities(&input["current"])?;
    let alive = identities(&input["alive"])?;
    let mut owned = identities(&input["owned"])?;
    let mut seen = input["seen"].as_bool()?;
    let now = input["now"].as_f64()?;
    let mut empty = if input["empty_since"].is_null() {
        None
    } else {
        Some(input["empty_since"].as_f64()?)
    };
    for (pid, start) in current {
        if !baseline.get(&pid).is_some_and(|old| same(old, &start)) {
            owned.insert(pid, start);
            seen = true;
        }
    }
    owned.retain(|pid, start| alive.get(pid).is_some_and(|actual| same(start, actual)));
    let running = !owned.is_empty();
    if running {
        empty = None;
    } else if seen && empty.is_none() {
        empty = Some(now);
    }
    let finished = seen && !running && empty.is_some_and(|since| now - since >= 2.0);
    serde_json::to_vec(&json!({"owned":owned,"seen":seen,"empty_since":empty,"running":running,"finished":finished})).ok()
}
/// # Safety
/// Input is readable, output writable and non-overlapping for the supplied ranges.
#[no_mangle]
pub unsafe extern "C" fn ember_process_lifecycle(
    data: *const u8,
    size: usize,
    target: *mut u8,
    capacity: usize,
) -> i32 {
    if data.is_null() || target.is_null() || size > 1024 * 1024 || capacity < 1024 * 1024 + 4096 {
        return -1;
    }
    let result = std::panic::catch_unwind(|| lifecycle(std::slice::from_raw_parts(data, size)));
    match result {
        Ok(Some(output)) if output.len() <= capacity => {
            std::ptr::copy_nonoverlapping(output.as_ptr(), target, output.len());
            output.len() as i32
        }
        _ => -1,
    }
}
