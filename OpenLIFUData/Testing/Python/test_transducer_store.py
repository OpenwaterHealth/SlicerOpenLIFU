"""Standalone persistence tests; run with OpenLIFUData on PYTHONPATH."""

import copy
import json
from pathlib import Path

import numpy as np
import pytest

from openlifu.db import Database, Session, Subject
from openlifu.xdc.transducer import Transducer, TransformedTransducer
from openlifu.xdc.transducerarray import TransducerArray
from OpenLIFUDataLib import transducer_store as store


@pytest.fixture
def db(tmp_path):
    return Database.initialize_empty_database(tmp_path / "db")


@pytest.fixture
def source(tmp_path):
    directory = tmp_path / "source"
    directory.mkdir()
    (directory / "body.obj").write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n")
    (directory / "surface.obj").write_text("v 0 0 1\nv 1 0 1\nv 0 1 1\nf 1 2 3\n")
    return directory


@pytest.fixture
def transducer():
    return Transducer.gen_matrix_array(
        nx=1, ny=1, id="device", name="Original device",
        transducer_body_filename="body.obj", registration_surface_filename="surface.obj",
    )


def snapshot(db):
    root = Path(db.path)
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def add_session(db, transducer_id="device"):
    subject = Subject(id="another_subject")
    db.write_subject(subject)
    session = Session(id="saved_session", subject_id=subject.id, transducer_id=transducer_id)
    db.write_session(subject, session)
    return db.get_session_filename(subject.id, session.id)


@pytest.mark.parametrize("transducer_id", [None, "", ".", "..", "../device", "a/b", "a\\b", "C:device", "CON", "device.", " device", "a\x00b"])
def test_invalid_ids(transducer_id):
    with pytest.raises(ValueError, match="single filename component"):
        store.validate_transducer_id(transducer_id)


def test_save_flat_and_overwrite_from_same_database(db, transducer, source):
    original = transducer.to_json()
    assert store.save_transducer(db, transducer, source) is None
    assert transducer.to_json() == original
    directory = db.get_transducer_filename("device").parent
    for filename in ("body.obj", "surface.obj"):
        assert (directory / filename).read_bytes() == (source / filename).read_bytes()
    loaded = db.load_transducer("device", convert_array=False)
    assert loaded.to_json() == original
    loaded.name = "Updated name"
    assert store.save_transducer(db, loaded, directory, overwrite=True) is None
    assert db.load_transducer("device").name == "Updated name"
    assert db.get_transducer_ids() == ["device"]
    assert not list(directory.parent.glob(".transducer-store-*"))


def test_array_preserves_all_module_companions_and_input(db, transducer, source):
    (source / "module").mkdir()
    (source / "module" / "module.obj").write_text("v 3 0 0\n")
    first = TransformedTransducer.from_transducer(transducer, np.eye(4))
    second = copy.deepcopy(first)
    second.id = "second"
    second.transducer_body_filename = "module/module.obj"
    second.registration_surface_filename = None
    array = TransducerArray(
        id="device", modules=[first, second],
        attrs={"transducer_body_filename": str(source / "body.obj")},
    )
    original = array.to_json()
    store.save_transducer(db, array, source)
    assert array.to_json() == original
    loaded = db.load_transducer("device", convert_array=False)
    assert isinstance(loaded, TransducerArray)
    assert len(loaded.modules) == 2
    assert loaded.transducer_body_filename == "body.obj"
    assert loaded.modules[1].transducer_body_filename == "module.obj"
    assert loaded.modules[0].registration_surface_filename == "surface.obj"
    flat = db.load_transducer("device", convert_array=True)
    assert flat.numelements() == 2
    paths = db.get_transducer_absolute_filepaths("device")
    assert Path(paths["transducer_body_abspath"]).read_bytes() == (source / "body.obj").read_bytes()
    assert Path(paths["registration_surface_abspath"]).read_bytes() == (source / "surface.obj").read_bytes()
    assert (db.get_transducer_filename("device").parent / "module.obj").read_bytes() == (source / "module/module.obj").read_bytes()


