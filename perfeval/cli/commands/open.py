from __future__ import annotations

import argparse
import functools
import http.server
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Sequence


CommandLauncher = Callable[[Sequence[str]], subprocess.Popen[Any]]


def register_parser(subparsers: Any) -> argparse.ArgumentParser:
    parser = subparsers.add_parser("open", help="Open a report artifact with a local viewer.")
    parser.add_argument("path", help="Artifact path, for example artifacts/android/perfeval-diagnose.pftrace.")
    parser.add_argument(
        "--viewer",
        choices=["system", "android-studio", "perfetto-ui", "report"],
        default="system",
        help="Viewer to launch. report serves report.html through localhost; perfetto-ui serves .pftrace and opens ui.perfetto.dev.",
    )
    parser.add_argument(
        "--serve-seconds",
        type=int,
        default=300,
        help="How long to keep the local Perfetto trace server alive.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the open action without launching it.")
    parser.set_defaults(func=handle)
    return parser


def handle(args: argparse.Namespace) -> int:
    path = Path(args.path).expanduser()
    result = open_artifact(
        path,
        viewer=args.viewer,
        serve_seconds=args.serve_seconds,
        dry_run=args.dry_run,
    )
    for line in result:
        print(line)
    return 0


def open_artifact(
    path: Path,
    *,
    viewer: str,
    serve_seconds: int = 300,
    dry_run: bool = False,
    launcher: CommandLauncher = subprocess.Popen,
) -> list[str]:
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"artifact not found: {path}")
    resolved = path.resolve()
    if viewer == "perfetto-ui":
        return _open_perfetto_ui(resolved, serve_seconds=serve_seconds, dry_run=dry_run, launcher=launcher)
    if viewer == "report":
        return _open_report(resolved, serve_seconds=serve_seconds, dry_run=dry_run, launcher=launcher)

    command = _viewer_command(resolved, viewer)
    if not dry_run:
        launcher(command)
    return [f"{'Would open' if dry_run else 'Opened'} {resolved}", f"Command: {_format_command(command)}"]


def _viewer_command(path: Path | str, viewer: str) -> list[str]:
    target = str(path)
    if viewer == "android-studio":
        if sys.platform == "darwin":
            return ["open", "-a", "Android Studio", target]
        studio = shutil.which("android-studio") or shutil.which("studio.sh") or shutil.which("studio")
        if studio:
            return [studio, target]
        return ["android-studio", target]
    if viewer == "system":
        return _system_open_command(target)
    raise ValueError(f"unsupported viewer: {viewer}")


def _system_open_command(target: str) -> list[str]:
    if sys.platform == "darwin":
        return ["open", target]
    if sys.platform.startswith("win"):
        return ["cmd", "/c", "start", "", target]
    opener = shutil.which("xdg-open")
    if opener:
        return [opener, target]
    raise RuntimeError("No system opener found; install xdg-open or choose another viewer.")


def _open_perfetto_ui(
    path: Path,
    *,
    serve_seconds: int,
    dry_run: bool,
    launcher: CommandLauncher,
) -> list[str]:
    basename = path.name
    if dry_run:
        local_url = f"http://127.0.0.1:<port>/{urllib.parse.quote(basename)}"
        ui_url = _perfetto_ui_url(local_url)
        return [
            f"Would serve {path}",
            f"Perfetto UI: {ui_url}",
            f"Command: {_format_command(_system_open_command(ui_url))}",
        ]

    server = _TraceFileServer(path)
    local_url = f"http://127.0.0.1:{server.port}/{urllib.parse.quote(basename)}"
    ui_url = _perfetto_ui_url(local_url)
    launcher(_system_open_command(ui_url))
    print(f"Serving {path} for Perfetto UI at {local_url}")
    print(f"Perfetto UI: {ui_url}")
    print(f"Keep this command running while the trace loads. It will stop after {serve_seconds}s.")
    try:
        server.serve_for(max(1, serve_seconds))
    finally:
        server.close()
    return [f"Stopped Perfetto trace server for {path}"]


def _open_report(
    path: Path,
    *,
    serve_seconds: int,
    dry_run: bool,
    launcher: CommandLauncher,
) -> list[str]:
    if path.suffix.lower() not in {".html", ".htm"}:
        raise ValueError("--viewer report expects an HTML report path")
    root = path.parent.parent if path.parent.name == "report" else path.parent
    relative = path.relative_to(root)
    if dry_run:
        url = f"http://127.0.0.1:<port>/{urllib.parse.quote(str(relative), safe='/')}"
        return [
            f"Would serve {root}",
            f"Report URL: {url}",
            f"Command: {_format_command(_system_open_command(url))}",
        ]

    server = _DirectoryServer(root)
    url = f"http://127.0.0.1:{server.port}/{urllib.parse.quote(str(relative), safe='/')}"
    launcher(_system_open_command(url))
    print(f"Serving {root} at http://127.0.0.1:{server.port}/")
    print(f"Report URL: {url}")
    print("Use the report's Perfetto UI direct-open buttons while this command is running.")
    print(f"The server will stop after {serve_seconds}s.")
    try:
        server.serve_for(max(1, serve_seconds))
    finally:
        server.close()
    return [f"Stopped report server for {path}"]


def _perfetto_ui_url(trace_url: str) -> str:
    return "https://ui.perfetto.dev/#!/?url=" + urllib.parse.quote(trace_url, safe="")


class _TraceFileServer:
    def __init__(self, path: Path) -> None:
        self.path = path
        handler = functools.partial(_TraceFileHandler, trace_path=path)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = int(self.httpd.server_address[1])

    def serve_for(self, seconds: int) -> None:
        deadline = time.monotonic() + seconds
        self.httpd.timeout = 0.5
        while time.monotonic() < deadline:
            self.httpd.handle_request()

    def close(self) -> None:
        self.httpd.server_close()


class _DirectoryServer:
    def __init__(self, root: Path) -> None:
        handler = functools.partial(_DirectoryHandler, directory=str(root))
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = int(self.httpd.server_address[1])

    def serve_for(self, seconds: int) -> None:
        deadline = time.monotonic() + seconds
        self.httpd.timeout = 0.5
        while time.monotonic() < deadline:
            self.httpd.handle_request()

    def close(self) -> None:
        self.httpd.server_close()


class _DirectoryHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "https://ui.perfetto.dev")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Range")
        self.send_header("Cross-Origin-Resource-Policy", "cross-origin")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        return


class _TraceFileHandler(http.server.BaseHTTPRequestHandler):
    server_version = "PerfEvalTraceServer/1.0"

    def __init__(self, *args: Any, trace_path: Path, **kwargs: Any) -> None:
        self.trace_path = trace_path
        super().__init__(*args, **kwargs)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        requested = urllib.parse.unquote(urllib.parse.urlparse(self.path).path.lstrip("/"))
        if requested and requested != self.trace_path.name:
            self.send_error(404, "Trace not found")
            return
        self.send_response(200)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(self.trace_path.stat().st_size))
        self.end_headers()
        with self.trace_path.open("rb") as handle:
            shutil.copyfileobj(handle, self.wfile)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "https://ui.perfetto.dev")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Range")
        self.send_header("Cross-Origin-Resource-Policy", "cross-origin")


def _format_command(command: Sequence[str]) -> str:
    return " ".join(_quote_arg(part) for part in command)


def _quote_arg(value: str) -> str:
    if not value:
        return "''"
    if all(ch.isalnum() or ch in "/._-:=#?&%" for ch in value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"
