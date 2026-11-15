"""`till install` is optional and safe; `till open` tells the truth.

Every test runs with a fake home ($HOME points into tmp_path) and a fake
`systemctl` first on PATH that only records its arguments, so nothing here
can reach the real user's systemd, desktop or ~/.hermes. Listeners that the
installer must (or must not) stop are real processes on real ports.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from till import install
from till.cli import main

REPO = Path(__file__).resolve().parent.parent

FAKE_SYSTEMCTL = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_SYSTEMCTL_LOG"
case "$*" in
  *show-environment*)
    if [ "$FAKE_SYSTEMCTL_BUS" = down ]; then
      echo "Failed to connect to bus: No medium found" >&2
      exit 1
    fi
    echo "HOME=$HOME"; exit 0 ;;
  *MainPID*) echo "${FAKE_MAINPID:-0}"; exit 0 ;;
  *is-active*) exit "${FAKE_ACTIVE:-3}" ;;
esac
exit 0
"""

FAKE_OPENER = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_OPENER_LOG"
exit "${FAKE_OPENER_RC:-0}"
"""

# A stand-in for `till serve`: listens on the port and answers /api/health.
# Its command line names the file it was started as, like a real console script.
FAKE_SERVER = """#!{python}
import http.server, json, sys
port = int(sys.argv[sys.argv.index("--port") + 1])
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({{"ok": True}}).encode()
        self.send_response(200); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _script(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Fake home, fake systemctl and xdg-open first on PATH, a free port."""
    home = tmp_path / "home"
    home.mkdir()
    bindir = tmp_path / "fakebin"
    _script(bindir / "systemctl", FAKE_SYSTEMCTL)
    _script(bindir / "xdg-open", FAKE_OPENER)
    _script(bindir / "update-desktop-database", "#!/bin/sh\nexit 0\n")
    log = tmp_path / "systemctl.log"
    log.write_text("")
    opened = tmp_path / "opener.log"
    opened.write_text("")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_SYSTEMCTL_LOG", str(log))
    monkeypatch.setenv("FAKE_OPENER_LOG", str(opened))
    for name in ("FAKE_SYSTEMCTL_BUS", "FAKE_MAINPID", "FAKE_ACTIVE", "FAKE_OPENER_RC", "TILL_DB"):
        monkeypatch.delenv(name, raising=False)
    port = _free_port()
    monkeypatch.setenv("TILL_PORT", str(port))
    assert shutil.which("systemctl") == str(bindir / "systemctl"), "must never reach the real systemctl"
    assert Path.home() == home
    till = _script(tmp_path / "checkout" / ".venv" / "bin" / "till", FAKE_SERVER.format(python=sys.executable))
    monkeypatch.setattr(install, "default_till", lambda: till)
    monkeypatch.setattr(install, "default_db", lambda: tmp_path / "checkout" / "data" / "till.db")

    class Env:
        pass

    e = Env()
    e.home, e.bindir, e.port, e.till, e.tmp = home, bindir, port, till, tmp_path
    e.workdir = tmp_path / "checkout"
    e.unit = home / ".config/systemd/user/till.service"
    e.desktop = home / ".local/share/applications/till.desktop"
    e.link = home / ".local/bin/till"
    e.calls = lambda: [line for line in log.read_text().splitlines() if line]
    e.opened = lambda: [line for line in opened.read_text().splitlines() if line]
    return e


@pytest.fixture
def healthy(monkeypatch):
    """The service the fake systemctl 'started' answers at once."""
    monkeypatch.setattr(install, "wait_health", lambda port, timeout=8.0: True)


def _snapshot(root: Path) -> dict[str, bytes]:
    skip = {".venv", ".git", "data", "__pycache__", ".pytest_cache", "till.egg-info", ".worktrees"}
    out = {}
    for p in sorted(root.rglob("*")):
        if any(part in skip for part in p.relative_to(root).parts):
            continue
        if p.is_file():
            out[str(p.relative_to(root))] = p.read_bytes()
    return out


def _spawn_listener(script: Path, port: int) -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, str(script), "serve", "--port", str(port)])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return proc
        except OSError:
            time.sleep(0.05)
    proc.kill()
    raise AssertionError("listener did not start")


# --- templates -------------------------------------------------------------


def test_packaging_holds_templates_only():
    names = sorted(p.name for p in (REPO / "packaging").iterdir())
    assert names == ["till.desktop.in", "till.service.in"]
    service = (REPO / "packaging" / "till.service.in").read_text()
    desktop = (REPO / "packaging" / "till.desktop.in").read_text()
    for marker in ("@TILL@", "@DB@", "@WORKDIR@", "@PORT@"):
        assert marker in service
    for marker in ("@TILL@", "@DB@", "@PORT@"):
        assert marker in desktop


def test_unit_and_desktop_render_this_installs_paths_and_port(tmp_path):
    root = tmp_path / "my till"  # a space, so quoting matters
    till = root / ".venv" / "bin" / "till"
    db = root / "data" / "till.db"
    unit = install.unit_text(till=till, db=db, workdir=root, port=7791)
    desktop = install.desktop_text(till=till, db=db, port=7791)
    assert f'ExecStart="{till}" serve --port 7791' in unit
    assert f'Environment="TILL_DB={db}"' in unit
    assert "Environment=TILL_PORT=7791" in unit
    assert f"WorkingDirectory={root}" in unit
    assert "WantedBy=default.target" in unit
    assert f'Exec=env "TILL_DB={db}" "{till}" open --port 7791' in desktop
    assert "Terminal=false" in desktop
    assert "@" not in unit and "@" not in desktop


def test_a_path_with_a_line_break_is_refused(tmp_path):
    with pytest.raises(ValueError):
        install.unit_text(till=tmp_path / "a\nExecStartPre=/bin/evil", db=tmp_path / "d", workdir=tmp_path, port=7740)


def test_no_machine_specific_paths_anywhere_in_the_repo():
    banned = ("/mnt" + "/storage", "/home/" + "wen" + "del", "wen" + "del")
    hits = []
    for name, data in _snapshot(REPO).items():
        if name.startswith("docs/review/") or name.endswith(".png"):
            continue
        text = data.decode("utf-8", "replace")
        hits += [f"{name}: {b}" for b in banned if b in text]
    assert hits == []


# --- install ---------------------------------------------------------------


def test_install_writes_into_home_and_never_into_the_repo(env, healthy, capsys):
    before = _snapshot(REPO)
    workdir_packaging = env.workdir / "packaging"
    workdir_packaging.mkdir(parents=True)
    (workdir_packaging / "till.service").write_text("tracked\n")
    assert main(["install"]) == 0
    assert _snapshot(REPO) == before
    assert (workdir_packaging / "till.service").read_text() == "tracked\n"
    unit = env.unit.read_text()
    assert f"serve --port {env.port}" in unit
    assert f"Environment=TILL_PORT={env.port}" in unit
    assert f"--port {env.port}" in env.desktop.read_text()
    assert env.link.is_symlink() and env.link.resolve() == env.till.resolve()
    calls = env.calls()
    assert "--user daemon-reload" in calls
    assert "--user enable till.service" in calls
    assert "--user restart till.service" in calls
    out = capsys.readouterr().out
    assert f"http://127.0.0.1:{env.port}/" in out
    assert env.opened() == [f"http://127.0.0.1:{env.port}/"]


def test_install_port_option_beats_the_environment(env, healthy):
    other = _free_port()
    assert main(["install", "--port", str(other)]) == 0
    assert f"serve --port {other}" in env.unit.read_text()


def test_install_without_systemctl_says_so_and_writes_nothing(env, monkeypatch, capsys):
    (env.bindir / "systemctl").unlink()
    monkeypatch.setenv("PATH", str(env.bindir))  # no systemctl, no ss
    assert main(["install"]) == 1
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert "Traceback" not in text
    assert "autostart" in text.lower()
    assert "till serve" in text
    assert f"http://127.0.0.1:{env.port}/" in text
    assert list(env.home.iterdir()) == []


def test_install_with_no_user_bus_says_so_and_writes_nothing(env, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_SYSTEMCTL_BUS", "down")
    assert main(["install"]) == 1
    text = "".join(capsys.readouterr())
    assert "autostart" in text.lower() and "till serve" in text
    assert not env.unit.exists()
    assert not env.desktop.exists()
    assert not env.link.exists()
    assert not any("enable" in c or "restart" in c or "stop" in c for c in env.calls())


def test_install_links_the_hermes_skill_only_when_asked(env, healthy):
    skills = env.home / ".hermes" / "skills" / "productivity"
    skills.mkdir(parents=True)
    assert main(["install"]) == 0
    assert list(skills.iterdir()) == []
    assert main(["install", "--hermes"]) == 0
    link = skills / "till"
    assert link.is_symlink()
    assert link.resolve() == (REPO / "skills" / "till").resolve()


def test_install_refuses_to_replace_another_folders_service(env, healthy, capsys):
    env.unit.parent.mkdir(parents=True)
    theirs = "[Service]\nWorkingDirectory=/srv/their-till\nExecStart=/srv/their-till/.venv/bin/till serve\n"
    env.unit.write_text(theirs)
    assert main(["install"]) == 1
    assert env.unit.read_text() == theirs
    assert "/srv/their-till/.venv/bin/till" in "".join(capsys.readouterr())
    assert not any(c.split()[1:2] in (["stop"], ["restart"], ["enable"], ["disable"]) for c in env.calls())
    assert not env.desktop.exists()


def test_install_never_stops_a_till_from_another_folder_on_the_port(env, healthy, capsys):
    other = _script(env.tmp / "their-till" / ".venv" / "bin" / "till", FAKE_SERVER.format(python=sys.executable))
    proc = _spawn_listener(other, env.port)
    try:
        assert main(["install"]) == 1
        time.sleep(0.2)
        assert proc.poll() is None, "another folder's Till was stopped"
        text = "".join(capsys.readouterr())
        assert f"{env.port}" in text and "won't stop" in text
        assert not env.unit.exists()
    finally:
        proc.kill()
        proc.wait()


def test_install_without_ss_refuses_a_busy_port_it_cannot_identify(env, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(env.bindir))  # fake systemctl, but no ss
    other = _script(env.tmp / "their-till" / ".venv" / "bin" / "till", FAKE_SERVER.format(python=sys.executable))
    proc = _spawn_listener(other, env.port)
    try:
        assert main(["install"]) == 1
        assert proc.poll() is None
        assert "Traceback" not in "".join(capsys.readouterr())
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.skipif(shutil.which("ss") is None, reason="needs ss to see who owns the port")
def test_install_stops_a_stray_till_from_this_same_install(env, healthy):
    proc = _spawn_listener(env.till, env.port)
    try:
        assert main(["install"]) == 0
        proc.wait(timeout=5)
        assert "--user restart till.service" in env.calls()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_install_twice_replaces_its_own_unit(env, healthy):
    assert main(["install"]) == 0
    assert main(["install"]) == 0
    assert f"serve --port {env.port}" in env.unit.read_text()


# --- uninstall -------------------------------------------------------------


def test_uninstall_removes_what_install_wrote(env, healthy, capsys):
    db = env.workdir / "data" / "till.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"the books")
    skills = env.home / ".hermes" / "skills" / "productivity"
    skills.mkdir(parents=True)
    assert main(["install", "--hermes"]) == 0
    before = _snapshot(REPO)
    assert main(["uninstall"]) == 0
    assert not env.unit.exists()
    assert not env.desktop.exists()
    assert not env.link.exists() and not env.link.is_symlink()
    assert not (skills / "till").is_symlink()
    assert "--user disable --now till.service" in env.calls()
    assert db.read_bytes() == b"the books"
    assert _snapshot(REPO) == before
    assert str(db) in capsys.readouterr().out


def test_uninstall_leaves_another_folders_service_alone(env, capsys):
    env.unit.parent.mkdir(parents=True)
    theirs = "[Service]\nExecStart=/srv/their-till/.venv/bin/till serve\n"
    env.unit.write_text(theirs)
    assert main(["uninstall"]) == 1
    assert env.unit.read_text() == theirs
    assert not any("disable" in c for c in env.calls())


def test_uninstall_without_systemctl_still_removes_the_files(env, healthy, monkeypatch):
    assert main(["install"]) == 0
    (env.bindir / "systemctl").unlink()
    monkeypatch.setenv("PATH", str(env.bindir))
    assert main(["uninstall"]) == 0
    assert not env.unit.exists() and not env.desktop.exists()


# --- open, doctor, ensure_serving -----------------------------------------


@pytest.fixture
def health_server(env):
    """Something answering /api/health on env.port, like a running Till."""

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", env.port), H)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def test_open_uses_till_port_and_opens_the_browser(env, health_server, capsys):
    assert main(["open"]) == 0
    assert env.opened() == [f"http://127.0.0.1:{env.port}/"]
    assert f"http://127.0.0.1:{env.port}/" in capsys.readouterr().out


def test_open_fails_loudly_when_no_browser_opens(env, health_server, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_OPENER_RC", "3")
    assert main(["open"]) == 1
    text = "".join(capsys.readouterr())
    assert f"http://127.0.0.1:{env.port}/" in text
    assert "browser" in text.lower()


def test_open_without_any_opener_fails_and_prints_the_url(env, health_server, monkeypatch, capsys):
    (env.bindir / "xdg-open").unlink()
    monkeypatch.setenv("PATH", str(env.bindir))
    assert main(["open"]) == 1
    assert f"http://127.0.0.1:{env.port}/" in "".join(capsys.readouterr())


def test_open_fails_when_nothing_serves_and_nothing_can_start(env, monkeypatch, capsys):
    monkeypatch.setattr(install, "default_till", lambda: env.tmp / "no" / "till")
    monkeypatch.setattr(install, "wait_health", lambda port, timeout=8.0: install.health_ok(port))
    assert main(["open"]) == 1
    text = "".join(capsys.readouterr())
    assert "till serve" in text
    assert f"http://127.0.0.1:{env.port}/" in text
    assert env.opened() == []


def test_ensure_serving_starts_its_own_unit_on_the_same_port(env, healthy, monkeypatch):
    assert main(["install"]) == 0
    before = len(env.calls())
    monkeypatch.setattr(install, "health_ok", lambda port: False)
    assert install.ensure_serving(home=env.home, till=env.till, port=env.port) is True
    assert "--user start till.service" in env.calls()[before:]


def test_ensure_serving_does_not_start_another_folders_unit(env, healthy, monkeypatch):
    env.unit.parent.mkdir(parents=True)
    env.unit.write_text(f"[Service]\nExecStart=/srv/their-till/.venv/bin/till serve --port {env.port}\n")
    monkeypatch.setattr(install, "health_ok", lambda port: False)
    monkeypatch.setattr(install, "default_till", lambda: env.tmp / "no" / "till")
    install.ensure_serving(home=env.home, till=env.tmp / "no" / "till", port=env.port)
    assert not any("start" in c for c in env.calls())


def test_ensure_serving_spawns_serve_on_the_port_when_there_is_no_unit(env):
    assert install.ensure_serving(home=env.home, till=env.till, db=env.tmp / "x.db", port=env.port) is True
    assert install.health_ok(env.port)
    pid = install.listener_pid(env.port)
    if pid:
        os.kill(pid, 15)
    else:  # no ss: find it by asking the port to go away
        subprocess.run(["pkill", "-f", str(env.till)], check=False)


def test_doctor_reports_the_configured_port(env, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TILL_DB", str(tmp_path / "doc.db"))
    main(["doctor"])
    assert f"listen    127.0.0.1:{env.port}" in capsys.readouterr().out


def test_a_bad_till_port_is_a_message_not_a_traceback(env, monkeypatch, capsys):
    monkeypatch.setenv("TILL_PORT", "seventy")
    assert main(["open"]) == 2
    assert "TILL_PORT" in capsys.readouterr().err
