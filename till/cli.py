"""till serve | demo | open | brief | tick | backup | restore | doctor | install | uninstall | seed | telegram-brief."""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from till.install import (
    DEMO_PORT,
    SettingError,
    app_url,
    configured_port,
    env_port,
    health_ok,
    install_app,
    open_app,
    uninstall_app,
)
from till.money import format_cents
from till.seed import book_kind, remove_book, seed
from till.store import Store
from till.web import DEFAULT_DB, PROJECT, create_app


def _store() -> Store:
    store = Store(os.environ.get("TILL_DB", DEFAULT_DB))
    store.migrate()
    return store


def _db_path() -> Path:
    return Path(os.environ.get("TILL_DB", DEFAULT_DB))


def _port() -> int:
    return configured_port()


def _demo_db_path() -> Path:
    """The sample book: $TILL_DEMO_DB, else data/demo.db. Never the real book."""
    return Path(os.environ.get("TILL_DEMO_DB", PROJECT / "data" / "demo.db"))


def _refuse_sample_data(path: Path, kind: str) -> int:
    what = "isn't a Till book" if kind == "foreign" else "has your own data in it"
    print(
        f"{path} {what}, so Till won't put sample data there (not even with --force).\n"
        "To try Till with sample data, run `till demo`: it uses a separate book.\n"
        "If you really mean to replace this file, run `till backup` first, then move it away yourself.",
        file=sys.stderr,
    )
    return 1


def _seed_fresh(path: Path) -> Store:
    remove_book(path)
    store = Store(path)
    store.migrate()
    seed(store)
    return store


def _seed_cmd(force: bool) -> int:
    """Compatibility: `till seed` writes sample data into TILL_DB, but only
    into a missing or empty book, or (with --force) over a sample book."""
    path = _db_path()
    kind = book_kind(path)
    if kind in ("real", "foreign"):
        return _refuse_sample_data(path, kind)
    if kind == "demo" and not force:
        print(f"{path} is already a sample book. Pass --force to rebuild it.")
        return 0
    store = _seed_fresh(path)
    print(f"seeded sample data into {path}")
    print("To try Till without touching your own book, use `till demo` instead.")
    print(store.brief_text())
    store.close()
    return 0


def _demo_cmd(port: int | None, fresh: bool) -> int:
    path = _demo_db_path()
    port = env_port("TILL_DEMO_PORT", DEMO_PORT) if port is None else port
    if path.resolve() == _db_path().resolve():
        print(
            f"The sample book ({path}) would be the same file as your own book (TILL_DB). "
            "Point TILL_DEMO_DB somewhere else.",
            file=sys.stderr,
        )
        return 1
    kind = book_kind(path)
    if kind in ("real", "foreign"):
        return _refuse_sample_data(path, kind)
    # Checked before seeding: a busy port may be a demo server that still has
    # this book open, and --fresh must not delete a book out from under it.
    if _port_in_use(port):
        print(
            f"Port {port} is busy (a demo already running? It's at {app_url(port)}). "
            "Set TILL_DEMO_PORT or pass --port to use another.",
            file=sys.stderr,
        )
        return 1
    if kind == "demo" and not fresh:
        print(f"Using the sample book at {path} (pass --fresh to rebuild it).", flush=True)
    else:
        _seed_fresh(path).close()
        print(f"Made a sample book at {path}.", flush=True)
    import uvicorn

    print(f"Sample business: open {app_url(port)}  (Ctrl+C stops it; your own book isn't touched)", flush=True)
    uvicorn.run(create_app(path), host="127.0.0.1", port=port, log_level="warning")
    return 0


def _backup_name(db: Path, tag: str = "") -> Path:
    """data/backups/till-YYYYMMDD-HHMMSS.db next to the book (till-<tag>-... with
    a tag), never an existing file."""
    folder = db.parent / "backups"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    stem = f"{db.stem}-{tag}" if tag else db.stem
    path = folder / f"{stem}-{stamp}.db"
    n = 1
    while path.exists() or Path(f"{path}-wal").exists() or Path(f"{path}-shm").exists():
        path = folder / f"{stem}-{stamp}-{n}.db"
        n += 1
    return path


def _copy_book(src: Path, dst: Path) -> None:
    """Copy one SQLite book onto another with SQLite's online backup API.

    It reads a consistent snapshot of the last committed state, WAL included,
    so it is safe while the server is running and writing. Writing into the
    destination through SQLite (not over the file) keeps its WAL and -shm
    consistent too. mode=rw: a missing source is an error, never a new file.
    """
    source = sqlite3.connect(f"{src.resolve().as_uri()}?mode=rw", uri=True)
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(dst)
        try:
            target.execute("PRAGMA busy_timeout = 5000")
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def _check_book(path: Path) -> str | None:
    """None if `path` is an intact Till book, else what is wrong with it."""
    try:
        con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            result = [r[0] for r in con.execute("PRAGMA integrity_check")]
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
    except sqlite3.Error as exc:
        return f"it isn't a readable SQLite database ({exc})"
    if result != ["ok"]:
        return "integrity check failed: " + "; ".join(result[:5])
    if not {"invoices", "journal", "journal_lines", "customers"} <= tables:
        return "it isn't a Till book (no invoices or journal tables)"
    return None


