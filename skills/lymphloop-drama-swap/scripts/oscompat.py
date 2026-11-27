"""The OS differences of this skill in one place, so the same scripts run on macOS, Windows and Linux.

- UTF-8 mode on Windows: every script here reads and writes UTF-8 (Chinese notes, prompts, JSON) with the default
  encoding, and talks to ffmpeg / node through text pipes. On Windows the default is the ANSI code page, so a script
  started without UTF-8 mode is started again with it (PYTHONUTF8=1, inherited by every child Python). Importing this
  module first is what does it; nothing happens on macOS / Linux or when UTF-8 mode is already on.
- File locks: flock on POSIX, LockFileEx on Windows (shared or exclusive, released by the OS when the holder dies).
- pid_alive, detached child processes, font folders, the private Python of a skill folder, executable names.
"""
from __future__ import annotations
import contextlib, os, subprocess, sys, time
from pathlib import Path

WINDOWS = os.name == 'nt'
MACOS = sys.platform == 'darwin'
HERE = Path(__file__).resolve().parent


def _is_skill_script():
    try: return bool(sys.argv and sys.argv[0]) and Path(sys.argv[0]).resolve().parent == HERE
    except OSError: return False


def ensure_utf8():
    """On Windows, restart this script in UTF-8 mode unless it already is. Only for scripts of this folder run as a
    program (tests and other hosts set PYTHONUTF8=1 themselves)."""
    if not WINDOWS or sys.flags.utf8_mode or os.environ.get('PYTHONUTF8') == '1' or not _is_skill_script(): return
    os.environ['PYTHONUTF8'] = '1'
    argv = getattr(sys, 'orig_argv', None) or [sys.executable, *sys.argv]
    raise SystemExit(subprocess.call([sys.executable, *argv[1:]]))


ensure_utf8()


