# Standard library imports
import os
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, List, Optional

SAMPLE_DATABASE_REPOSITORY_URL = "https://github.com/OpenwaterHealth/openlifu-sample-database"
SAMPLE_DATABASE_TAG = "openlifu-v0.20.0"
STARTER_DATABASE_TAG = "openlifu-v0.20.0-no-subjects"


def archive_url_for_tag(tag: str) -> str:
    return f"{SAMPLE_DATABASE_REPOSITORY_URL}/archive/refs/tags/{tag}.zip"


def readme_url_for_tag(tag: str) -> str:
    return f"{SAMPLE_DATABASE_REPOSITORY_URL}/blob/{tag}/README.md"


SAMPLE_DATABASE_ARCHIVE_URL = archive_url_for_tag(SAMPLE_DATABASE_TAG)
STARTER_DATABASE_ARCHIVE_URL = archive_url_for_tag(STARTER_DATABASE_TAG)
SAMPLE_DATABASE_BUTTON_HELP_TEXT = (
    "Create a database in the selected folder containing Openwater sample data."
)
STARTER_DATABASE_BUTTON_HELP_TEXT = (
    "Create a database in the selected folder containing Openwater sample data, excluding any sample subjects. Sample transducers and protocols are provided."
)
SAMPLE_DATABASE_README_URL = readme_url_for_tag(SAMPLE_DATABASE_TAG)
STARTER_DATABASE_README_URL = readme_url_for_tag(STARTER_DATABASE_TAG)

REQUIRED_OPENLIFU_DATABASE_INDEX_FILES = (
    "protocols/protocols.json",
    "subjects/subjects.json",
    "transducers/transducers.json",
    "users/users.json",
)

PROGRESS_LINE_PREFIX = "OPENLIFU_SAMPLE_DATA_PROGRESS "
GIT_LFS_POINTER_SIGNATURE = b"version https://git-lfs.github.com/spec/v1"
BYTES_PER_MB = 1024 * 1024
DOWNLOAD_SOCKET_TIMEOUT_SECONDS = 45


class SampleDatabaseSetupCanceled(Exception):
    """Raised when sample database setup is canceled by the caller."""


def raise_if_canceled(cancel_callback: Optional[Callable[[], bool]]) -> None:
    if cancel_callback is not None and cancel_callback():
        raise SampleDatabaseSetupCanceled("Sample database setup canceled.")


def path_is_openlifu_database_root(path: Path) -> bool:
    """Check if a path has the required OpenLIFU database index files."""
    path = Path(path)
    if not path.is_dir():
        return False

    for relative_path in REQUIRED_OPENLIFU_DATABASE_INDEX_FILES:
        if not (path / relative_path).is_file():
            return False

    return True


def validate_openlifu_database_root(path: Path) -> None:
    path = Path(path)
    if not path.is_dir():
        raise RuntimeError(f"Sample database source is not a directory: {path}")

    missing_paths = [
        relative_path
        for relative_path in REQUIRED_OPENLIFU_DATABASE_INDEX_FILES
        if not (path / relative_path).is_file()
    ]
    if missing_paths:
        raise RuntimeError(
            "Sample database is missing required database index files: "
            + ", ".join(missing_paths)
        )


def find_git_lfs_pointer_files(path: Path) -> List[Path]:
    path = Path(path)
    pointer_files = []
    for candidate in path.rglob("*"):
        if not candidate.is_file():
            continue
        with candidate.open("rb") as candidate_file:
            header = candidate_file.read(1024)
        if GIT_LFS_POINTER_SIGNATURE in header:
            pointer_files.append(candidate)
    return pointer_files


def find_extracted_sample_database_root(extraction_dir: Path) -> Path:
    extraction_dir = Path(extraction_dir)
    candidates = [extraction_dir]
    candidates.extend(child for child in extraction_dir.iterdir() if child.is_dir())

    for candidate in candidates:
        if path_is_openlifu_database_root(candidate):
            return candidate

    raise RuntimeError(
        "Downloaded sample database archive did not contain an OpenLIFU database root."
    )


def validate_sample_database_destination_can_install(destination: Path) -> None:
    destination = Path(destination)

    if destination.exists():
        if not destination.is_dir():
            raise RuntimeError(f"Sample database destination is not a directory: {destination}")
        if any(destination.iterdir()):
            raise RuntimeError(
                "Sample data can only be created in an empty folder. "
                f"The selected folder is not empty: {destination}"
            )


def validate_sample_database_can_install(source: Path, destination: Path) -> None:
    source = Path(source)

    validate_openlifu_database_root(source)
    pointer_files = find_git_lfs_pointer_files(source)
    if pointer_files:
        rel_pointer_files = [
            str(pointer_file.relative_to(source))
            for pointer_file in pointer_files[:5]
        ]
        more_files = len(pointer_files) - len(rel_pointer_files)
        more_suffix = f", and {more_files} more" if more_files else ""
        raise RuntimeError(
            "Sample database contains unresolved Git LFS pointer files: "
            + ", ".join(rel_pointer_files)
            + more_suffix
        )

    validate_sample_database_destination_can_install(destination)


