"""Persist transducer definitions and meshes without changing loaded scene objects."""

import copy
import json
import os
from pathlib import Path
import shutil
import tempfile


_MESH_FIELDS = ("transducer_body_filename", "registration_surface_filename")
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
}


class _RollbackError(RuntimeError):
    """Recovery files must remain available after an unsuccessful rollback."""


def _validate_component(value, description):
    if (
        not isinstance(value, str)
        or not value
        or value in (".", "..")
        or value != value.strip()
        or value.endswith(".")
        or any(ord(char) < 32 or char in '/\\<>:"|?*' for char in value)
        or value.split(".")[0].upper() in _WINDOWS_RESERVED
    ):
        raise ValueError(f"{description} must be a valid single filename component: {value!r}")
    return value


def validate_transducer_id(transducer_id):
    """Return the ID, or raise ValueError if it cannot safely name a directory."""
    return _validate_component(transducer_id, "Transducer ID")


def _read_index(path, key):
    path = Path(path)
    if path.is_symlink():
        raise ValueError(f"Database index must not be a symbolic link: {path}")
    content = path.read_bytes()
    data = json.loads(content)
    if not isinstance(data, dict) or not isinstance(data.get(key), list):
        raise ValueError(f"Database index must contain a '{key}' list: {path}")
    for item in data[key]:
        _validate_component(item, f"ID in {path}")
    if len(set(data[key])) != len(data[key]):
        raise ValueError(f"Database index contains duplicate IDs: {path}")
    return data, content


def ensure_transducer_not_referenced(
    db, transducer_id, loaded_ids=(), active_transducer_id=None
):
    """Reject loaded or session-referenced IDs, including unreadable session indexes."""
    validate_transducer_id(transducer_id)
    if transducer_id in loaded_ids:
        raise ValueError(f"Transducer '{transducer_id}' is loaded in the scene. Unload it first.")
    if transducer_id == active_transducer_id:
        raise ValueError(f"Transducer '{transducer_id}' is referenced by the active session.")

    subjects, _ = _read_index(db.get_subjects_filename(), "subject_ids")
    references = []
    for subject_id in subjects["subject_ids"]:
        sessions, _ = _read_index(db.get_sessions_filename(subject_id), "session_ids")
        for session_id in sessions["session_ids"]:
            metadata = db.load_session_info(subject_id, session_id)
            if not isinstance(metadata, dict) or "transducer_id" not in metadata:
                raise ValueError(f"Session {subject_id}/{session_id} has no transducer_id field.")
            reference = metadata["transducer_id"]
            if reference is not None:
                validate_transducer_id(reference)
            if reference == transducer_id:
                references.append(f"{subject_id}/{session_id}")
    if references:
        raise ValueError(
            f"Transducer '{transducer_id}' is referenced by saved sessions: "
            + ", ".join(references)
        )


def _database_paths(db, transducer_id):
    validate_transducer_id(transducer_id)
    root = Path(db.path).resolve() / "transducers"
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"Transducers directory must be an existing directory, not a link: {root}")
    target = root / transducer_id
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise ValueError(f"Transducer destination must be a directory, not a link: {target}")
    index = root / "transducers.json"
    data, content = _read_index(index, "transducer_ids")
    indexed = transducer_id in data["transducer_ids"]
    if indexed != target.exists():
        raise ValueError(f"Transducer index and directory disagree for '{transducer_id}'.")
    return root, target, index, data, content


def _copy_meshes(transducer, source_dir, destination):
    sources_by_name = {}
    metadata_name = f"{transducer.id}.json".casefold()
    objects = [transducer, *getattr(transducer, "modules", [])]
    for obj in objects:
        for field in _MESH_FIELDS:
            filename = getattr(obj, field, None)
            if filename is None:
                continue
            if not isinstance(filename, str) or not filename:
                raise ValueError(f"Invalid {field}: {filename!r}")
            source = (Path(source_dir) / filename).resolve(strict=True)
            if not source.is_file():
                raise ValueError(f"Mesh is not a file: {source}")
            name = _validate_component(Path(filename).name, "Mesh filename")
            key = name.casefold()
            if key == metadata_name:
                raise ValueError(f"Mesh filename conflicts with transducer metadata: {name}")
            previous = sources_by_name.get(key)
            if previous is not None and previous[0] != source:
                raise ValueError(f"Different mesh files have the same destination filename: {name}")
            if previous is None:
                shutil.copy2(source, destination / name)
                sources_by_name[key] = (source, name)
            else:
                name = previous[1]
            setattr(obj, field, name)