@pytest.mark.parametrize("problem", ["missing", "basename_collision", "metadata_collision"])
def test_bad_companions_leave_database_and_input_unchanged(db, transducer, source, problem):
    store.save_transducer(db, transducer, source)
    before = snapshot(db)
    if problem == "missing":
        transducer.registration_surface_filename = "missing.obj"
    elif problem == "basename_collision":
        (source / "other").mkdir()
        (source / "other/body.obj").write_text("different mesh")
        transducer.registration_surface_filename = "other/body.obj"
    else:
        (source / "device.json").write_text("mesh colliding with metadata")
        transducer.registration_surface_filename = "device.json"
    original = transducer.to_json()
    with pytest.raises((ValueError, FileNotFoundError)):
        store.save_transducer(db, transducer, source, overwrite=True)
    assert snapshot(db) == before
    assert transducer.to_json() == original


def test_unconfirmed_overwrite_does_not_write(db, transducer, source):
    store.save_transducer(db, transducer, source)
    before = snapshot(db)
    with pytest.raises(ValueError, match="overwrite was not confirmed"):
        store.save_transducer(db, transducer, source)
    assert snapshot(db) == before


@pytest.mark.parametrize("operation", ["overwrite", "delete"])
@pytest.mark.parametrize("reference", ["loaded", "active", "saved"])
def test_references_block_both_mutations(db, transducer, source, operation, reference):
    store.save_transducer(db, transducer, source)
    kwargs = {}
    if reference == "loaded":
        kwargs["loaded_ids"] = ["device"]
    elif reference == "active":
        kwargs["active_transducer_id"] = "device"
    else:
        add_session(db)
    before = snapshot(db)
    with pytest.raises(ValueError, match="loaded|referenced"):
        if operation == "overwrite":
            store.save_transducer(db, transducer, source, overwrite=True, **kwargs)
        else:
            store.delete_transducer(db, "device", **kwargs)
    assert snapshot(db) == before


@pytest.mark.parametrize("damage", ["missing_subject_index", "invalid_subject_index", "missing_session_index", "invalid_session_index", "missing_session", "invalid_session", "missing_reference"])
def test_reference_scan_fails_closed(db, transducer, source, damage):
    store.save_transducer(db, transducer, source)
    metadata_path = add_session(db, transducer_id="another_device")
    if damage == "missing_subject_index":
        db.get_subjects_filename().unlink()
    elif damage == "invalid_subject_index":
        db.get_subjects_filename().write_text('{}')
    elif damage == "missing_session_index":
        db.get_sessions_filename("another_subject").unlink()
    elif damage == "invalid_session_index":
        db.get_sessions_filename("another_subject").write_text('{"session_ids": "saved_session"}')
    elif damage == "missing_session":
        metadata_path.unlink()
    elif damage == "invalid_session":
        metadata_path.write_text('{')
    else:
        metadata_path.write_text('{}')
    before = snapshot(db)
    with pytest.raises((ValueError, FileNotFoundError)):
        store.delete_transducer(db, "device")
    assert snapshot(db) == before


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", ["copy", "serialization", "install"])
def test_save_failure_restores_original_bytes(db, transducer, source, monkeypatch, existing, failure):
    if existing:
        store.save_transducer(db, transducer, source)
    before = snapshot(db)
    transducer.name = "Replacement"
    (source / "body.obj").write_text("replacement body")
    if failure == "copy":
        original_copy = store.shutil.copy2

        def fail_copy(src, dst):
            if Path(src).name == "surface.obj":
                raise OSError("injected copy failure")
            return original_copy(src, dst)

        monkeypatch.setattr(store.shutil, "copy2", fail_copy)
    elif failure == "serialization":
        def fail_serialization(*args, **kwargs):
            raise OSError("injected serialization failure")

        monkeypatch.setattr(Transducer, "to_file", fail_serialization)
    else:
        original_replace = store.os.replace
        target = db.get_transducer_filename("device").parent
        failed = False

        def fail_install(src, dst):
            nonlocal failed
            if Path(dst) == target and not failed:
                failed = True
                raise OSError("injected install failure")
            return original_replace(src, dst)

        monkeypatch.setattr(store.os, "replace", fail_install)
    with pytest.raises(OSError, match="injected"):
        store.save_transducer(db, transducer, source, overwrite=existing)
    assert snapshot(db) == before