def move_sample_database_into_place(
    source: Path,
    destination: Path,
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
    cancel_callback: Optional[Callable[[], bool]] = None,
) -> None:
    source = Path(source)
    destination = Path(destination)

    raise_if_canceled(cancel_callback)
    if progress_callback is not None:
        progress_callback("Validating sample database contents...", 0, 0)

    validate_sample_database_can_install(source, destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination_existed = destination.exists()

    raise_if_canceled(cancel_callback)
    if progress_callback is not None:
        progress_callback("Installing sample database into the selected folder...", 0, 0)

    raise_if_canceled(cancel_callback)
    try:
        if destination_existed:
            destination.rmdir()
        source.rename(destination)
    except Exception:
        if destination_existed and not destination.exists():
            destination.mkdir(parents=True, exist_ok=True)
        raise


def copy_sample_database_from_archive(
    destination: Path,
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
    archive_url: Optional[str] = None,
    work_dir: Optional[Path] = None,
    cancel_callback: Optional[Callable[[], bool]] = None,
) -> None:
    destination = Path(destination)
    raise_if_canceled(cancel_callback)
    validate_sample_database_destination_can_install(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    archive_url = archive_url or SAMPLE_DATABASE_ARCHIVE_URL

    if work_dir is None:
        with tempfile.TemporaryDirectory(prefix=f".{destination.name}-sample-download-", dir=destination.parent) as temp_dir:
            _copy_sample_database_from_archive_in_work_dir(
                destination,
                Path(temp_dir),
                archive_url,
                progress_callback,
                cancel_callback,
            )
    else:
        _copy_sample_database_from_archive_in_work_dir(
            destination,
            Path(work_dir),
            archive_url,
            progress_callback,
            cancel_callback,
        )


def _copy_sample_database_from_archive_in_work_dir(
    destination: Path,
    work_dir: Path,
    archive_url: str,
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
    cancel_callback: Optional[Callable[[], bool]] = None,
) -> None:
    destination = Path(destination)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    raise_if_canceled(cancel_callback)
    if any(work_dir.iterdir()):
        raise RuntimeError(f"Sample database work directory is not empty: {work_dir}")

    archive_path = work_dir / f"openlifu-sample-database-{SAMPLE_DATABASE_TAG}.zip"
    extraction_dir = work_dir / "extracted"
    extraction_dir.mkdir()

    download_and_extract_archive_with_progress(
        archive_url,
        archive_path,
        extraction_dir,
        progress_callback or (lambda message, value, maximum: None),
        cancel_callback=cancel_callback,
    )

    raise_if_canceled(cancel_callback)
    sample_database_root = find_extracted_sample_database_root(extraction_dir)
    move_sample_database_into_place(
        sample_database_root,
        destination,
        progress_callback=progress_callback,
        cancel_callback=cancel_callback,
    )


def download_and_extract_archive_with_progress(
    url: str,
    archive_path: Path,
    extraction_dir: Path,
    progress_callback: Callable[[str, int, int], None],
    cancel_callback: Optional[Callable[[], bool]] = None,
) -> None:
    archive_path = Path(archive_path)
    extraction_dir = Path(extraction_dir)

    raise_if_canceled(cancel_callback)
    progress_callback(
        "Downloading OpenLIFU sample data. This is a large download and may take several minutes.",
        0,
        0,
    )

    with urllib.request.urlopen(url, timeout=DOWNLOAD_SOCKET_TIMEOUT_SECONDS) as response:
        total_size = int(response.headers.get("Content-Length") or 0)
        total_mb = max(1, total_size // BYTES_PER_MB) if total_size else 0
        downloaded_size = 0
        last_reported_download_mb = -1

        with archive_path.open("wb") as archive_file:
            while True:
                raise_if_canceled(cancel_callback)
                chunk = response.read(BYTES_PER_MB)
                if not chunk:
                    break

                raise_if_canceled(cancel_callback)
                archive_file.write(chunk)
                downloaded_size += len(chunk)

                downloaded_mb = downloaded_size // BYTES_PER_MB
                if downloaded_mb == last_reported_download_mb:
                    continue

                if total_mb:
                    reported_download_mb = min(total_mb, downloaded_mb)
                    progress_callback(
                        f"Downloading OpenLIFU sample data ({reported_download_mb} of {total_mb} MB)...",
                        reported_download_mb,
                        total_mb,
                    )
                else:
                    progress_callback(
                        f"Downloading OpenLIFU sample data ({downloaded_mb} MB downloaded)...",
                        0,
                        0,
                    )
                last_reported_download_mb = downloaded_mb

    raise_if_canceled(cancel_callback)
    progress_callback("Extracting OpenLIFU sample data...", 0, 0)

    extraction_dir_resolved = extraction_dir.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        total_members = len(members)
        report_every = max(1, total_members // 100) if total_members else 1

        for member_index, member in enumerate(members, start=1):
            raise_if_canceled(cancel_callback)
            destination_path = (extraction_dir / member.filename).resolve()
            if os.path.commonpath([str(extraction_dir_resolved), str(destination_path)]) != str(extraction_dir_resolved):
                raise RuntimeError(
                    f"Sample database archive contains an unsafe path: {member.filename}"
                )

            archive.extract(member, extraction_dir)
            if member_index == total_members or member_index % report_every == 0:
                progress_callback(
                    f"Extracting OpenLIFU sample data ({member_index} of {total_members} files)...",
                    member_index,
                    total_members,
                )
