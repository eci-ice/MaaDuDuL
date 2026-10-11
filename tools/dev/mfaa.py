"""Explicit preparation and launch of the local MFAA workspace.

No operation runs on import. All mutations are limited to the owned payload;
GUI binaries, configuration, logs and screenshots are never cleaned here.
"""

import argparse
import errno
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE = ".mddl-dev.json"
LOCK = ".mddl-dev.lock"
TREES = ("resource", "tasks", "locales", "agent")
EXCLUDED = {"__pycache__", "config", "debug", "logs", ".git"}


class WorkspaceBusy(ValueError):
    pass


def lock_descriptor(descriptor: int) -> None:
    """Non-blocking OS lock; closing the descriptor (even on process death) releases it."""
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            raise WorkspaceBusy("Workspace busy: another management command is running.") from None
        raise


@contextmanager
def management_lock(workspace: Path, *, inspect_only: bool = False):
    # Keep the inode/path stable across installation's directory swap. Never unlink it.
    path = safe_path(workspace.parent, f".{workspace.name}.mddl-management.lock")
    try:
        descriptor = os.open(path, os.O_RDWR | (0 if inspect_only else os.O_CREAT), 0o600)
    except FileNotFoundError:
        if not inspect_only:
            raise
        yield
        return
    try:
        lock_descriptor(descriptor)
        yield
    finally:
        os.close(descriptor)


def legacy_lock_owner(workspace: Path) -> bool:
    """Return whether a legacy marker exists; refuse live or unverifiable owners."""
    path = safe_path(workspace, LOCK)
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return False
    if not value.isascii() or not value.isdecimal() or not 0 < int(value) <= 0xFFFFFFFF:
        raise WorkspaceBusy(f"Unverifiable legacy lock: {path}. Verify no old management command/GUI/Agent "
                            "is running before removing it manually.")
    owner = int(value)
    if sys.platform == "win32":
        # os.kill(pid, 0) is not a safe process probe on Windows.
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"[bool](Get-Process -Id {owner} -ErrorAction SilentlyContinue)"],
            capture_output=True, text=True, check=False, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode or result.stdout.strip() not in ("True", "False"):
            raise WorkspaceBusy(f"Cannot inspect legacy lock owner PID {owner}; cancelled.")
        running = result.stdout.strip() == "True"
    else:
        try:
            os.kill(owner, 0)
            running = True
        except ProcessLookupError:
            running = False
        except PermissionError:
            running = True
    if running:
        raise WorkspaceBusy(f"Workspace busy: legacy lock owner PID {owner} is still running ({path}).")
    return True


@contextmanager
def workspace_lock(workspace: Path):
    with management_lock(workspace):
        stale = legacy_lock_owner(workspace)
        if workspace.exists() and any(path.name != LOCK for path in workspace.iterdir()):
            ensure_gui_stopped(executable(workspace))
        ensure_agent_stopped(workspace)
        lock = safe_path(workspace, LOCK)
        if stale:
            lock.unlink()
            print(f"Recovered stale workspace lock: {lock}")
        # Retain a PID marker so older launchers also refuse concurrent access.
        with lock.open("x", encoding="utf-8") as stream:
            stream.write(str(os.getpid()))
        try:
            yield
        finally:
            lock.unlink(missing_ok=True)


def workspace_busy(workspace: Path) -> bool:
    try:
        with management_lock(workspace, inspect_only=True):
            legacy_lock_owner(workspace)
    except WorkspaceBusy:
        return True
    return False


def runtime_platform() -> str:
    system = {"Windows": "win", "Linux": "linux", "Darwin": "osx"}.get(platform.system())
    arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine().lower())
    if not system or not arch:
        raise ValueError("Unsupported operating system or CPU architecture.")
    return f"{system}-{arch}"


def framework_version(workspace: Path) -> str:
    """Read MaaVersion in a child process so DLLs are released before replacement."""
    filename = {"win32": "MaaFramework.dll", "darwin": "libMaaFramework.dylib"}.get(sys.platform, "libMaaFramework.so")
    library = safe_path(workspace, f"runtimes/{runtime_platform()}/native/{filename}")
    if not library.is_file():
        raise ValueError(f"MaaFramework library missing: {library}. Run yarn dev:install.")
    probe = (
        "import ctypes, os, pathlib, sys; "
        "p = pathlib.Path(sys.argv[1]); "
        "dll_dir = os.add_dll_directory(str(p.parent)) if sys.platform == 'win32' else None; "
        "lib = (ctypes.WinDLL if sys.platform == 'win32' else ctypes.CDLL)(str(p)); "
        "lib.MaaVersion.restype = ctypes.c_char_p; "
        "lib.MaaVersion.argtypes = []; print('MDDL_MAA_VERSION=' + lib.MaaVersion().decode())"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-B", "-c", probe, str(library)],
            cwd=tempfile.gettempdir(), capture_output=True, text=True, timeout=15, check=False,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}),
        )
    except subprocess.TimeoutExpired as error:
        raise ValueError("MaaFramework version probe timed out; run yarn dev:install.") from error
    versions = [
        line.removeprefix("MDDL_MAA_VERSION=")
        for line in result.stdout.splitlines() if line.startswith("MDDL_MAA_VERSION=")
    ]
    if result.returncode or len(versions) != 1:
        raise ValueError(f"Cannot read MaaFramework version: {result.stderr.strip()}. Run yarn dev:install.")
    return versions[0].removeprefix("v")


