#!/usr/bin/env python3
"""Stage the Windows x64 MSYS2 ADB distribution from its locked upstream archives."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil
import struct
import tarfile
import tempfile
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import urlopen


LOCK_PATH = Path(__file__).with_name("adb-lock.json")
REPO = "https://repo.msys2.org/mingw"
PACKAGE_ROOT = "clang64"
PACKAGE_PREFIX = "mingw-w64-clang-x86_64-"


def download(url: str, expected_digest: str, destination: Path) -> None:
    for attempt in range(3):
        try:
            with urlopen(url, timeout=60) as response, destination.open("wb") as downloaded:
                shutil.copyfileobj(response, downloaded)
        except (URLError, TimeoutError):
            if attempt == 2:
                raise
        else:
            break
    with destination.open("rb") as downloaded:
        actual_digest = hashlib.file_digest(downloaded, "sha256").hexdigest()
    if actual_digest != expected_digest:
        raise ValueError(f"SHA-256 mismatch for {url}: expected {expected_digest}; got {actual_digest}")


def extract_files(archive: Path, mode: str, files: dict[str, Path]) -> None:
    remaining = files.copy()
    with tarfile.open(archive, mode) as source:
        for member in source:
            destination = remaining.pop(member.name, None)
            if destination is None:
                continue
            if not member.isfile():
                raise ValueError(f"{archive.name} has a non-regular entry for {member.name}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(member) as stream, destination.open("xb") as staged:
                shutil.copyfileobj(stream, staged)
            if not remaining:
                return
    raise ValueError(f"{archive.name} is missing: {', '.join(sorted(remaining))}")


def verify_x64(binary: Path) -> None:
    with binary.open("rb") as executable:
        dos_header = executable.read(64)
        if len(dos_header) == 64 and dos_header.startswith(b"MZ"):
            executable.seek(struct.unpack_from("<I", dos_header, 60)[0])
            # PE signature followed by the COFF AMD64 machine identifier.
            if executable.read(6) == b"PE\0\0\x64\x86":
                return
    raise ValueError(f"{binary.name} is not a Windows x64 executable")


def stage(output: Path) -> None:
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError(f"Output must be empty or absent: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory(prefix="adb-stage-", dir=output.parent) as temporary:
        workspace = Path(temporary)
        payload = workspace / "platform-tools"
        licenses = payload / "licenses"
        source_package = lock["source"]
        source_archive = workspace / source_package["archive"]

        with ThreadPoolExecutor(max_workers=5) as pool:
            downloads = {}
            for package in lock["packages"]:
                name = f"{PACKAGE_PREFIX}{package['name']}-any.pkg.tar.zst"
                archive = workspace / name
                future = pool.submit(download, f"{REPO}/{PACKAGE_ROOT}/{quote(name)}", package["sha256"], archive)
                downloads[future] = package, archive
            source_download = pool.submit(
                download,
                f"{REPO}/sources/{source_archive.name}",
                source_package["sha256"],
                source_archive,
            )

            for future, (package, archive) in downloads.items():
                future.result()
                files = {f"{PACKAGE_ROOT}/bin/{binary}": payload / binary for binary in package["binaries"]}
                files.update({
                    f"{PACKAGE_ROOT}/share/licenses/{license_name}": licenses / license_name
                    for license_name in package["licenses"]
                })
                extract_files(archive, "r|zst", files)
            source_download.result()

        upstream_source = workspace / "android-tools-source.tar.xz"
        extract_files(source_archive, "r|zst", {source_package["upstream"]: upstream_source})
        extract_files(
            upstream_source,
            "r|xz",
            {source: licenses / destination for source, destination in source_package["licenses"].items()},
        )
        shutil.copyfile(source_archive, licenses / source_archive.name)
        for filename in (
            "ADB-THIRD-PARTY-NOTICES.txt",
            "ADB-LZ4-LICENSE.txt",
            "ADB-PROTOBUF-UTF8-RANGE-LICENSE.txt",
            LOCK_PATH.name,
        ):
            shutil.copyfile(LOCK_PATH.with_name(filename), licenses / filename)

        for binary in payload.iterdir():
            if binary.is_file():
                verify_x64(binary)
        if output.exists():
            output.rmdir()
        payload.rename(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    stage(args.output.resolve())
    print(f"Staged Windows x64 ADB in {args.output}")


if __name__ == "__main__":
    main()