def _port_in_use(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


BOOK_FILES = ("", "-wal", "-shm", "-journal")


def _book_files(db: Path) -> list[Path]:
    """The book and whichever of its -wal, -shm and -journal exist."""
    return [p for p in (Path(f"{db}{s}") for s in BOOK_FILES) if os.path.lexists(p)]


def _unfinished_write(db: Path) -> bool:
    """True if a rollback journal that SQLite would play back sits beside the
    book: a writer is in the middle of a transaction, or one stopped part way.
    Opening the book through SQLite would roll it back into the file, so the
    in-use check and the copy below leave such a book alone."""
    try:
        with open(f"{db}-journal", "rb") as f:
            return f.read(1) not in (b"", b"\0")
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _open_alone(path: Path, timeout: float) -> sqlite3.Connection:
    """A connection that takes the book for itself (locking_mode EXCLUSIVE)
    and writes nothing back when it closes.

    With EXCLUSIVE locking, SQLite can't take a WAL book while any other
    connection has it open, even one sitting idle (a running Till), and it
    reads the WAL without touching the -shm. NO_CKPT_ON_CLOSE stops the close
    from checkpointing the WAL into the book and deleting it."""
    con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=rw", uri=True, timeout=timeout, isolation_level=None)
    try:
        if hasattr(con, "setconfig"):  # Python 3.12+
            con.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
        con.execute("PRAGMA locking_mode = EXCLUSIVE")
        con.execute("BEGIN EXCLUSIVE")
        con.execute("ROLLBACK")
    except BaseException:
        con.close()
        raise
    return con


def _book_in_use(db: Path) -> tuple[str, str]:
    """("free", ""), ("in use", ""), or ("can't tell", why).

    It asks SQLite for the book to itself, with a short wait, and gives it
    straight back. Anything SQLite can't open or read is "can't tell": this
    check never judges the book, it only asks whether something holds it."""
    if not db.is_file():
        return "free", ""
    if _unfinished_write(db):
        return "can't tell", (
            f"there's an unfinished write beside it ({db.name}-journal): a program is writing the book now, "
            "or one stopped part way through, and Till can't tell which without touching the book"
        )
    try:
        _open_alone(db, 0.2).close()
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc) or "busy" in str(exc):
            return "in use", ""
        return "can't tell", f"SQLite can't open it ({exc})"
    except sqlite3.Error as exc:
        return "can't tell", f"SQLite can't open it ({exc})"
    return "free", ""


def _save_before(db: Path, dst: Path) -> str | None:
    """Best effort: a consistent copy of the current book at `dst`, through
    SQLite's backup API, without writing to the book's own files. None when
    it worked, else why not (and no copy is left behind)."""
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=dst.parent, prefix=".restore-") as tmp:
            if _unfinished_write(db):
                # Let SQLite finish the recovery on a copy, not on the book.
                src = Path(tmp) / db.name
                shutil.copyfile(db, src)
                shutil.copyfile(f"{db}-journal", f"{src}-journal")
                source = sqlite3.connect(src, timeout=5)
            else:
                try:
                    source = _open_alone(db, 0.2)
                except sqlite3.OperationalError:
                    # Something has it open (--force): read a snapshot alongside it.
                    source = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
            try:
                target = sqlite3.connect(dst)
                try:
                    source.backup(target)
                finally:
                    target.close()
            finally:
                source.close()
        con = sqlite3.connect(f"{dst.resolve().as_uri()}?mode=ro", uri=True)
        try:
            result = [r[0] for r in con.execute("PRAGMA integrity_check")]
        finally:
            con.close()
        if result != ["ok"]:
            raise sqlite3.DatabaseError("; ".join(result[:3]))
    except (sqlite3.Error, OSError) as exc:
        for p in _book_files(dst):
            p.unlink(missing_ok=True)
        return str(exc)
    return None


def _move_files(files: list[Path], folder: Path) -> list[tuple[Path, Path]]:
    """Move each file into `folder` under its own name (os.replace, the same
    filesystem, so nothing is copied or rewritten). All or none: if one
    can't be moved, the ones already moved go back and the error is raised."""
    moved: list[tuple[Path, Path]] = []
    try:
        for p in files:
            to = folder / p.name
            os.replace(p, to)
            moved.append((p, to))
    except OSError:
        _move_back(moved)
        raise
    return moved