def runtime_ready(workspace: Path) -> bool:
    expected = json.loads((ROOT / "maa-project.json").read_text(encoding="utf-8"))["maafw"]["version"]
    actual = framework_version(workspace)
    print(f"MFAA MaaFramework: {actual}; required: {expected}")
    if actual != expected:
        print("Runtime mismatch. Close MFAA, run yarn dev:install --framework-only, then yarn dev:prepare.")
        return False
    return True


def safe_path(root: Path, relative: str) -> Path:
    path = root / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError(f"Unsafe workspace path: {relative}")
    if not path.resolve().is_relative_to(root) or path == root:
        raise ValueError(f"Path escapes workspace: {path}")
    for item in (path, *path.parents):
        if item == root:
            break
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise ValueError(f"Linked workspace paths are not supported: {item}")
    return path


def inventory(root: Path) -> dict[str, Path]:
    found = {}
    for tree in TREES:
        base = safe_path(root, tree)
        if not base.exists():
            continue
        for directory, dirs, files in os.walk(base, followlinks=False):
            dirs[:] = [name for name in dirs if name not in EXCLUDED]
            for name in dirs:
                safe_path(root, (Path(directory) / name).relative_to(root).as_posix())
            for name in files:
                if name.endswith((".pyc", ".pyo")):
                    continue
                relative = (Path(directory) / name).relative_to(root).as_posix()
                found[relative] = safe_path(root, relative)
    for relative in ("interface.json", "public/logo.png"):
        path = safe_path(root, relative)
        if path.is_file():
            found[relative] = path
    return found


def digest(files: dict[str, Path]) -> str:
    result = hashlib.sha256()
    for name, path in sorted(files.items()):
        result.update(name.encode())
        result.update(b"\0")
        result.update(hashlib.sha256(path.read_bytes()).digest())
    return result.hexdigest()


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".mddl-")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def executable(workspace: Path) -> Path:
    names = ("MFAAvalonia.exe",) if sys.platform == "win32" else (
        "MFAAvalonia", "MFAAvalonia.app/Contents/MacOS/MFAAvalonia",
    )
    for name in names:
        path = workspace / name
        if path.is_file():
            return path
    raise ValueError(f"MFAA executable missing in {workspace}")


def ensure_gui_stopped(gui: Path) -> None:
    """Also detect GUIs opened outside the managed launcher."""
    if sys.platform == "win32":
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "@(Get-Process | Where-Object ProcessName -eq MFAAvalonia | "
             "Select-Object Id,Path) | ConvertTo-Json -Compress"],
            capture_output=True, text=True, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode:
            raise ValueError("Cannot inspect running MFAA processes; preparation cancelled.")
        processes = json.loads(result.stdout.strip() or "[]")
        if isinstance(processes, dict):
            processes = [processes]
        # Missing path may mean an elevated process that cannot be inspected.
        running = any(
            not item.get("Path") or os.path.normcase(item["Path"]) == os.path.normcase(str(gui))
            for item in processes
        )
    else:
        result = subprocess.run(["ps", "-ax", "-o", "comm="], capture_output=True, text=True, check=False)
        if result.returncode:
            raise ValueError("Cannot inspect running MFAA processes; preparation cancelled.")
        running = any(Path(line.strip()).name == gui.name for line in result.stdout.splitlines())
    if running:
        raise ValueError("MFAA is running. Close the GUI and its Agent before preparing or starting another session.")


def ensure_agent_stopped(workspace: Path) -> None:
    """A killed launcher/GUI can leave its Python Agent behind."""
    if sys.platform == "win32":
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "$ErrorActionPreference = 'Stop'; "
             "@(Get-CimInstance Win32_Process | Where-Object Name -Match '^python.*' | "
             "Select-Object ProcessId,CommandLine) | ConvertTo-Json -Compress"],
            capture_output=True, text=True, check=False, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode:
            raise ValueError("Cannot inspect running Agent processes; cancelled.")
        processes = json.loads(result.stdout.strip() or "[]")
        if isinstance(processes, dict):
            processes = [processes]
        # CIM can return a missing command line for an unrelated elevated process
        # or one exiting during enumeration. That is not evidence of a workspace
        # Agent. Keep checking the remaining processes rather than blocking all
        # development commands (or skipping a visible Agent later in the list).
        commands = [command for item in processes if isinstance(command := item.get("CommandLine"), str) and command]
    else:
        result = subprocess.run(["ps", "-ax", "-o", "args="], capture_output=True, text=True, check=False)
        if result.returncode:
            raise ValueError("Cannot inspect running Agent processes; cancelled.")
        commands = result.stdout.splitlines()
    absolute = str(workspace / "agent" / "main.py").replace("\\", "/")
    for command in commands:
        normalized = command.replace("\\", "/")
        if sys.platform == "win32":
            normalized, absolute = normalized.casefold(), absolute.casefold()
        # Relative paths have no inspectable cwd here: conservatively require exit.
        if re.search(r'(?:^|[\s\"\'])(?:\./)?agent/main\.py(?:[\s\"\']|$)', normalized) or absolute in normalized:
            raise ValueError("A workspace Agent may still be running. Close its GUI/Agent before continuing.")


