"""Optional extras: start Till on login (systemd user service), a desktop
launcher, `till open`, and `till uninstall`.

Till itself needs none of this: `till serve` and a browser are enough.

Safety rules this module keeps:
- It writes only into the user's own directories (given as `home`), never
  into the checkout. The unit and launcher are rendered from the templates in
  packaging/.
- Without systemd it says so and writes nothing.
- It never stops a program it can't prove is this same install of Till: a
  listener on the port from another folder, or one it can't identify, is left
  alone and the install is refused. So is a till.service from another folder.
- The Hermes skill is linked only when asked (`--hermes`).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_PORT = 7740
DEMO_PORT = 7749
UNIT_NAME = "till.service"


class SettingError(ValueError):
    """A setting (like TILL_PORT) is unusable; the message says which and why."""


# --- settings ----------------------------------------------------------------


def env_port(name: str = "TILL_PORT", default: int = DEFAULT_PORT) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    if raw.isdigit() and 1 <= int(raw) <= 65535:
        return int(raw)
    raise SettingError(f"{name} must be a port number from 1 to 65535, not {raw!r}")


def configured_port() -> int:
    """The port Till uses everywhere: TILL_PORT, else 7740."""
    return env_port("TILL_PORT", DEFAULT_PORT)


def app_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/"


def health_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/api/health"


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def templates_dir() -> Path:
    return repo_root() / "packaging"


def default_till() -> Path:
    # Not resolved: in a venv, sys.executable is a symlink into the system
    # Python, and the `till` script lives next to the link, not the target.
    bound = Path(sys.executable).parent / "till"
    if bound.exists():
        return bound
    return repo_root() / ".venv" / "bin" / "till"


def default_db() -> Path:
    from till.web import PROJECT

    return Path(os.environ.get("TILL_DB", PROJECT / "data" / "till.db")).resolve()


def _paths(home: Path) -> dict[str, Path]:
    return {
        "unit": home / ".config" / "systemd" / "user" / UNIT_NAME,
        "desktop": home / ".local" / "share" / "applications" / "till.desktop",
        "link": home / ".local" / "bin" / "till",
        "hermes": home / ".hermes" / "skills" / "productivity" / "till",
    }


# --- rendering -----------------------------------------------------------------


def _one_line(value: str) -> str:
    if "\n" in value or "\r" in value or "\0" in value:
        raise ValueError(f"a path with a line break can't go in a service file: {value!r}")
    return value


def _systemd_arg(value: str) -> str:
    """One argument for ExecStart= or Environment=, double-quoted, with %
    doubled so systemd doesn't read it as a specifier."""
    value = _one_line(value).replace("%", "%%")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _desktop_arg(value: str) -> str:
    """One argument for a .desktop Exec= key: quoted per the Desktop Entry
    spec (backslash before " ` $ \\ inside quotes), then the key's own string
    escaping (\\ doubled), and % doubled for field codes."""
    value = _one_line(value)
    quoted = '"' + re.sub(r'([`"$\\])', r"\\\1", value) + '"'
    return quoted.replace("\\", "\\\\").replace("%", "%%")


def render(template: str, values: dict[str, str]) -> str:
    """Fill @KEY@ markers. Comment lines (the template's own notes) go."""
    lines = [line for line in template.splitlines(keepends=True) if not line.startswith("#")]
    text = "".join(lines)
    for key, value in values.items():
        text = text.replace(f"@{key}@", value)
    left = re.findall(r"@[A-Z]+@", text)
    if left:
        raise ValueError(f"template has unfilled values: {', '.join(sorted(set(left)))}")
    return text


def _template(name: str) -> str:
    path = templates_dir() / name
    try:
        return path.read_text()
    except OSError as exc:
        raise SettingError(
            f"can't read the template {path} ({exc.strerror or exc}). "
            "`till install` works from a git clone installed with `pip install -e .`."
        ) from exc


def unit_text(*, till: Path, db: Path, workdir: Path, port: int) -> str:
    return render(
        _template("till.service.in"),
        {
            "WORKDIR": _one_line(str(workdir)).replace("%", "%%"),
            "TILL": _systemd_arg(str(till)),
            "DB": _systemd_arg(f"TILL_DB={db}"),
            "PORT": str(int(port)),
        },
    )