# ---------------------------------------------------------------- file locks
if WINDOWS:
    import ctypes, msvcrt
    from ctypes import wintypes

    class _Overlapped(ctypes.Structure):
        _fields_ = [('Internal', ctypes.c_void_p), ('InternalHigh', ctypes.c_void_p), ('Offset', wintypes.DWORD),
                    ('OffsetHigh', wintypes.DWORD), ('hEvent', wintypes.HANDLE)]

    _k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _k32.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped)]
    _k32.LockFileEx.restype = wintypes.BOOL
    _k32.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped)]
    _k32.UnlockFileEx.restype = wintypes.BOOL
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _k32.GetExitCodeProcess.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _LOCK_EXCLUSIVE, _LOCK_FAIL_NOW = 0x2, 0x1

    def _region():
        # One byte far beyond any content: Windows locks are mandatory, and the lock files also hold a note about
        # the holder that other processes read.
        o = _Overlapped(); o.Offset = 0xFFFFFFFE; o.OffsetHigh = 0x7FFFFFFF; return o

    def lock_file(fh, exclusive=True, blocking=True):
        """Lock an open file. Non-blocking and already held elsewhere: OSError."""
        flags = (_LOCK_EXCLUSIVE if exclusive else 0) | (0 if blocking else _LOCK_FAIL_NOW)
        if not _k32.LockFileEx(msvcrt.get_osfhandle(fh.fileno()), flags, 0, 1, 0, ctypes.byref(_region())):
            raise OSError(ctypes.get_last_error(), 'file is locked by another process')

    def unlock_file(fh):
        _k32.UnlockFileEx(msvcrt.get_osfhandle(fh.fileno()), 0, 1, 0, ctypes.byref(_region()))

    def pid_alive(pid):
        try: pid = int(pid)
        except (TypeError, ValueError): return False
        if pid <= 0: return False
        h = _k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h: return ctypes.get_last_error() == 5     # access denied: it exists, it is just not ours
        try:
            code = wintypes.DWORD()
            return bool(_k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259   # STILL_ACTIVE
        finally: _k32.CloseHandle(h)
else:
    import fcntl

    def lock_file(fh, exclusive=True, blocking=True):
        """Lock an open file. Non-blocking and already held elsewhere: OSError."""
        fcntl.flock(fh.fileno(), (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | (0 if blocking else fcntl.LOCK_NB))

    def unlock_file(fh):
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def pid_alive(pid):
        try:
            pid = int(pid)
            if pid <= 0: return False      # 0 and -1 address process groups, not a process
            os.kill(pid, 0); return True
        except Exception: return False


@contextlib.contextmanager
def locked(path, exclusive=True, wait=None, poll=0.5):
    """Hold the lock file `path` for the block. wait=None blocks; a number polls up to that many seconds and then
    raises TimeoutError."""
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a+b') as f:
        if wait is None: lock_file(f, exclusive)
        else:
            deadline = time.monotonic() + max(0.0, float(wait))
            while True:
                try: lock_file(f, exclusive, blocking=False); break
                except OSError:
                    if time.monotonic() >= deadline: raise TimeoutError(str(path)) from None
                    time.sleep(poll)
        try: yield f
        finally: unlock_file(f)


# ---------------------------------------------------------------- processes and tools
def detached():
    """Popen keywords for a daemon that outlives this process and its terminal."""
    if WINDOWS: return {'creationflags': subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {'start_new_session': True}


def exe(name):
    """'ffmpeg' -> 'ffmpeg.exe' on Windows."""
    return name + '.exe' if WINDOWS and not str(name).lower().endswith('.exe') else str(name)


def venv_python(skill):
    """The private interpreter of a skill folder: .venv/bin/python, or .venv\\Scripts\\python.exe on Windows."""
    skill = Path(skill)
    return skill/'.venv'/'Scripts'/'python.exe' if WINDOWS else skill/'.venv'/'bin'/'python'


# ---------------------------------------------------------------- fonts
def font_dirs():
    """Folders that hold the fonts installed on this machine."""
    if WINDOWS:
        dirs = [Path(os.environ.get('WINDIR', r'C:\Windows'))/'Fonts']
        if os.environ.get('LOCALAPPDATA'): dirs.append(Path(os.environ['LOCALAPPDATA'])/'Microsoft'/'Windows'/'Fonts')
    elif MACOS:
        dirs = [Path('/System/Library/Fonts'), Path('/System/Library/Fonts/Supplemental'), Path('/Library/Fonts'), Path.home()/'Library/Fonts']
    else:
        dirs = [Path('/usr/share/fonts'), Path('/usr/local/share/fonts'), Path.home()/'.fonts', Path.home()/'.local/share/fonts']
    return [d for d in dirs if d.is_dir()]


def find_font(*names):
    """The first of these font file names found on this machine (searched below every font folder), or None."""
    dirs = font_dirs()
    for name in names:
        for d in dirs:
            p = d/name
            if p.is_file(): return str(p)
        if not WINDOWS and not MACOS:
            for d in dirs:
                hit = next(iter(sorted(d.rglob(name))), None)
                if hit: return str(hit)
    return None


SANS_REGULAR = ('Arial.ttf', 'arial.ttf', 'Helvetica.ttc', 'segoeui.ttf', 'DejaVuSans.ttf', 'LiberationSans-Regular.ttf')
SANS_BOLD = ('Arial Bold.ttf', 'arialbd.ttf', 'Helvetica.ttc', 'segoeuib.ttf', 'DejaVuSans-Bold.ttf', 'LiberationSans-Bold.ttf')


if __name__ == '__main__':      # what this machine looks like to the scripts
    import json, locale
    print(json.dumps({'platform': sys.platform, 'python': sys.executable, 'utf8_mode': sys.flags.utf8_mode,
                      'default_encoding': locale.getpreferredencoding(False), 'font_dirs': [str(d) for d in font_dirs()],
                      'sans': find_font(*SANS_REGULAR)}, ensure_ascii=False, indent=1))
