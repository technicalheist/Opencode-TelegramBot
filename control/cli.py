"""Command line interface for the opencode-voice control plane.

The same entry point is used by the packaged binary's own child processes:
``bot``, ``mcp-server`` and ``send-media`` re-enter this dispatcher instead of
running Python scripts that do not exist inside a onefile bundle.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import webbrowser
from pathlib import Path
from typing import Sequence

from control import __version__, api, paths, service, settings
from control.manager import PARENT_PID_ENV, Manager, ManagerError

SUBCOMMANDS = (
    "serve",
    "bot",
    "mcp-server",
    "send-media",
    "status",
    "config",
    "logs",
    "service",
    "open",
    "version",
)


def _bootstrap() -> None:
    """Point every child process at the panel's settings before config loads."""
    target = paths.env_file()
    os.environ.setdefault("OCV_ENV_FILE", str(target))
    os.environ.setdefault("OCV_CONFIG_DIR", str(paths.config_dir()))
    if target != paths.REPO_ROOT / ".env":
        # Installed package: relative DB/media paths belong to the app dir,
        # not to site-packages.
        os.environ.setdefault("OCV_HOME", str(paths.config_dir()))


def _format_uptime(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _status_lines(snapshot: dict) -> list[str]:
    bot = snapshot.get("bot", {})
    opencode = snapshot.get("opencode", {})
    version = f" v{opencode['version']}" if opencode.get("version") else ""
    width = 10
    lines = [
        f"{'opencode'.ljust(width)} {opencode.get('status', 'unknown')}{version}"
        f"  {opencode.get('url', '')}".rstrip(),
        f"{'bot'.ljust(width)} {bot.get('status', 'unknown')}"
        f"  pid {bot.get('pid') or '-'}  up {_format_uptime(bot.get('uptime'))}",
        f"{'mode'.ljust(width)} {snapshot.get('mode', 'polling')}",
        f"{'settings'.ljust(width)} {snapshot.get('env_file', '')}",
    ]
    for key in ("opencode", "bot"):
        detail = snapshot.get(key, {}).get("detail")
        if detail:
            lines.append(f"{'':{width}} {key}: {detail}")
    return lines


def _cmd_serve(args: argparse.Namespace) -> int:
    paths.ensure_dirs()
    return api.run(port=args.port, host=args.host, open_browser=not args.no_open)


def _watch_parent() -> None:
    """Exit when the panel that spawned us disappears (even on a hard kill)."""
    raw = os.environ.get(PARENT_PID_ENV, "").strip()
    if not raw.isdigit():
        return
    parent = int(raw)

    def loop() -> None:
        import psutil

        while True:
            try:
                alive = psutil.Process(parent).is_running()
            except Exception:
                alive = False
            if not alive:
                os._exit(0)
            time.sleep(2.0)

    import threading

    threading.Thread(target=loop, name="parent-watchdog", daemon=True).start()


def _cmd_bot(_: argparse.Namespace) -> int:
    _watch_parent()
    from telegram_bot import launcher

    return int(launcher.main() or 0)


def _cmd_mcp_server(_: argparse.Namespace) -> int:
    from telegram_bot import mcp_server

    mcp_server.run()
    return 0


def _cmd_send_media(args: argparse.Namespace) -> int:
    from telegram_bot import send_media

    argv = list(args.media_args)
    if argv and argv[0] == "--":
        argv = argv[1:]
    return int(send_media.main(argv) or 0)


def _cmd_status(args: argparse.Namespace) -> int:
    snapshot = Manager().snapshot()
    if args.json:
        print(json.dumps(snapshot, indent=2, default=str))
        return 0
    for line in _status_lines(snapshot):
        print(line)
    return 0


def _masked_values() -> dict[str, str]:
    payload = settings.payload()
    values = dict(payload["values"])
    for key, stored in payload["secrets_set"].items():
        values[key] = "********" if stored else ""
    return values


def _cmd_config(args: argparse.Namespace) -> int:
    action = args.action
    if action == "path":
        print(paths.env_file())
        return 0
    if action == "list":
        values = _masked_values()
        group = None
        for field in settings.FIELDS:
            if field.group != group:
                group = field.group
                print(f"[{group}]")
            print(f"{field.key}={values.get(field.key, '')}")
        return 0
    if action == "get":
        values = settings.read_env()
        merged = {**settings.defaults(), **values}
        for key in args.keys:
            if key not in settings.FIELDS_BY_KEY:
                print(f"Unknown setting: {key}", file=sys.stderr)
                return 1
            print(merged.get(key, ""))
        return 0
    if action == "set":
        changes = {}
        unknown = []
        for item in args.keys:
            if "=" not in item:
                print(f"Expected KEY=VALUE, got {item!r}", file=sys.stderr)
                return 1
            key, value = item.split("=", 1)
            key = key.strip()
            if key not in settings.FIELDS_BY_KEY:
                unknown.append(key)
                continue
            changes[key] = value
        if unknown:
            for key in unknown:
                print(f"Unknown setting: {key}", file=sys.stderr)
            return 1
        try:
            settings.save(changes)
        except ValueError as exc:
            for line in str(exc).splitlines():
                print(line, file=sys.stderr)
            return 1
        print(f"Saved {len(changes)} setting(s) to {paths.env_file()}")
        return 0
    if action == "import":
        source = Path(args.file).expanduser()
        imported = settings.read_env(source)
        if not imported:
            print(f"No settings found in {source}", file=sys.stderr)
            return 1
        merged = {**settings.defaults(), **settings.read_env(), **imported}
        errors, _ = settings.validate(merged)
        if errors:
            for line in errors:
                print(line, file=sys.stderr)
            return 1
        settings.write_env(merged)
        print(f"Imported {len(imported)} setting(s) into {paths.env_file()}")
        return 0
    print(f"Unknown config action: {action}", file=sys.stderr)
    return 1


def _follow(path: Path) -> int:  # pragma: no cover - interactive
    try:
        offset = path.stat().st_size
    except OSError:
        offset = 0
    try:
        while True:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            if size < offset:
                offset = 0
            if size > offset:
                with path.open("r", encoding="utf-8", errors="replace") as stream:
                    stream.seek(offset)
                    data = stream.read()
                    offset = stream.tell()
                for line in data.splitlines():
                    print(line)
                sys.stdout.flush()
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0


def _cmd_logs(args: argparse.Namespace) -> int:
    target = paths.panel_log() if args.source == "panel" else paths.bot_log()
    if args.follow:
        return _follow(target)
    return _print_file(target, args.lines)


def _print_file(path: Path, limit: int) -> int:
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - 256 * 1024))
            data = stream.read().decode("utf-8", errors="replace")
    except OSError:
        return 0
    for line in data.splitlines()[-limit:]:
        print(line)
    return 0