def _move_back(moved: list[tuple[Path, Path]]) -> None:
    for back, there in reversed(moved):
        os.replace(there, back)


def _backup(target: str | None) -> int:
    db = _db_path()
    if not db.is_file():
        print(f"no book at {db}", file=sys.stderr)
        return 1
    dst = Path(target) if target else _backup_name(db)
    if dst.exists():
        print(f"{dst} already exists; choose another path", file=sys.stderr)
        return 1
    try:
        _copy_book(db, dst)
    except sqlite3.Error as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        return 1
    problem = _check_book(dst)
    if problem:
        print(f"backup written to {dst} but it doesn't check out: {problem}", file=sys.stderr)
        return 1
    print(dst)
    return 0


def _restore_names(db: Path) -> tuple[Path, Path]:
    """backups/<stem>-before-restore-<time>.db and backups/<stem>-replaced-<time>/,
    neither of which exists yet."""
    folder = db.parent / "backups"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    n = 0
    while True:
        tag = f"{stamp}-{n}" if n else stamp
        copy, replaced = folder / f"{db.stem}-before-restore-{tag}.db", folder / f"{db.stem}-replaced-{tag}"
        if not _book_files(copy) and not os.path.lexists(replaced):
            return copy, replaced
        n += 1


def _restore(source: str, force: bool) -> int:
    """Put a backup in place of the book. It never judges the current book and
    never deletes or rewrites a byte of it: its files are moved, as they are,
    into the backups folder, after a best-effort copy of it is saved there."""
    db, src = _db_path(), Path(source)
    if not src.is_file():
        print(f"no file at {src}", file=sys.stderr)
        return 1
    # Before anything opens the source: opening the book itself would touch it.
    if db.is_file() and src.resolve() == db.resolve():
        print(f"{src} is the book itself; restore a backup of it instead", file=sys.stderr)
        return 1
    problem = _check_book(src)
    if problem:
        print(f"won't restore {src}: {problem}", file=sys.stderr)
        return 1
    port = _port()
    running = _port_in_use(port)
    state, why = _book_in_use(db)
    if not force:
        if running:
            print(
                f"Till looks like it's running on port {port}. Stop it first, or pass --force to restore anyway.",
                file=sys.stderr,
            )
            return 1
        if state == "in use":
            print(
                f"{db} is in use by another program (a Till still running?). "
                "Stop it first, or pass --force to restore anyway.",
                file=sys.stderr,
            )
            return 1
        if state == "can't tell":
            print(
                f"Till can't tell whether {db} is in use: {why}. Stop anything that might be using it, then pass "
                "--force to restore anyway. Nothing of the current book is deleted: its files are moved, as they "
                "are, into the backups folder.",
                file=sys.stderr,
            )
            return 1
    copy, replaced = _restore_names(db)
    if db.is_file():
        failed = _save_before(db, copy)
        if failed:
            print(f"couldn't make a copy of the current book first ({failed}); its files are still moved aside below")
        else:
            print(f"current book saved to {copy}")
    moved: list[tuple[Path, Path]] = []
    current = _book_files(db)
    if current:
        try:
            replaced.mkdir(parents=True)
            moved = _move_files(current, replaced)
        except OSError as exc:
            if replaced.is_dir() and not any(replaced.iterdir()):
                replaced.rmdir()
            print(f"couldn't move the current book aside, so nothing was restored: {exc}", file=sys.stderr)
            return 1
        print(f"the current book's files are kept, as they were, in {replaced}")
    interrupted: BaseException | None = None
    try:
        _copy_book(src, db)
        problem = _check_book(db)
    except Exception as exc:  # any failure here must put the current book back
        problem = str(exc) or type(exc).__name__
    except BaseException as exc:  # Ctrl-C: put the current book back, then stop
        problem, interrupted = "interrupted", exc
    if problem:
        try:
            for p in _book_files(db):
                p.unlink()
            _move_back(moved)
            if moved:
                replaced.rmdir()
        except OSError as exc:
            print(
                f"restore failed ({problem}), and the current book couldn't be put back ({exc}). "
                f"Its files are in {replaced}.",
                file=sys.stderr,
            )
            if interrupted:
                raise interrupted
            return 1
        print(f"restore failed ({problem}); the current book was put back as it was.", file=sys.stderr)
        if interrupted:
            raise interrupted
        return 1
    print(f"restored {src} to {db}")
    if running:
        print(f"Till is running on port {port}: restart it so it opens the restored book fresh.")
    elif state != "free":
        print("Something may have had the book open: restart it so it opens the restored book fresh.")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except SettingError as exc:
        print(exc, file=sys.stderr)
        return 2


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="till", description="Local small-business daybook.")
    sub = parser.add_subparsers(dest="cmd")
    serve = sub.add_parser("serve", help="Run the local web app (port: --port or TILL_PORT, default 7740)")
    serve.add_argument("--port", type=int)
    demo_p = sub.add_parser("demo", help="Try Till on a sample business, in its own book and on its own port")
    demo_p.add_argument("--port", type=int, help="default: TILL_DEMO_PORT, else 7749")
    demo_p.add_argument("--fresh", action="store_true", help="rebuild the sample book first")
    seed_p = sub.add_parser("seed", help="Put sample data into an empty book (use `till demo` instead)")
    seed_p.add_argument("--force", action="store_true", help="rebuild a sample book; never touches real data")
    sub.add_parser("brief", help="Print today's brief")
    sub.add_parser("telegram-brief", help="Optional: send today's brief through the Hermes assistant")
    sub.add_parser("tick", help="Issue draft invoices for due retainers")
    backup_p = sub.add_parser("backup", help="Copy the book safely, even while Till runs")
    backup_p.add_argument("path", nargs="?", help="where to write it (default: data/backups/till-<time>.db)")
    restore_p = sub.add_parser("restore", help="Replace the book with a backup (the current one is backed up first)")
    restore_p.add_argument("path")
    restore_p.add_argument(
        "--force", action="store_true", help="restore even though the book seems to be in use, or Till can't tell"
    )
    sub.add_parser("doctor", help="Check the database and listener")
    open_p = sub.add_parser("open", help="Start Till if needed and open it in the browser")
    open_p.add_argument("--port", type=int)
    install_p = sub.add_parser(
        "install",
        help="Optional, Linux with systemd: start Till on login and add a launcher",
    )
    install_p.add_argument("--port", type=int)
    install_p.add_argument("--hermes", action="store_true", help="also link the skill for the Hermes assistant")
    sub.add_parser("uninstall", help="Undo `till install` (the books stay)")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    cmd = args.cmd or "serve"
    port = getattr(args, "port", None)
    if port is not None and not 1 <= port <= 65535:
        raise SettingError(f"--port must be from 1 to 65535, not {port}")

    if cmd == "serve":
        import uvicorn

        os.environ.setdefault("TILL_DB", str(DEFAULT_DB))
        uvicorn.run(
            create_app,
            factory=True,
            host="127.0.0.1",
            port=port if port is not None else _port(),
            log_level="info",
        )
        return 0

    if cmd == "demo":
        return _demo_cmd(port, args.fresh)

    if cmd == "seed":
        return _seed_cmd(args.force)

    if cmd == "brief":
        print(_store().brief_text())
        return 0

    if cmd == "tick":
        try:
            result = _store().tick_recurring()
        except ValueError as exc:
            print(f"tick stopped: {exc}", file=sys.stderr)
            return 1
        created, failed = result["created"], result["failed"]
        if not created and not failed:
            print("no retainers due")
            return 0
        for inv in created:
            print(f"{inv['number']} {inv['customer_name']} draft {format_cents(inv['total_cents'], inv['currency'])}")
        for f in failed:
            who = f["customer_name"] or "no customer"
            print(f"retainer {f['recurring_id']} ({who}) not issued: {f['error']}", file=sys.stderr)
        return 1 if failed else 0

    if cmd == "backup":
        return _backup(args.path)

    if cmd == "restore":
        return _restore(args.path, args.force)

    if cmd == "telegram-brief":
        text = _store().brief_text()
        hermes = shutil.which("hermes")
        if not hermes:
            print("hermes is not on PATH", file=sys.stderr)
            return 1
        proc = subprocess.run([hermes, "send", "--to", "telegram", text], check=False)
        return proc.returncode

    if cmd == "open":
        return open_app(port=port if port is not None else _port())

    if cmd == "install":
        port = port if port is not None else _port()
        rc = install_app(port=port, hermes=args.hermes)
        if rc != 0:
            return rc
        return open_app(port=port)

    if cmd == "uninstall":
        return uninstall_app()

    if cmd == "doctor":
        store = _store()
        company = store.get_company()
        print(f"db        {store.path}")
        print(f"company   {company['name']}")
        print(f"balanced  {store.journal_is_balanced()}")
        print(f"cash      {store.cash_cents()}")
        port = _port()
        print(f"listen    127.0.0.1:{port} ({'answering' if health_ok(port) else 'not answering'})")
        fk = store.db.execute("PRAGMA foreign_keys").fetchone()[0]
        print(f"fk        {fk}")
        return 0 if store.journal_is_balanced() and fk == 1 else 1

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
