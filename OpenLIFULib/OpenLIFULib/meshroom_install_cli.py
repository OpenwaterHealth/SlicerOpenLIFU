import argparse
import json
import os
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

PROGRESS_LINE_PREFIX = "OPENLIFU_MESHROOM_INSTALL_PROGRESS "
BYTES_PER_MB = 1024 * 1024
DOWNLOAD_SOCKET_TIMEOUT_SECONDS = 45


class MeshroomInstallCanceled(Exception):
    pass


def _emit_progress_event(event: dict) -> None:
    print(PROGRESS_LINE_PREFIX + json.dumps(event), flush=True)


def _progress_callback(message: str, value: int, maximum: int) -> None:
    _emit_progress_event({"message": message, "value": value, "maximum": maximum})


def _raise_if_canceled(cancel_callback):
    if cancel_callback is not None and cancel_callback():
        raise MeshroomInstallCanceled("Meshroom installation canceled.")


def _download_archive(url: str, archive_path: Path, cancel_callback) -> None:
    _raise_if_canceled(cancel_callback)
    _progress_callback("Downloading Meshroom. This is a large download and may take several minutes.", 0, 0)

    with urllib.request.urlopen(url, timeout=DOWNLOAD_SOCKET_TIMEOUT_SECONDS) as response:
        total_size = int(response.headers.get("Content-Length") or 0)
        total_mb = max(1, total_size // BYTES_PER_MB) if total_size else 0
        downloaded_size = 0
        last_reported_mb = -1

        with archive_path.open("wb") as f:
            while True:
                _raise_if_canceled(cancel_callback)
                chunk = response.read(BYTES_PER_MB)
                if not chunk:
                    break
                _raise_if_canceled(cancel_callback)
                f.write(chunk)
                downloaded_size += len(chunk)
                downloaded_mb = downloaded_size // BYTES_PER_MB
                if downloaded_mb == last_reported_mb:
                    continue
                if total_mb:
                    reported_mb = min(total_mb, downloaded_mb)
                    _progress_callback(
                        f"Downloading Meshroom ({reported_mb} of {total_mb} MB)...",
                        reported_mb,
                        total_mb,
                    )
                else:
                    _progress_callback(
                        f"Downloading Meshroom ({downloaded_mb} MB downloaded)...",
                        0,
                        0,
                    )
                last_reported_mb = downloaded_mb


def _extract_zip(archive_path: Path, extraction_dir: Path, cancel_callback) -> None:
    _raise_if_canceled(cancel_callback)
    _progress_callback("Extracting Meshroom...", 0, 0)
    extraction_dir_resolved = extraction_dir.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        total_members = len(members)
        report_every = max(1, total_members // 100)
        for i, member in enumerate(members, start=1):
            _raise_if_canceled(cancel_callback)
            destination_path = (extraction_dir / member.filename).resolve()
            if os.path.commonpath([str(extraction_dir_resolved), str(destination_path)]) != str(extraction_dir_resolved):
                raise RuntimeError(f"Archive contains an unsafe path: {member.filename}")
            archive.extract(member, extraction_dir)
            if i == total_members or i % report_every == 0:
                _progress_callback(
                    f"Extracting Meshroom ({i} of {total_members} files)...",
                    i,
                    total_members,
                )


def _extract_tarball(archive_path: Path, extraction_dir: Path, cancel_callback) -> None:
    _raise_if_canceled(cancel_callback)
    _progress_callback("Extracting Meshroom...", 0, 0)
    extraction_dir_resolved = extraction_dir.resolve()
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        total_members = len(members)
        report_every = max(1, total_members // 100)
        for i, member in enumerate(members, start=1):
            _raise_if_canceled(cancel_callback)
            destination_path = (extraction_dir / member.name).resolve()
            if os.path.commonpath([str(extraction_dir_resolved), str(destination_path)]) != str(extraction_dir_resolved):
                raise RuntimeError(f"Archive contains an unsafe path: {member.name}")
            archive.extract(member, extraction_dir, filter="data")
            if i == total_members or i % report_every == 0:
                _progress_callback(
                    f"Extracting Meshroom ({i} of {total_members} files)...",
                    i,
                    total_members,
                )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Download and extract Meshroom.")
    parser.add_argument("--destination", required=True, help="Directory where Meshroom will be extracted.")
    parser.add_argument("--work-dir", required=True, help="Temporary work directory for the archive download.")
    parser.add_argument("--archive-url", required=True, help="URL of the Meshroom archive to download.")
    parser.add_argument("--cancel-file", default="", help="Sentinel file; if it exists, installation exits as canceled.")
    args = parser.parse_args(argv)

    cancel_file = Path(args.cancel_file) if args.cancel_file else None
    cancel_callback = cancel_file.exists if cancel_file is not None else None

    destination = Path(args.destination)
    work_dir = Path(args.work_dir)
    url = args.archive_url

    archive_name = url.rsplit("/", 1)[-1]
    archive_path = work_dir / archive_name

    try:
        _download_archive(url, archive_path, cancel_callback)
        if archive_name.endswith(".tar.gz") or archive_name.endswith(".tgz"):
            _extract_tarball(archive_path, destination, cancel_callback)
        else:
            _extract_zip(archive_path, destination, cancel_callback)
    except MeshroomInstallCanceled as exc:
        _emit_progress_event({"message": str(exc), "value": 0, "maximum": 0, "success": False, "canceled": True})
        return 2
    except Exception as exc:
        _emit_progress_event({"message": str(exc), "value": 0, "maximum": 0, "success": False, "error": str(exc)})
        print(str(exc), file=sys.stderr, flush=True)
        return 1

    _emit_progress_event({"message": "Meshroom installed.", "value": 1, "maximum": 1, "success": True})
    return 0


if __name__ == "__main__":
    sys.exit(main())