def prepare(workspace: Path) -> None:
    import jsonc

    if not runtime_ready(workspace):
        raise ValueError("Preparation cancelled: MFAA and the project require different MaaFramework versions.")
    sources = inventory(ROOT)
    source_digest = digest(sources)
    for name in (*TREES, "interface.json", "public/logo.png"):
        if not (ROOT / name).exists():
            raise ValueError(f"Missing source: {name}")
    # Validate all destinations before the first write, including junctions.
    old = inventory(workspace)
    for name in sources:
        safe_path(workspace, name)
    interface = jsonc.loads(sources["interface.json"].read_text(encoding="utf-8"))
    agents = interface["agent"]
    for agent in agents if isinstance(agents, list) else [agents]:
        agent["child_exec"] = os.path.abspath(sys.executable)
        agent["child_args"] = ["-u", "agent/main.py"]
    generated = json.dumps(interface, ensure_ascii=False, indent=4).encode("utf-8") + b"\n"
    state_path = safe_path(workspace, STATE)
    # A partial preparation cannot subsequently be mistaken for a ready workspace.
    state_path.unlink(missing_ok=True)
    for name, path in sources.items():
        data = generated if name == "interface.json" else path.read_bytes()
        target = safe_path(workspace, name)
        if not target.is_file() or target.read_bytes() != data:
            write_atomic(target, data)
    for name in old.keys() - sources.keys():
        safe_path(workspace, name).unlink()
    # Preserve empty resource overlays as well as file-containing directories.
    for tree in TREES:
        for directory, dirs, _ in os.walk(ROOT / tree):
            dirs[:] = [name for name in dirs if name not in EXCLUDED]
            relative = Path(directory).relative_to(ROOT).as_posix()
            safe_path(workspace, relative).mkdir(parents=True, exist_ok=True)
    if digest(inventory(ROOT)) != source_digest:
        raise ValueError("Source changed during preparation; run yarn dev:prepare again.")
    state = {
        "source": str(ROOT), "python": os.path.abspath(sys.executable),
        "prepared_at": datetime.now(UTC).isoformat(),
        "source_digest": source_digest, "payload_digest": digest(inventory(workspace)),
    }
    write_atomic(state_path, (json.dumps(state, ensure_ascii=False, indent=4) + "\n").encode())
    print(f"Prepared: {workspace}\nPython: {state['python']}\nLogs and configuration preserved.")


def status(workspace: Path) -> bool:
    state_path = safe_path(workspace, STATE)
    print(f"Source: {ROOT}\nWorkspace: {workspace}\nPython: {os.path.abspath(sys.executable)}")
    runtime_matches = runtime_ready(workspace)
    if not state_path.is_file():
        print("Not prepared (or preparation was interrupted). Run yarn dev:prepare.")
        return False
    state = json.loads(state_path.read_text(encoding="utf-8"))
    ready = (
        runtime_matches
        and state.get("source") == str(ROOT)
        and state.get("python") == os.path.abspath(sys.executable)
        and state.get("source_digest") == digest(inventory(ROOT))
        and state.get("payload_digest") == digest(inventory(workspace))
    )
    print(f"Prepared at: {state.get('prepared_at')}\nState: {'ready' if ready else 'stale; run yarn dev:prepare'}")
    return ready


def main(arguments=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "start", "status"))
    parser.add_argument("--workspace", default=os.environ.get("MDDL_MFAA_ROOT", str(ROOT / "MFAAvalonia")))
    args = parser.parse_args(arguments)
    workspace = Path(args.workspace).absolute()
    try:
        # Reject aliases of source directories before considering any mutation.
        if workspace.resolve() != workspace or workspace == ROOT or ROOT.is_relative_to(workspace):
            raise ValueError("Workspace must be a real, separate MFAA installation directory.")
        if workspace.is_relative_to(ROOT) and workspace.parts[len(ROOT.parts)] != "MFAAvalonia":
            raise ValueError("Inside this repository only MFAAvalonia is a permitted workspace.")
        if args.command == "status":
            ready = status(workspace)
            print(f"Management lock: {workspace_busy(workspace)}")
            return 0 if ready else 1
        gui = executable(workspace)
        with workspace_lock(workspace):
            if args.command == "prepare":
                prepare(workspace)
            else:
                if not status(workspace):
                    return 1
                env = dict(os.environ, MDDL_DEV_MODE="1", MDDL_STATE_ROOT=str(workspace))
                # Keep the lock until GUI exit; do not launch duplicate managed sessions.
                return subprocess.run([str(gui)], cwd=workspace, env=env, check=False).returncode
        return 0
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