def _commit_directory(target, replacement, index, index_content, new_index, workspace):
    """Swap directories, then commit the index; restore the directory on failure."""
    prepared_index = workspace / "transducers.json"
    if new_index is not None:
        prepared_index.write_text(json.dumps(new_index), encoding="utf-8")
    if index.read_bytes() != index_content:
        raise RuntimeError("Transducer index changed during the operation. Please try again.")

    backup = workspace / "backup"
    moved_old = False
    installed_new = False
    try:
        if target.exists():
            os.replace(target, backup)
            moved_old = True
        if replacement is not None:
            os.replace(replacement, target)
            installed_new = True
        if new_index is not None:
            os.replace(prepared_index, index)
    except Exception as error:
        try:
            if installed_new:
                os.replace(target, workspace / "failed-replacement")
            if moved_old:
                os.replace(backup, target)
        except Exception as rollback_error:
            raise _RollbackError(
                f"Transducer operation failed ({error}) and could not restore the original "
                f"directory ({rollback_error}). Inspect {target}; recovery files are in {workspace}."
            ) from error
        raise


def _cleanup_failed_operation(workspace, error):
    # A backup still present after an exception is needed for manual recovery.
    if isinstance(error, _RollbackError) or (workspace / "backup").exists():
        return
    try:
        shutil.rmtree(workspace)
    except Exception as cleanup_error:
        raise RuntimeError(
            f"{error} Temporary files could not be removed from {workspace}: {cleanup_error}"
        ) from error


def _cleanup_committed_operation(workspace, description):
    try:
        shutil.rmtree(workspace)
    except Exception as error:
        return f"{description}, but temporary/backup files could not be removed from {workspace}: {error}"
    return None


def save_transducer(
    db, transducer, source_dir, *, overwrite=False, loaded_ids=(), active_transducer_id=None
):
    """Save a copy with local mesh basenames; return a warning only for cleanup failure.

    References, conflicting filenames and inconsistent indexes raise ValueError.
    Read/write errors propagate after rollback. A failed rollback raises RuntimeError
    with the recovery directory. No scene state or input object is changed.
    """
    from openlifu.db import Database

    transducer_id = validate_transducer_id(transducer.id)
    root, target, index, data, content = _database_paths(db, transducer_id)
    if target.exists() and not overwrite:
        raise ValueError(f"Transducer '{transducer_id}' already exists; overwrite was not confirmed.")
    loaded_ids = tuple(loaded_ids)
    ensure_transducer_not_referenced(db, transducer_id, loaded_ids, active_transducer_id)
    workspace = Path(tempfile.mkdtemp(prefix=".transducer-store-", dir=root))
    try:
        staged_db = Database.initialize_empty_database(workspace / "staged")
        staged_transducer = copy.deepcopy(transducer)
        replacement = staged_db.get_transducer_filename(transducer_id).parent
        replacement.mkdir()
        _copy_meshes(staged_transducer, source_dir, replacement)
        staged_db.write_transducer(staged_transducer)
        staged_db.load_transducer(transducer_id, convert_array=False)
        staged_db.load_transducer(transducer_id, convert_array=True)
        ensure_transducer_not_referenced(db, transducer_id, loaded_ids, active_transducer_id)
        _database_paths(db, transducer_id)
        new_index = None
        if transducer_id not in data["transducer_ids"]:
            new_index = copy.deepcopy(data)
            new_index["transducer_ids"].append(transducer_id)
        _commit_directory(target, replacement, index, content, new_index, workspace)
    except Exception as error:
        _cleanup_failed_operation(workspace, error)
        raise
    return _cleanup_committed_operation(workspace, f"Transducer '{transducer_id}' was saved")


def delete_transducer(db, transducer_id, *, loaded_ids=(), active_transducer_id=None):
    """Delete an unreferenced definition; return a warning only for cleanup failure."""
    root, target, index, data, content = _database_paths(db, transducer_id)
    if transducer_id not in data["transducer_ids"]:
        raise ValueError(f"Transducer '{transducer_id}' does not exist in the database.")
    ensure_transducer_not_referenced(db, transducer_id, loaded_ids, active_transducer_id)
    data["transducer_ids"].remove(transducer_id)
    workspace = Path(tempfile.mkdtemp(prefix=".transducer-store-", dir=root))
    try:
        _commit_directory(target, None, index, content, data, workspace)
    except Exception as error:
        _cleanup_failed_operation(workspace, error)
        raise
    return _cleanup_committed_operation(workspace, f"Transducer '{transducer_id}' was deleted")