@pytest.mark.parametrize("operation", ["insert", "delete"])
def test_index_replace_failure_rolls_back_directory(db, transducer, source, monkeypatch, operation):
    if operation == "delete":
        store.save_transducer(db, transducer, source)
    before = snapshot(db)
    original_replace = store.os.replace

    def fail_index(src, dst):
        if Path(dst) == db.get_transducers_filename():
            raise OSError("injected index replacement failure")
        return original_replace(src, dst)

    monkeypatch.setattr(store.os, "replace", fail_index)
    with pytest.raises(OSError, match="injected index"):
        if operation == "insert":
            store.save_transducer(db, transducer, source)
        else:
            store.delete_transducer(db, "device")
    assert snapshot(db) == before


@pytest.mark.parametrize("operation", ["overwrite", "delete"])
def test_cleanup_failure_reports_committed_change(db, transducer, source, monkeypatch, operation):
    store.save_transducer(db, transducer, source)
    transducer.name = "Replacement"

    def fail_cleanup(path):
        raise OSError("injected cleanup failure")

    monkeypatch.setattr(store.shutil, "rmtree", fail_cleanup)
    if operation == "overwrite":
        warning = store.save_transducer(db, transducer, source, overwrite=True)
        assert db.load_transducer("device").name == "Replacement"
    else:
        warning = store.delete_transducer(db, "device")
        assert db.get_transducer_ids() == []
        assert not db.get_transducer_filename("device").parent.exists()
    assert "was saved" in warning or "was deleted" in warning
    assert "injected cleanup failure" in warning
    backup = next((Path(db.path) / "transducers").glob(".transducer-store-*/backup"))
    assert json.loads((backup / "device.json").read_text())["name"] == "Original device"


def test_failed_rollback_preserves_recovery_files(db, transducer, source, monkeypatch):
    store.save_transducer(db, transducer, source)
    original_bytes = db.get_transducer_filename("device").read_bytes()
    original_index = db.get_transducers_filename().read_bytes()
    original_replace = store.os.replace
    target = db.get_transducer_filename("device").parent

    def fail_install_and_restore(src, dst):
        if Path(dst) == target:
            raise OSError("injected install/restore failure")
        return original_replace(src, dst)

    monkeypatch.setattr(store.os, "replace", fail_install_and_restore)
    with pytest.raises(RuntimeError, match="recovery files"):
        store.save_transducer(db, transducer, source, overwrite=True)
    backup = next(target.parent.glob(".transducer-store-*/backup"))
    assert (backup / "device.json").read_bytes() == original_bytes
    assert db.get_transducers_filename().read_bytes() == original_index


def test_failed_insert_rollback_keeps_workspace(db, transducer, source, monkeypatch):
    original_index = db.get_transducers_filename().read_bytes()
    original_replace = store.os.replace

    def fail_index_and_rollback(src, dst):
        if Path(dst) == db.get_transducers_filename() or Path(dst).name == "failed-replacement":
            raise OSError("injected index/rollback failure")
        return original_replace(src, dst)

    monkeypatch.setattr(store.os, "replace", fail_index_and_rollback)
    with pytest.raises(RuntimeError, match="recovery files"):
        store.save_transducer(db, transducer, source)
    workspace = next((Path(db.path) / "transducers").glob(".transducer-store-*"))
    assert (workspace / "transducers.json").is_file()
    assert db.get_transducers_filename().read_bytes() == original_index
    assert db.get_transducer_filename("device").exists()


def test_delete_preserves_other_definitions_and_index_metadata(db, transducer, source):
    store.save_transducer(db, transducer, source)
    other = copy.deepcopy(transducer)
    other.id = "other"
    store.save_transducer(db, other, source)
    index = json.loads(db.get_transducers_filename().read_text())
    index["extra_metadata"] = {"version": 7}
    db.get_transducers_filename().write_text(json.dumps(index))
    other_bytes = db.get_transducer_filename("other").read_bytes()
    assert store.delete_transducer(db, "device") is None
    assert db.get_transducer_ids() == ["other"]
    assert db.get_transducer_filename("other").read_bytes() == other_bytes
    assert json.loads(db.get_transducers_filename().read_text())["extra_metadata"] == {"version": 7}
    assert not db.get_transducer_filename("device").parent.exists()
