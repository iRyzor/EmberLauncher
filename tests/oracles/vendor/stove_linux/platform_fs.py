"""OS filesystem primitives; Windows directories remain pinned against replacement."""

import ctypes
import errno
import os as native_os
from pathlib import Path
import sys
import stat

if sys.platform == "win32":
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetLongPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetLongPathNameW.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    kernel.FlushFileBuffers.argtypes = [wintypes.HANDLE]

    def fail():
        raise ctypes.WinError(ctypes.get_last_error())

    def long_path(path):
        """Expand 8.3 aliases without resolving junctions or symbolic links."""
        buffer = ctypes.create_unicode_buffer(32768)
        count = kernel.GetLongPathNameW(str(path), buffer, len(buffer))
        if 0 < count < len(buffer):
            return Path(buffer.value)
        code = ctypes.get_last_error()
        if not count and code in (2, 3) and path != path.parent:
            return long_path(path.parent) / path.name
        fail()

    def canonical(handle):
        buffer = ctypes.create_unicode_buffer(32768)
        count = kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        if not count or count >= len(buffer):
            fail()
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return native_os.path.normcase(native_os.path.abspath(value))

    class WindowsOS:
        O_DIRECTORY = 0x10000000
        O_NOFOLLOW = 0x20000000
        O_NONBLOCK = 0

        def __init__(self):
            self.directories = {}
            self.counter = -100000

        def __getattr__(self, name):
            return getattr(native_os, name)

        def path(self, name, dir_fd=None):
            if dir_fd is None:
                return native_os.path.abspath(native_os.fspath(name))
            handle, expected = self.directories[dir_fd]
            if canonical(handle) != expected:
                raise OSError("Pinned directory moved")
            text = native_os.fspath(name)
            if native_os.path.isabs(text) or any(
                x in (".", "..", "") for x in text.replace("\\", "/").split("/")
            ):
                raise ValueError("Unsafe relative filesystem path")
            return native_os.path.join(expected, text)

        def open(self, name, flags, mode=0o777, *, dir_fd=None):
            try:
                path = str(path_without_links(self.path(name, dir_fd)))
            except ValueError:
                raise OSError("Reparse points are not permitted") from None
            directory = bool(flags & self.O_DIRECTORY)
            access = 0x80000000
            if flags & (native_os.O_WRONLY | native_os.O_RDWR):
                access |= 0x40000000
            disposition = 3
            if flags & native_os.O_CREAT:
                disposition = (
                    1
                    if flags & native_os.O_EXCL
                    else 2
                    if flags & native_os.O_TRUNC
                    else 4
                )
            elif flags & native_os.O_TRUNC:
                disposition = 5
            attributes = 0x00200000 | (0x02000000 if directory else 0)
            share = 7 if Path(path).name == ".stove-update.lock" else 3
            handle = kernel.CreateFileW(
                path, access, share, None, disposition, attributes, None
            )
            if handle == ctypes.c_void_p(-1).value:
                fail()
            try:
                info = (ctypes.c_uint32 * 13)()
                if not kernel.GetFileInformationByHandle(handle, info):
                    fail()
                if info[0] & 0x400:
                    raise OSError("Reparse points are not permitted")
                if bool(info[0] & 0x10) != directory:
                    raise OSError("Unexpected filesystem object")
                if canonical(handle) != native_os.path.normcase(path):
                    raise OSError("Filesystem path changed")
                if directory:
                    self.counter -= 1
                    descriptor = self.counter
                    self.directories[descriptor] = (handle, canonical(handle))
                    handle = None
                    return descriptor
                descriptor = msvcrt.open_osfhandle(
                    handle,
                    (
                        flags
                        & (native_os.O_RDONLY | native_os.O_WRONLY | native_os.O_RDWR)
                    )
                    | native_os.O_BINARY,
                )
                handle = None
                return descriptor
            finally:
                if handle is not None:
                    kernel.CloseHandle(handle)

        def close(self, fd):
            if fd in self.directories:
                handle, _ = self.directories.pop(fd)
                kernel.CloseHandle(handle)
            else:
                native_os.close(fd)

        def dup(self, fd):
            if fd in self.directories:
                return self.open(
                    self.directories[fd][1], self.O_DIRECTORY | self.O_NOFOLLOW
                )
            return native_os.dup(fd)

        def fsync(self, fd):
            if fd not in self.directories:
                return native_os.fsync(fd)
            # Windows file writes and MoveFileEx WRITE_THROUGH provide durability;
            # directory FlushFileBuffers is not supported on all filesystems.
            handle, _ = self.directories[fd]
            if not kernel.FlushFileBuffers(handle) and ctypes.get_last_error() not in (
                1,
                5,
                6,
            ):
                fail()

        def fchmod(self, fd, mode):
            pass

        def stat(self, name, *, dir_fd=None, follow_symlinks=True):
            result = native_os.stat(
                self.path(name, dir_fd), follow_symlinks=follow_symlinks
            )
            if getattr(result, "st_file_attributes", 0) & 0x400:
                raise OSError("Reparse points are not permitted")
            return result

        def mkdir(self, name, mode=0o777, *, dir_fd=None):
            return native_os.mkdir(self.path(name, dir_fd), mode)

        def unlink(self, name, *, dir_fd=None):
            return native_os.unlink(self.path(name, dir_fd))

        def rmdir(self, name, *, dir_fd=None):
            return native_os.rmdir(self.path(name, dir_fd))

        def replace(self, source, target, *, src_dir_fd=None, dst_dir_fd=None):
            if not kernel.MoveFileExW(
                self.path(source, src_dir_fd), self.path(target, dst_dir_fd), 0x1 | 0x8
            ):
                fail()

        def rename(self, source, target, *, src_dir_fd=None, dst_dir_fd=None):
            if not kernel.MoveFileExW(
                self.path(source, src_dir_fd), self.path(target, dst_dir_fd), 0x8
            ):
                fail()

    os = WindowsOS()

    class FileLock:
        LOCK_EX = 1
        LOCK_NB = 2

        def flock(self, fd, flags):
            native_os.lseek(fd, 0, native_os.SEEK_SET)
            msvcrt.locking(
                fd, msvcrt.LK_NBLCK if flags & self.LOCK_NB else msvcrt.LK_LOCK, 1
            )

    fcntl = FileLock()
