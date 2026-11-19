"""Linux-only worker. Limit a system pdftotext process before it reads stdin."""
import ctypes
import errno
import os
from pathlib import Path
import resource
import struct
import sys


READER = "/usr/bin/pdftotext"
# Only the binary, loader/libraries, and public font/encoding data are needed.
RESOURCES = (READER, "/lib", "/lib64", "/usr/lib", "/usr/lib64", "/etc/fonts",
             "/usr/share/fonts", "/usr/share/fontconfig", "/usr/share/poppler",
             "/var/cache/fontconfig", "/etc/ld.so.cache")
LIBC = ctypes.CDLL(None, use_errno=True)


def checked(value):
    if value < 0:
        raise OSError(ctypes.get_errno(), "PDF worker isolation unavailable")
    return value


def landlock():
    # Flight Wall targets native x86_64 Linux. These are the Landlock syscall
    # numbers for that architecture (include/uapi/asm-generic/unistd.h).
    if os.uname().machine != "x86_64":
        raise OSError("Unsupported architecture")
    abi = LIBC.syscall(444, 0, 0, 1)
    if abi < 1:
        return False
    # Handle all ABI 1 filesystem rights, plus REFER and TRUNCATE if present.
    handled = (1 << (15 if abi >= 3 else 14 if abi == 2 else 13)) - 1
    rules = ctypes.c_uint64(handled)
    fd = checked(LIBC.syscall(444, ctypes.byref(rules), 8, 0))
    try:
        for location in RESOURCES:
            if not os.path.exists(location):
                continue
            parent = os.open(location, os.O_PATH | os.O_CLOEXEC)
            try:
                # EXECUTE, READ_FILE, and (for directories) READ_DIR.
                access = 5 | (8 if os.path.isdir(location) else 0)
                rule = ctypes.create_string_buffer(struct.pack("=Qi", access, parent))
                checked(LIBC.syscall(445, fd, 1, ctypes.byref(rule), 0))
            finally:
                os.close(parent)
        checked(LIBC.syscall(446, fd, 0))
    finally:
        os.close(fd)
    return True


def system_only_root(mountpoint):
    # Some Batocera kernels omit Landlock. In that case create a private
    # mount namespace, bind the same system files, then discard privileges.
    root = Path(mountpoint)
    checked(LIBC.unshare(0x00020000))  # CLONE_NEWNS
    checked(LIBC.mount(None, b"/", None, 16384 | (1 << 18), None))  # REC|PRIVATE
    checked(LIBC.mount(b"tmpfs", os.fsencode(root), b"tmpfs", 2 | 4, b"size=1m,mode=0755"))
    # Batocera starts the service with umask 077. These system-only mount
    # points must remain traversable after this process drops to nobody.
    os.umask(0o022)
    for location in RESOURCES:
        if not os.path.exists(location):
            continue
        target = root / location.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        if os.path.isdir(location):
            target.mkdir(exist_ok=True)
        else:
            target.touch()
        checked(LIBC.mount(os.fsencode(location), os.fsencode(target), None, 4096, None))  # BIND
        checked(LIBC.mount(None, os.fsencode(target), None, 4096 | 32 | 1 | 2 | 4, None))
    os.chroot(root)
    os.chdir("/")


def allow_reader_syscalls(library):
    # Default-deny includes network, subprocess creation, and namespace changes.
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    ctx = library.seccomp_init(0x00050000 | errno.EPERM)
    if not ctx:
        raise OSError("No syscall filter")
    try:
        names = (
            "read write readv writev close pread64 lseek fstat newfstatat stat statx access "
            "faccessat faccessat2 open openat readlink readlinkat fcntl getdents64 getcwd "
            "mmap mprotect munmap brk mremap madvise ioctl futex getrandom clock_gettime "
            "gettimeofday getuid geteuid getgid getegid getpid gettid arch_prctl prlimit64 "
            "set_tid_address set_robust_list rseq rt_sigaction rt_sigprocmask rt_sigreturn "
            "sigaltstack sched_getaffinity uname execve exit exit_group"
        )
        for name in names.split():
            syscall = library.seccomp_syscall_resolve_name(name.encode("ascii"))
            if syscall >= 0:
                checked(library.seccomp_rule_add(ctx, 0x7FFF0000, syscall, 0))
        checked(library.seccomp_load(ctx))
    finally:
        library.seccomp_release(ctx)


def isolate(mountpoint, size):
    for kind, cap in ((resource.RLIMIT_CPU, 3), (resource.RLIMIT_AS, 256 * 1024 * 1024),
                      (resource.RLIMIT_FSIZE, size), (resource.RLIMIT_CORE, 0),
                      (resource.RLIMIT_NOFILE, 32)):
        resource.setrlimit(kind, (cap, cap))
    seccomp = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    checked(LIBC.prctl(38, 1, 0, 0, 0))  # PR_SET_NO_NEW_PRIVS
    if not landlock():
        system_only_root(mountpoint)
    if os.geteuid() == 0:
        os.setgroups([])
        os.setgid(65534)
        os.setuid(65534)
    allow_reader_syscalls(seccomp)


def main():
    mountpoint, pages, size = sys.argv[1:]
    if not (1 <= int(pages) <= 6 and 0 < int(size) <= 96 * 1024):
        raise ValueError("Invalid worker limits")
    isolate(mountpoint, int(size))
    # Extract one overflow page to detect incomplete/mixed itineraries.
    # pdftotext appends one form feed per page, including blank pages.
    os.execv(READER, [READER, "-layout", "-enc", "UTF-8", "-f", "1", "-l",
                     str(int(pages) + 1), "-", "-"])


if __name__ == "__main__":
    main()
