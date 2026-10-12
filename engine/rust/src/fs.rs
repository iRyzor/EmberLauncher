//! Capability-relative filesystem operations. Reject links at every component.
use crate::{require, Error, Result};
use cap_fs_ext::{DirExt, FollowSymlinks, OpenOptionsFollowExt};
use cap_std::fs::{Dir, OpenOptions};
use fs2::FileExt;
use md5::Md5;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    fs::File,
    io::{Read, Write},
    path::{Component, Path, PathBuf},
};

pub fn physical(path: &Path) -> Result<PathBuf> {
    let path = if path.is_absolute() {
        path.to_owned()
    } else {
        std::env::current_dir()?.join(path)
    };
    let mut result = PathBuf::new();
    for part in path.components() {
        match part {
            Component::ParentDir => return Err(Error::new("La ruta no puede contener '..'.")),
            Component::CurDir => (),
            _ => result.push(part.as_os_str()),
        }
    }
    #[cfg(target_os = "macos")]
    for (alias, target) in [
        ("/var", "/private/var"),
        ("/tmp", "/private/tmp"),
        ("/etc", "/private/etc"),
    ] {
        if result.starts_with(alias)
            && std::fs::canonicalize(alias).ok().as_deref() == Some(Path::new(target))
        {
            result = Path::new(target).join(result.strip_prefix(alias).unwrap());
            break;
        }
    }
    for p in result.ancestors() {
        match std::fs::symlink_metadata(p) {
            Ok(m) => {
                require(
                    !m.file_type().is_symlink(),
                    "La ruta atraviesa un enlace simbólico.",
                )?;
                #[cfg(windows)]
                {
                    use std::os::windows::fs::MetadataExt;
                    require(
                        m.file_attributes() & 0x400 == 0,
                        "La ruta atraviesa un punto de reanálisis.",
                    )?;
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => (),
            Err(e) => return Err(e.into()),
        }
    }
    // Normalize Windows short-name aliases without requiring a missing suffix.
    #[cfg(windows)]
    {
        let mut existing = result.clone();
        let mut suffix = Vec::new();
        while !existing.exists() {
            suffix.push(
                existing
                    .file_name()
                    .ok_or_else(|| Error::new("Ruta inválida"))?
                    .to_owned(),
            );
            existing.pop();
        }
        let canonical = std::fs::canonicalize(existing)?;
        let canonical = canonical.to_string_lossy();
        result = if let Some(path) = canonical.strip_prefix(r"\\?\UNC\") {
            PathBuf::from(format!(r"\\{}", path))
        } else {
            PathBuf::from(canonical.strip_prefix(r"\\?\").unwrap_or(&canonical))
        };
        for part in suffix.into_iter().rev() {
            result.push(part);
        }
    }
    Ok(result)
}
pub fn directory(path: &Path, create: bool) -> Result<Dir> {
    let path = physical(path)?;
    let mut anchor = PathBuf::new();
    let mut pieces = Vec::new();
    for c in path.components() {
        match c {
            Component::Normal(s) => pieces.push(s.to_owned()),
            _ => anchor.push(c.as_os_str()),
        }
    }
    let mut dir = Dir::open_ambient_dir(anchor, cap_std::ambient_authority())?;
    for part in pieces {
        if create {
            match dir.create_dir(&part) {
                Ok(()) => (),
                Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => (),
                Err(e) => return Err(e.into()),
            }
        }
        dir = dir.open_dir_nofollow(part)?;
    }
    Ok(dir)
}
pub fn parent(root: &Dir, relative: &str) -> Result<(Dir, String)> {
    let parts: Vec<_> = relative.split('/').collect();
    require(
        !parts.is_empty()
            && parts.iter().all(|p| {
                !p.is_empty() && *p != "." && *p != ".." && !p.contains(['\\', ':', '\0'])
            }),
        "Ruta relativa insegura.",
    )?;
    let mut dir = root.try_clone()?;
    for p in &parts[..parts.len() - 1] {
        dir = dir.open_dir_nofollow(p)?;
    }
    Ok((dir, parts.last().unwrap().to_string()))
}
pub fn open(root: &Dir, relative: &str, write: bool, create_new: bool) -> Result<File> {
    let (dir, name) = parent(root, relative)?;
    let mut options = OpenOptions::new();
    options
        .read(!write)
        .write(write)
        .create_new(create_new)
        .follow(FollowSymlinks::No);
    #[cfg(unix)]
    {
        use cap_std::fs::OpenOptionsExt;
        options.mode(if write { 0o600 } else { 0 });
        options.custom_flags(libc::O_NONBLOCK);
    }
    let file = dir.open_with(name, &options)?.into_std();
    require(
        file.metadata()?.is_file(),
        "Se esperaba un archivo regular.",
    )?;
    Ok(file)
}
pub fn read(path: &Path, limit: usize) -> Result<Vec<u8>> {
    let root = directory(
        path.parent().ok_or_else(|| Error::new("Ruta inválida"))?,
        false,
    )?;
    let mut file = open(
        &root,
        path.file_name()
            .and_then(|s| s.to_str())
            .ok_or_else(|| Error::new("Ruta inválida"))?,
        false,
        false,
    )?;
    let mut bytes = Vec::new();
    Read::by_ref(&mut file)
        .take(limit as u64 + 1)
        .read_to_end(&mut bytes)?;
    require(bytes.len() <= limit, "Archivo demasiado grande.")?;
    Ok(bytes)
}
pub fn json(path: &Path) -> Result<Value> {
    Ok(serde_json::from_slice(&read(path, 8 * 1024 * 1024)?)?)
}
pub fn sync(_dir: &Dir) -> Result<()> {
    #[cfg(unix)]
    {
        use std::os::fd::{AsRawFd, FromRawFd};
        let descriptor = unsafe {
            libc::openat(
                _dir.as_raw_fd(),
                c".".as_ptr(),
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            )
        };
        if descriptor < 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        unsafe { File::from_raw_fd(descriptor) }.sync_all()?;
    }
    Ok(())
}
pub fn atomic(path: &Path, bytes: &[u8]) -> Result<()> {
    let dir = directory(
        path.parent().ok_or_else(|| Error::new("Ruta inválida"))?,
        true,
    )?;
    let name = path
        .file_name()
        .and_then(|x| x.to_str())
        .ok_or_else(|| Error::new("Ruta inválida"))?;
    if let Ok(m) = dir.symlink_metadata(name) {
        require(
            !m.file_type().is_symlink() && m.is_file(),
            "Destino inseguro.",
        )?;
    }
    let temp = format!(".ember-tmp-{}", uuid::Uuid::new_v4().simple());
    let result = (|| {
        let mut file = open(&dir, &temp, true, true)?;
        file.write_all(bytes)?;
        file.sync_all()?;
        dir.rename(&temp, &dir, name)?;
        sync(&dir)
    })();
    let _ = dir.remove_file(temp);
    result
}
pub fn atomic_json(path: &Path, value: &Value) -> Result<()> {
    atomic(path, &serde_json::to_vec(value)?)
}
pub fn sha(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
pub fn hash(file: &mut File, md5: bool) -> Result<String> {
    let before = file.metadata()?;
    let mut sha = Sha256::new();
    let mut md = Md5::new();
    let mut buf = [0u8; 262144];
    loop {
        let n = file.read(&mut buf)?;
        if n == 0 {
            break;
        }
        if md5 {
            md.update(&buf[..n]);
        } else {
            sha.update(&buf[..n]);
        }
    }
    let after = file.metadata()?;
    require(
        before.len() == after.len() && before.modified()? == after.modified()?,
        "El archivo cambió durante la verificación.",
    )?;
    Ok(if md5 {
        format!("{:x}", md.finalize())
    } else {
        format!("{:x}", sha.finalize())
    })
}
pub fn hash_path(path: &Path, md5: bool) -> Result<String> {
    let dir = directory(
        path.parent().ok_or_else(|| Error::new("Ruta inválida"))?,
        false,
    )?;
    hash(
        &mut open(
            &dir,
            path.file_name()
                .unwrap()
                .to_str()
                .ok_or_else(|| Error::new("Ruta inválida"))?,
            false,
            false,
        )?,
        md5,
    )
}
pub struct Lock(pub File);
impl Lock {
    pub fn at(dir: &Dir, name: &str) -> Result<Self> {
        let mut options = OpenOptions::new();
        options
            .read(true)
            .write(true)
            .create(true)
            .follow(FollowSymlinks::No);
        #[cfg(unix)]
        {
            use cap_std::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let file = dir.open_with(name, &options)?.into_std();
        require(file.metadata()?.is_file(), "Bloqueo inseguro.")?;
        file.try_lock_exclusive()
            .map_err(|_| Error::new("La instalación está en uso por otra operación."))?;
        Ok(Self(file))
    }
    pub fn game(path: &Path) -> Result<Self> {
        let dir = directory(path, false)?;
        let lock = Self::at(&dir, ".stove-update.lock")?;
        let journal = path.join(".stove-update-transaction");
        if std::fs::symlink_metadata(&journal).is_ok() {
            let state = json(&journal.join("journal.json"))?;
            require(
                matches!(
                    state["phase"].as_str(),
                    Some("committed" | "files_verified" | "rolled_back")
                ),
                "La actualización necesita recuperación.",
            )?;
        }
        Ok(lock)
    }
}
pub fn publish(source: &Path, dest: &Path) -> Result<()> {
    let root = directory(
        source.parent().ok_or_else(|| Error::new("Ruta inválida"))?,
        false,
    )?;
    require(
        source.parent() == dest.parent(),
        "La publicación debe estar en la misma carpeta.",
    )?;
    #[cfg(unix)]
    let a = source.file_name().unwrap();
    #[cfg(unix)]
    let b = dest.file_name().unwrap();
    #[cfg(target_os = "linux")]
    {
        use std::os::{fd::AsRawFd, unix::ffi::OsStrExt};
        let a = std::ffi::CString::new(a.as_bytes()).map_err(|_| Error::new("Ruta inválida"))?;
        let b = std::ffi::CString::new(b.as_bytes()).map_err(|_| Error::new("Ruta inválida"))?;
        if unsafe {
            libc::renameat2(
                root.as_raw_fd(),
                a.as_ptr(),
                root.as_raw_fd(),
                b.as_ptr(),
                libc::RENAME_NOREPLACE,
            )
        } != 0
        {
            return Err(std::io::Error::last_os_error().into());
        }
    }
    #[cfg(target_os = "macos")]
    {
        use std::os::{fd::AsRawFd, unix::ffi::OsStrExt};
        let a = std::ffi::CString::new(a.as_bytes()).map_err(|_| Error::new("Ruta inválida"))?;
        let b = std::ffi::CString::new(b.as_bytes()).map_err(|_| Error::new("Ruta inválida"))?;
        if unsafe {
            libc::renameatx_np(
                root.as_raw_fd(),
                a.as_ptr(),
                root.as_raw_fd(),
                b.as_ptr(),
                libc::RENAME_EXCL,
            )
        } != 0
        {
            return Err(std::io::Error::last_os_error().into());
        }
    }
    #[cfg(windows)]
    {
        std::fs::rename(source, dest)?;
    }
    sync(&root)
}