def desktop_text(*, till: Path, db: Path, port: int) -> str:
    return render(
        _template("till.desktop.in"),
        {
            "TILL": _desktop_arg(str(till)),
            "DB": _desktop_arg(f"TILL_DB={db}"),
            "PORT": str(int(port)),
        },
    )


def _unit_exec(unit: Path) -> tuple[Path | None, int]:
    """(the till an installed unit runs, its port) or (None, port) if unreadable."""
    try:
        text = unit.read_text()
    except OSError:
        return None, DEFAULT_PORT
    for line in text.splitlines():
        if line.startswith("ExecStart="):
            cmd = line[len("ExecStart="):].lstrip("-@:+!")
            try:
                argv = shlex.split(cmd.replace("%%", "%"))
            except ValueError:
                return None, DEFAULT_PORT
            if not argv:
                return None, DEFAULT_PORT
            port = DEFAULT_PORT
            if "--port" in argv[:-1]:
                try:
                    port = int(argv[argv.index("--port") + 1])
                except ValueError:
                    pass
            return Path(argv[0]), port
    return None, DEFAULT_PORT


def _same_file(a: Path | str, b: Path | str) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except (OSError, RuntimeError):
        return False


def _unit_is_ours(unit: Path, till: Path) -> bool:
    runs, _ = _unit_exec(unit)
    return runs is not None and _same_file(runs, till)


# --- probes (each copes with the tool being absent) ----------------------------


def _run(argv: list[str], **kw) -> subprocess.CompletedProcess | None:
    """subprocess.run that returns None when the program isn't there."""
    try:
        return subprocess.run(argv, check=False, capture_output=True, text=True, timeout=30, **kw)
    except (OSError, subprocess.SubprocessError):
        return None


def systemd_problem() -> str | None:
    """None if a systemd user manager is reachable, else why not."""
    if not shutil.which("systemctl"):
        return "this computer has no systemctl (no systemd)"
    proc = _run(["systemctl", "--user", "show-environment"])
    if proc is None:
        return "systemctl couldn't be run"
    if proc.returncode != 0:
        reason = (proc.stderr or proc.stdout or "").strip().splitlines()
        return "the systemd user session isn't reachable" + (f" ({reason[0]})" if reason else "")
    return None


def _systemctl(*args: str) -> subprocess.CompletedProcess | None:
    return _run(["systemctl", "--user", *args])