def _cmd_service(args: argparse.Namespace) -> int:
    action = args.action
    if action == "install":
        result = service.install(port=args.port or paths.default_port())
    elif action == "status":
        result = service.status()
    else:
        result = getattr(service, action)()
    print(result.detail)
    return 0 if result.ok else 1


def _cmd_open(_: argparse.Namespace) -> int:
    port = paths.default_port()
    url = f"http://127.0.0.1:{port}"
    if not api._panel_running(url):
        print("Panel is not running. Start it with: ocv serve", file=sys.stderr)
        return 1
    token = api.admin_token(create=False)
    webbrowser.open(f"{url}/?t={token}" if token else url)
    return 0


def _cmd_version(_: argparse.Namespace) -> int:
    print(__version__)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opencode-voice",
        description="Local control panel for the opencode-voice Telegram bot.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the panel (default)")
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--no-open", action="store_true", help="do not open a browser")
    serve.set_defaults(func=_cmd_serve)

    bot = sub.add_parser("bot", help="run the Telegram bot (internal)")
    bot.set_defaults(func=_cmd_bot)

    mcp = sub.add_parser("mcp-server", help="run the MCP server (internal)")
    mcp.set_defaults(func=_cmd_mcp_server)

    media = sub.add_parser("send-media", help="send a file to Telegram (internal)")
    media.add_argument("media_args", nargs=argparse.REMAINDER)
    media.set_defaults(func=_cmd_send_media)

    status = sub.add_parser("status", help="show opencode and bot status")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=_cmd_status)

    cfg = sub.add_parser("config", help="read or write settings")
    cfg_sub = cfg.add_subparsers(dest="action", required=True)
    cfg_sub.add_parser("list")
    cfg_sub.add_parser("path")
    cfg_get = cfg_sub.add_parser("get")
    cfg_get.add_argument("keys", nargs="+")
    cfg_set = cfg_sub.add_parser("set")
    cfg_set.add_argument("keys", nargs="+", metavar="KEY=VALUE")
    cfg_import = cfg_sub.add_parser("import")
    cfg_import.add_argument("file")
    cfg.set_defaults(func=_cmd_config)

    logs = sub.add_parser("logs", help="print or follow logs")
    logs.add_argument("source", nargs="?", default="bot", choices=("bot", "panel"))
    logs.add_argument("-n", "--lines", type=int, default=200)
    logs.add_argument("-f", "--follow", action="store_true")
    logs.set_defaults(func=_cmd_logs)

    svc = sub.add_parser("service", help="install or control the background service")
    svc_sub = svc.add_subparsers(dest="action", required=True)
    svc_install = svc_sub.add_parser("install")
    svc_install.add_argument("--port", type=int, default=None)
    for name in ("uninstall", "start", "stop", "restart", "status"):
        svc_sub.add_parser(name)
    svc.set_defaults(func=_cmd_service)

    opener = sub.add_parser("open", help="open the running panel in a browser")
    opener.set_defaults(func=_cmd_open)

    version = sub.add_parser("version")
    version.set_defaults(func=_cmd_version)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    help_flags = {"-h", "--help", "--version", "-v"}
    if not args_list:
        args_list = ["serve"]
    elif (
        args_list[0] not in SUBCOMMANDS
        and args_list[0] not in help_flags
        and args_list[0].startswith("-")
    ):
        args_list.insert(0, "serve")
    parser = build_parser()
    args = parser.parse_args(args_list)
    _bootstrap()
    try:
        return int(args.func(args))
    except ManagerError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