else:
    import fcntl

    os = native_os


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_no_replace(source, destination):
    if sys.platform == "win32":
        os.rename(source, destination)
        return
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        function = library.renamex_np
        function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        arguments = [native_os.fsencode(source), native_os.fsencode(destination), 4]
    else:
        function = library.renameat2
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        arguments = [
            -100,
            native_os.fsencode(source),
            -100,
            native_os.fsencode(destination),
            1,
        ]
    function.restype = ctypes.c_int
    if function(*arguments) != 0:
        code = ctypes.get_errno()
        raise OSError(code, "Cannot publish without replacing the destination")


def path_without_links(name):
    """Accept Windows short-name aliases, reject links/reparse points in ancestors."""
    path = Path(native_os.path.abspath(native_os.fspath(name)))
    if sys.platform == 'darwin' and len(path.parts) > 1:
        # macOS exposes these OS-owned aliases, including tempfile's /var/folders.
        # Accept only their expected targets; links inside the library remain rejected.
        aliases = {'var': '/private/var', 'tmp': '/private/tmp', 'etc': '/private/etc'}
        target = aliases.get(path.parts[1])
        if target:
            alias = Path('/') / path.parts[1]
            try:
                alias_info = native_os.lstat(alias)
            except FileNotFoundError:
                alias_info = None
            if alias_info and stat.S_ISLNK(alias_info.st_mode) and native_os.path.realpath(alias) == target:
                path = Path(target).joinpath(*path.parts[2:])
    for component in (path, *path.parents):
        try:
            info = native_os.lstat(component)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Filesystem path traverses a link or reparse point')
    return long_path(path) if sys.platform == 'win32' else path