def health_ok(port: int) -> bool:
    try:
        with urllib.request.urlopen(health_url(port), timeout=0.4) as resp:
            if getattr(resp, "status", 200) != 200:
                return False
            body = json.loads(resp.read().decode())
            return bool(body.get("ok"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError):
        return False


def wait_health(port: int, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if health_ok(port):
            return True
        time.sleep(0.1)
    return False


def port_in_use(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def systemd_main_pid() -> int | None:
    proc = _systemctl("show", "-p", "MainPID", "--value", UNIT_NAME)
    if proc is None or proc.returncode != 0:
        return None
    try:
        return int((proc.stdout or "0").strip() or "0") or None
    except ValueError:
        return None


def listener_pid(port: int) -> int | None:
    """The pid listening on 127.0.0.1:port, if `ss` can say (None otherwise)."""
    proc = _run(["ss", "-ltnp", f"sport = :{port}"])
    if proc is None:
        return None
    match = re.search(r"pid=(\d+)", proc.stdout or "")
    return int(match.group(1)) if match else None


def _cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode(errors="replace") for part in raw.split(b"\x00") if part]


def _looks_like_till(pid: int) -> bool:
    return any("till" in part for part in _cmdline(pid))


def _is_this_till(pid: int, till: Path) -> bool:
    """True only when the process was started from this very `till` script."""
    return any(_same_file(part, till) for part in _cmdline(pid)[:3] if part.startswith("/"))


def _stop_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _port_owner(port: int, till: Path) -> tuple[str, int | None, str]:
    """Who is on the port: ("free"|"service"|"stray"|"other", pid, command).

    "service": this install's own systemd service. "stray": a Till started
    from this same `till` script outside systemd (safe to stop). "other":
    anything else, including a listener `ss` can't identify.
    """
    if not port_in_use(port):
        return "free", None, ""
    pid = listener_pid(port)
    if pid is None:
        return "other", None, ""
    command = " ".join(_cmdline(pid))
    if not (_looks_like_till(pid) and _is_this_till(pid, till)):
        return "other", pid, command
    if pid == systemd_main_pid():
        return "service", pid, command
    return "stray", pid, command


# --- serving and opening ---------------------------------------------------------


def ensure_serving(
    *,
    home: Path | None = None,
    till: Path | None = None,
    db: Path | None = None,
    port: int | None = None,
) -> bool:
    """Make sure something answers on the port: this install's own service if
    it has one for this port, else a `till serve` started in the background."""
    port = configured_port() if port is None else port
    if health_ok(port):
        return True
    home = Path(home or Path.home())
    till = Path(till or default_till())
    db = Path(db or default_db())
    unit = _paths(home)["unit"]
    if unit.exists() and _unit_is_ours(unit, till) and _unit_exec(unit)[1] == port and shutil.which("systemctl"):
        _systemctl("start", UNIT_NAME)
        if wait_health(port):
            return True
    if not till.exists() or port_in_use(port):
        return False
    env = os.environ.copy()
    env.setdefault("TILL_DB", str(db))
    env["TILL_PORT"] = str(port)
    try:
        subprocess.Popen(
            [str(till), "serve", "--port", str(port)],
            cwd=str(repo_root()),
            env=env,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return wait_health(port)


def _open_browser(url: str) -> bool:
    """Ask the desktop to open `url`. False if there is no way to, or the
    opener reports failure. An opener still running after a few seconds is
    taken to have started a browser."""
    opener = shutil.which("xdg-open")
    if opener is None and sys.platform == "darwin":
        opener = shutil.which("open")
    if opener is None:
        return False
    try:
        proc = subprocess.Popen([opener, url], start_new_session=True, stdout=subprocess.DEVNULL)
    except OSError:
        return False
    try:
        return proc.wait(timeout=3) == 0
    except subprocess.TimeoutExpired:
        return True


def open_app(*, port: int | None = None, home: Path | None = None) -> int:
    """0 only when Till answers and a browser was asked to show it."""
    port = configured_port() if port is None else port
    url = app_url(port)
    if not ensure_serving(home=home, port=port):
        print(
            f"Till isn't answering on {url} and couldn't be started from here.\n"
            f"Start it with `till serve`, then open {url} in your browser.",
            file=sys.stderr,
        )
        return 1
    print(url)
    if not _open_browser(url):
        print(f"Till is running, but no browser could be opened from here. Open {url} yourself.", file=sys.stderr)
        return 1
    return 0


# --- install / uninstall -----------------------------------------------------------


def _link_hermes_skill(dest: Path, src: Path) -> str:
    if not dest.parent.parent.parent.is_dir():  # ~/.hermes
        return f"Hermes isn't set up here (no {dest.parent.parent.parent}); skipped the skill."
    if dest.is_symlink() and _same_file(dest, src):
        return f"Hermes skill already linked: {dest}"
    if dest.exists() or dest.is_symlink():
        return f"{dest} already exists and isn't this copy's skill; left it alone."
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.symlink_to(src)
    return f"Hermes skill linked: {dest} -> {src}"


def install_app(
    *,
    home: Path | None = None,
    till: Path | None = None,
    db: Path | None = None,
    workdir: Path | None = None,
    port: int | None = None,
    hermes: bool = False,
) -> int:
    """Start Till on login (systemd user service) and add a launcher.

    Returns 0 when the service answers. Returns 1, with a message and
    nothing written, when systemd isn't available or something else already
    holds the service name or the port.
    """
    home = Path(home or Path.home())
    till = Path(till or default_till()).resolve()
    workdir = Path(workdir or repo_root()).resolve()
    db = Path(db or default_db()).resolve()
    port = configured_port() if port is None else port
    url = app_url(port)
    paths = _paths(home)

    problem = systemd_problem()
    if problem:
        print(
            f"Autostart isn't available: {problem}. Nothing was installed.\n"
            f"Till doesn't need it: run `till serve` and open {url} in your browser.",
            file=sys.stderr,
        )
        return 1
    unit_path = paths["unit"]
    if unit_path.exists() and not _unit_is_ours(unit_path, till):
        runs, _ = _unit_exec(unit_path)
        print(
            f"A Till service from another copy is already installed: {unit_path} runs {runs or 'something else'}.\n"
            "Nothing was changed. Run `till uninstall` from that copy first, or keep using it.",
            file=sys.stderr,
        )
        return 1
    owner, pid, command = _port_owner(port, till)
    if owner == "other":
        who = f"pid {pid}: {command}" if pid else "a program this user can't identify"
        print(
            f"Port {port} is already in use ({who}). Till won't stop it, so nothing was installed.\n"
            f"Stop it yourself, or install on another port: till install --port {port + 1}",
            file=sys.stderr,
        )
        return 1
    try:
        unit = unit_text(till=till, db=db, workdir=workdir, port=port)
        desktop = desktop_text(till=till, db=db, port=port)
    except ValueError as exc:
        print(f"Can't install: {exc}", file=sys.stderr)
        return 1

    for key in ("unit", "desktop", "link"):
        paths[key].parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(unit)
    paths["desktop"].write_text(desktop)
    link = paths["link"]
    if link.is_symlink() or not link.exists():
        if link.is_symlink():
            link.unlink()
        link.symlink_to(till)
    elif not _same_file(link, till):
        print(f"{link} already exists and isn't a link; left it alone.")
    if hermes:
        print(_link_hermes_skill(paths["hermes"], workdir / "skills" / "till"))

    _systemctl("daemon-reload")
    if owner == "stray":
        _stop_pid(pid)
    for args in (("enable", UNIT_NAME), ("restart", UNIT_NAME)):
        proc = _systemctl(*args)
        if proc is None or proc.returncode != 0:
            detail = ((proc.stderr or proc.stdout).strip() if proc else "") or "no output"
            print(f"systemctl --user {' '.join(args)} failed: {detail}", file=sys.stderr)
            return 1
    updater = shutil.which("update-desktop-database")
    if updater:
        _run([updater, str(paths["desktop"].parent)])
    if not wait_health(port):
        print(
            f"Till is installed but isn't answering on {url}.\n"
            f"See `systemctl --user status {UNIT_NAME}` and `journalctl --user -u {UNIT_NAME}`.",
            file=sys.stderr,
        )
        return 1
    print(f"Till starts on login now. Click Till in your app launcher, or open {url}")
    print(f"Books: {db}. To undo: till uninstall")
    return 0


def uninstall_app(*, home: Path | None = None, till: Path | None = None, db: Path | None = None) -> int:
    """Remove what install wrote for this copy. Never touches the books, and
    leaves anything another copy installed alone (exit 1 if there was any)."""
    home = Path(home or Path.home())
    till = Path(till or default_till()).resolve()
    db = Path(db or default_db()).resolve()
    paths = _paths(home)
    removed, kept = [], []

    unit = paths["unit"]
    if unit.exists():
        if _unit_is_ours(unit, till):
            if shutil.which("systemctl"):
                _systemctl("disable", "--now", UNIT_NAME)
            unit.unlink()
            removed.append(unit)
            if shutil.which("systemctl"):
                _systemctl("daemon-reload")
        else:
            runs, _ = _unit_exec(unit)
            kept.append(f"{unit} runs {runs or 'something else'}, not this copy")

    desktop = paths["desktop"]
    if desktop.exists():
        text = desktop.read_text(errors="replace")
        if str(till) in text.replace("\\\\", "\\"):
            desktop.unlink()
            removed.append(desktop)
            updater = shutil.which("update-desktop-database")
            if updater:
                _run([updater, str(desktop.parent)])
        else:
            kept.append(f"{desktop} belongs to another copy")

    for key, target in (("link", till), ("hermes", repo_root() / "skills" / "till")):
        path = paths[key]
        if path.is_symlink() and _same_file(path, target):
            path.unlink()
            removed.append(path)

    for path in removed:
        print(f"removed {path}")
    if not removed and not kept:
        print("Nothing to remove: Till wasn't installed for this user.")
    print(f"Your books are untouched: {db}")
    for note in kept:
        print(f"left alone: {note}", file=sys.stderr)
    return 1 if kept else 0
