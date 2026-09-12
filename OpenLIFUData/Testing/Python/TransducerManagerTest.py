"""Exercise the transducer manager in Slicer with a temporary database."""

import copy
from contextlib import ExitStack
import gc
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import weakref

import numpy as np
import qt
import slicer
import vtk

from openlifu.db import Database, Session, Subject
from openlifu.xdc import Transducer, TransducerArray
from openlifu_sdk.io.LIFUUserConfig import LifuUserConfig

from OpenLIFUDataLib import transducer_manager as manager


MESH = """# vtk DataFile Version 3.0
Test transducer surface
ASCII
DATASET POLYDATA
POINTS 3 float
0 0 0
10 0 0
0 10 0
POLYGONS 1 4
3 0 1 2
"""


def module_config(index, frequency=400):
    return {
        "hwid": f"MODULE{index}",
        "freq": frequency,
        "module": {
            "id": f"module_{index}",
            "name": f"Module {index}",
            "nx": 2,
            "ny": 2,
            "pitch": 1.0,
            "units": "mm",
            "frequency": frequency * 1000.0,
        },
        "calibration_note": f"Keep module {index} calibration",
    }


class FakeTxDevice:
    def __init__(self, configs):
        self.configs = copy.deepcopy(configs)
        self.read_calls = []
        self.writes = []
        self.write_error = None

    def get_tx_module_count(self):
        return len(self.configs)

    def get_module_count(self):
        return len(self.configs)

    def read_config(self, module=0):
        self.read_calls.append(module)
        return LifuUserConfig(json_data=copy.deepcopy(self.configs[module]))

    def write_config_json(self, payload, module=0):
        if self.write_error is not None:
            raise self.write_error
        data = json.loads(payload)
        self.writes.append((module, data))
        self.configs[module] = copy.deepcopy(data)
        return LifuUserConfig(json_data=data)


class FakeInterface:
    def __init__(self, configs):
        self.txdevice = FakeTxDevice(configs)
        self.connected = True

    def is_device_connected(self):
        return self.connected, False

    def close(self):
        raise AssertionError("The manager must not close the shared hardware interface")


class TransducerManagerTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.db = Database.initialize_empty_database(self.root / "db")
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.error_display = self.patches.enter_context(patch.object(slicer.util, "errorDisplay"))
        self.info_display = self.patches.enter_context(patch.object(slicer.util, "infoDisplay"))
        self.warning_display = self.patches.enter_context(patch.object(slicer.util, "warningDisplay"))
        self.confirm = self.patches.enter_context(patch.object(slicer.util, "confirmYesNoDisplay", return_value=False))
        self.current_db = self.patches.enter_context(patch.object(manager, "get_cur_db", return_value=self.db))
        self.patches.enter_context(patch.object(manager, "get_user_account_mode_state", return_value=False))
        self.dialogs = []
        self.interface = None
        original_get_logic = slicer.util.getModuleLogic
        self.original_get_logic = original_get_logic
        self.data_logic = original_get_logic("OpenLIFUData")

        def get_logic(name):
            if name == "OpenLIFUSonicationControl":
                return SimpleNamespace(cur_lifu_interface=self.interface)
            return original_get_logic(name)

        self.patches.enter_context(patch.object(slicer.util, "getModuleLogic", side_effect=get_logic))
        self.addCleanup(self._cleanup_scene)
        slicer.mrmlScene.Clear()
        self.data_logic.getParameterNode()
        slicer.vtkMRMLSubjectHierarchyNode.GetSubjectHierarchyNode(slicer.mrmlScene)
        slicer.app.processEvents()

    def _cleanup_scene(self):
        for dialog in reversed(self.dialogs):
            dialog.reject()
            dialog.deleteLater()
        qt.QCoreApplication.sendPostedEvents(None, qt.QEvent.DeferredDelete)
        self.dialogs.clear()
        slicer.app.processEvents()
        slicer.mrmlScene.Clear()

    def _scene_ids(self):
        return {
            slicer.mrmlScene.GetNthNode(index).GetID()
            for index in range(slicer.mrmlScene.GetNumberOfNodes())
        }

    def _dialog(self):
        dialog = manager.TransducerManagerDialog(self.db)
        self.dialogs.append(dialog)
        return dialog

    def _select(self, dialog, transducer_id):
        for row in range(dialog.table.rowCount):
            if dialog.table.item(row, 1).text() == transducer_id:
                dialog.table.selectRow(row)
                return
        self.fail(f"Transducer {transducer_id} is missing from the manager")

    def _mesh_files(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        body = directory / "body.vtk"
        surface = directory / "surface.vtk"
        body.write_text(MESH)
        surface.write_text(MESH)
        return body, surface

    def _file_definition(self, transducer_id="imported", array=False, name="Imported transducer"):
        directory = self.root / "input" / transducer_id
        body, surface = self._mesh_files(directory)
        if array:
            obj = TransducerArray.from_module_user_configs(
                [module_config(0), module_config(1)], arr_id=transducer_id, arr_name=name,
            )
        else:
            obj = Transducer.gen_matrix_array(id=transducer_id, name=name, nx=2, ny=2, units="mm")
        obj.transducer_body_filename = body.name
        obj.registration_surface_filename = surface.name
        path = directory / f"{transducer_id}.json"
        obj.to_file(path)
        return path, obj

    def _choose_file(self, filepath):
        return patch.object(manager.qt, "QFileDialog", SimpleNamespace(
            getOpenFileName=Mock(return_value=str(filepath)),
        ))

    def _write_template(self, count, template_id=None):
        template_id = template_id or f"openlifu_{count}x400"
        template = TransducerArray.from_module_user_configs(
            [module_config(i) for i in range(count)], arr_id=template_id, arr_name="Template",
        )
        for index, module in enumerate(template.modules):
            module.transform[0, 3] = 25.0 * (2 * index - (count - 1))
        body, surface = self._mesh_files(self.root / "template-meshes" / template_id)
        self.db.write_transducer(template, surface, body)
        return template

    def _edit_device(self, transducer_id="device", name="Device", accepted=True):
        return patch.object(manager, "DeviceConfigEditDialog", return_value=SimpleNamespace(
            customexec_=lambda: (int(accepted), transducer_id, name),
        ))

    def _assert_action_rejected(self, action):
        self.error_display.reset_mock()
        try:
            action(False)
        except (ValueError, RuntimeError, OSError):
            pass
        self.assertTrue(self.error_display.called, "Rejected action must explain the failure")

    def _assert_meshes_copied(self, transducer_id):
        paths = self.db.get_transducer_absolute_filepaths(transducer_id)
        self.assertEqual(MESH, Path(paths["transducer_body_abspath"]).read_text())
        self.assertEqual(MESH, Path(paths["registration_surface_abspath"]).read_text())

    def test_file_import_copies_flat_and_array_definitions_without_changing_scene(self):
        dialog = self._dialog()
        for array in (False, True):
            with self.subTest(array=array):
                transducer_id = "array_import" if array else "flat_import"
                path, _ = self._file_definition(transducer_id, array=array)
                before = self._scene_ids()
                with self._choose_file(path):
                    dialog.addFileButton.click()
                saved = self.db.load_transducer(transducer_id, convert_array=False)
                self.assertIsInstance(saved, TransducerArray if array else Transducer)
                self._assert_meshes_copied(transducer_id)
                self.assertEqual(before, self._scene_ids())
                self.assertNotIn(transducer_id, self.data_logic.getParameterNode().loaded_transducers)
                self._select(dialog, transducer_id)
        self.error_display.assert_not_called()

    def test_cancel_file_selection_leaves_database_and_scene_unchanged(self):
        dialog = self._dialog()
        before = self._scene_ids()
        with self._choose_file(""):
            dialog.onAddFromFile(False)
        self.assertEqual([], self.db.get_transducer_ids())
        self.assertEqual(before, self._scene_ids())
        self.confirm.assert_not_called()
        self.error_display.assert_not_called()

    def test_file_overwrite_requires_separate_confirmation(self):
        path, original = self._file_definition(name="Original name")
        self.db.write_transducer(original, path.parent / "surface.vtk", path.parent / "body.vtk")
        path, _ = self._file_definition(name="Replacement name")
        dialog = self._dialog()
        before = self._scene_ids()
        with self._choose_file(path):
            dialog.onAddFromFile(False)
        self.confirm.assert_called_once()
        self.assertEqual("Original name", self.db.load_transducer(original.id).name)
        self.assertEqual(before, self._scene_ids())

        self.confirm.return_value = True
        with self._choose_file(path):
            dialog.onAddFromFile(False)
        self.assertEqual("Replacement name", self.db.load_transducer(original.id).name)
        self._assert_meshes_copied(original.id)
        self.assertEqual(before, self._scene_ids())
        self.error_display.assert_not_called()

    def test_database_change_during_confirmation_prevents_file_overwrite(self):
        path, original = self._file_definition(name="Original name")
        self.db.write_transducer(original, path.parent / "surface.vtk", path.parent / "body.vtk")
        path, _ = self._file_definition(name="Replacement name")
        dialog = self._dialog()
        other_db = Database.initialize_empty_database(self.root / "other-db")

        def change_database(*args, **kwargs):
            self.current_db.return_value = other_db
            return True

        self.confirm.side_effect = change_database
        with self._choose_file(path):
            self._assert_action_rejected(dialog.onAddFromFile)
        self.assertEqual("Original name", self.db.load_transducer(original.id).name)
        self.assertEqual([], other_db.get_transducer_ids())

    def test_operator_cannot_import_when_account_mode_is_enabled(self):
        path, obj = self._file_definition()
        with patch.object(manager, "get_user_account_mode_state", return_value=True), patch.object(
            manager, "get_current_user", return_value=SimpleNamespace(roles=["operator"]),
        ):
            dialog = self._dialog()
            with self._choose_file(path):
                self._assert_action_rejected(dialog.onAddFromFile)
        self.assertNotIn(obj.id, self.db.get_transducer_ids())

    def _check_protected_definition(self, loaded):
        path, original = self._file_definition(name="Original name")
        self.db.write_transducer(original, path.parent / "surface.vtk", path.parent / "body.vtk")
        if loaded:
            self.data_logic.load_transducer_from_openlifu(
                copy.deepcopy(original),
                transducer_abspaths_info=self.db.get_transducer_absolute_filepaths(original.id),
            )
        else:
            subject = Subject(id="subject", name="Subject")
            self.db.write_subject(subject)
            self.db.write_session(subject, Session(id="session", subject_id=subject.id, transducer_id=original.id))
        path, _ = self._file_definition(name="Replacement name")
        dialog = self._dialog()
        self.confirm.return_value = True
        before = self._scene_ids()
        with self._choose_file(path):
            self._assert_action_rejected(dialog.onAddFromFile)
        self.assertEqual("Original name", self.db.load_transducer(original.id).name)
        self._select(dialog, original.id)
        self._assert_action_rejected(dialog.onDelete)
        self.assertIn(original.id, self.db.get_transducer_ids())
        self.assertEqual(before, self._scene_ids())

    def test_saved_session_blocks_file_overwrite_and_delete(self):
        self._check_protected_definition(loaded=False)

    def test_loaded_definition_blocks_file_overwrite_and_delete(self):
        self._check_protected_definition(loaded=True)

    def test_missing_file_mesh_reports_error_without_importing_scene_or_database(self):
        path, obj = self._file_definition()
        (path.parent / obj.transducer_body_filename).unlink()
        dialog = self._dialog()
        before = self._scene_ids()
        with self._choose_file(path):
            self._assert_action_rejected(dialog.onAddFromFile)
        self.assertNotIn(obj.id, self.db.get_transducer_ids())
        self.assertEqual(before, self._scene_ids())

    def test_delete_cancel_and_confirm_refresh_inventory(self):
        path, obj = self._file_definition()
        self.db.write_transducer(obj, path.parent / "surface.vtk", path.parent / "body.vtk")
        dialog = self._dialog()
        self._select(dialog, obj.id)
        dialog.onDelete(False)
        self.assertIn(obj.id, self.db.get_transducer_ids())
        self.confirm.return_value = True
        dialog.onDelete(False)
        self.assertNotIn(obj.id, self.db.get_transducer_ids())
        self.assertFalse(Path(self.db.get_transducer_filename(obj.id)).parent.exists())
        self.assertEqual(0, dialog.table.rowCount)
        self.error_display.assert_not_called()

    def test_device_import_copies_template_for_one_and_two_modules(self):
        for count in (1, 2):
            with self.subTest(count=count):
                self._write_template(count)
                self.interface = FakeInterface([module_config(i) for i in range(count)])
                dialog = self._dialog()
                before = self._scene_ids()
                transducer_id = f"device_{count}"
                with self._edit_device(transducer_id):
                    dialog.onAddFromDevice(False)
                saved = self.db.load_transducer(transducer_id, convert_array=False)
                self.assertIsInstance(saved, TransducerArray)
                self.assertEqual(count, len(saved.modules))
                self.assertEqual([f"MODULE{i}" for i in range(count)], [m.attrs["hwid"] for m in saved.modules])
                self._assert_meshes_copied(transducer_id)
                self.assertEqual(before, self._scene_ids())
                self.assertEqual([], self.interface.txdevice.writes)
        self.error_display.assert_not_called()

    def test_cancel_device_edit_does_not_save_or_write_hardware(self):
        template = self._write_template(2)
        self.interface = FakeInterface([module_config(0), module_config(1)])
        dialog = self._dialog()
        with self._edit_device(accepted=False):
            dialog.onAddFromDevice(False)
        self.assertEqual([template.id], self.db.get_transducer_ids())
        self.assertEqual([], self.interface.txdevice.writes)
        self.error_display.assert_not_called()

    def test_device_import_requires_a_database_template(self):
        self.interface = FakeInterface([module_config(0), module_config(1)])
        dialog = self._dialog()
        with patch.object(dialog, "_choose_template_id", return_value=None), self._edit_device():
            self._assert_action_rejected(dialog.onAddFromDevice)
        self.assertEqual([], self.db.get_transducer_ids())
        self.assertEqual([], self.interface.txdevice.writes)

    def test_existing_template_can_be_selected_when_canonical_id_is_missing(self):
        template = self._write_template(2, "openlifu_2x400_evt1")
        self.interface = FakeInterface([module_config(0), module_config(1)])
        dialog = self._dialog()
        with patch.object(dialog, "_choose_template_id", return_value=template.id) as choose, self._edit_device():
            dialog.onAddFromDevice(False)
        choose.assert_called_once()
        saved = self.db.load_transducer("device", convert_array=False)
        self.assertEqual(2, len(saved.modules))
        for actual, expected in zip(saved.modules, template.modules):
            np.testing.assert_allclose(actual.transform, expected.transform)
        self._assert_meshes_copied("device")
        self.error_display.assert_not_called()

    def test_cancel_template_selection_does_not_import(self):
        template = self._write_template(2, "openlifu_2x400_evt1")
        self.interface = FakeInterface([module_config(0), module_config(1)])
        dialog = self._dialog()
        with patch.object(dialog, "_choose_template_id", return_value=None), self._edit_device() as edit:
            dialog.onAddFromDevice(False)
        edit.assert_not_called()
        self.assertEqual([template.id], self.db.get_transducer_ids())
        self.assertEqual([], self.interface.txdevice.writes)
        self.error_display.assert_not_called()

    def test_template_module_count_must_match_connected_device(self):
        self._write_template(1, "openlifu_2x400")
        self.interface = FakeInterface([module_config(0), module_config(1)])
        dialog = self._dialog()
        with self._edit_device():
            self._assert_action_rejected(dialog.onAddFromDevice)
        self.assertNotIn("device", self.db.get_transducer_ids())
        self.assertEqual([], self.interface.txdevice.writes)

    def test_template_missing_declared_mesh_does_not_import(self):
        template = self._write_template(2)
        paths = self.db.get_transducer_absolute_filepaths(template.id)
        Path(paths["transducer_body_abspath"]).unlink()
        self.interface = FakeInterface([module_config(0), module_config(1)])
        dialog = self._dialog()
        with self._edit_device():
            self._assert_action_rejected(dialog.onAddFromDevice)
        self.assertNotIn("device", self.db.get_transducer_ids())
        self.assertEqual([], self.interface.txdevice.writes)

    def test_device_import_uses_the_configs_captured_before_the_dialog(self):
        self._write_template(1)
        self.interface = FakeInterface([module_config(0)])
        dialog = self._dialog()

        def finish_edit():
            self.interface.txdevice.configs[0]["module"]["nx"] = 3
            return 1, "device", "Device"

        with patch.object(manager, "DeviceConfigEditDialog", return_value=SimpleNamespace(customexec_=finish_edit)):
            dialog.onAddFromDevice(False)
        saved = self.db.load_transducer("device", convert_array=False)
        self.assertEqual(4, saved.modules[0].numelements())
        self.assertEqual([], self.interface.txdevice.writes)
        self.error_display.assert_not_called()

    def test_recorded_template_does_not_bypass_frequency_validation(self):
        self._write_template(2)
        configs = [module_config(0), module_config(1, frequency=155)]
        configs[0]["device"] = {
            "id": "device", "template": "openlifu_2x400",
            "modules": [{"hwid": cfg["hwid"]} for cfg in configs],
        }
        self.interface = FakeInterface(configs)
        dialog = self._dialog()
        with self._edit_device() as edit:
            self._assert_action_rejected(dialog.onAddFromDevice)
        edit.assert_not_called()
        self.assertNotIn("device", self.db.get_transducer_ids())
        self.assertEqual([], self.interface.txdevice.writes)

    def test_device_identity_mismatch_does_not_save(self):
        self._write_template(2)
        configs = [module_config(0), module_config(1)]
        configs[0]["device"] = {
            "id": "device", "template": "openlifu_2x400",
            "modules": [{"hwid": "MODULE0"}, {"hwid": "WRONG"}],
        }
        self.interface = FakeInterface(configs)
        dialog = self._dialog()
        with self._edit_device():
            self._assert_action_rejected(dialog.onAddFromDevice)
        self.assertNotIn("device", self.db.get_transducer_ids())
        self.assertEqual([], self.interface.txdevice.writes)

    def test_writeback_preserves_current_config_and_full_template_identity(self):
        self._write_template(2)
        self.interface = FakeInterface([module_config(0), module_config(1)])
        dialog = self._dialog()

        def confirm_write(*args, **kwargs):
            self.interface.txdevice.configs[0]["recent_setting"] = {"keep": True}
            return True

        self.confirm.side_effect = confirm_write
        with self._edit_device():
            dialog.onAddFromDevice(False)
        self.assertEqual(1, len(self.interface.txdevice.writes))
        module, payload = self.interface.txdevice.writes[0]
        self.assertEqual(0, module)
        self.assertEqual({"keep": True}, payload["recent_setting"])
        self.assertEqual("Keep module 0 calibration", payload["calibration_note"])
        self.assertEqual("openlifu_2x400", payload["device"]["template"])
        self.assertEqual(["MODULE0", "MODULE1"], [m["hwid"] for m in payload["device"]["modules"]])
        self.assertIn("device", self.db.get_transducer_ids())
        self.error_display.assert_not_called()

    def test_writeback_rechecks_all_module_identities(self):
        self._write_template(2)
        self.interface = FakeInterface([module_config(0), module_config(1)])
        dialog = self._dialog()

        def replace_second_module(*args, **kwargs):
            self.interface.txdevice.configs[1]["hwid"] = "REPLACEMENT"
            return True

        self.confirm.side_effect = replace_second_module
        with self._edit_device():
            self._assert_action_rejected(dialog.onAddFromDevice)
        self.assertEqual([], self.interface.txdevice.writes)
        self.assertIn("device", self.db.get_transducer_ids())

    def test_writeback_failure_keeps_saved_database_definition(self):
        self._write_template(1)
        self.interface = FakeInterface([module_config(0)])
        self.interface.txdevice.write_error = RuntimeError("Device write failed")
        self.confirm.return_value = True
        dialog = self._dialog()
        with self._edit_device():
            self._assert_action_rejected(dialog.onAddFromDevice)
        self.assertIn("device", self.db.get_transducer_ids())
        self._assert_meshes_copied("device")
        self.assertEqual([], self.interface.txdevice.writes)

    def _control_shutdown_fixture(self, stop_error=None):
        from openlifu_sdk.io.signal import OWSignal
        from OpenLIFUSonicationControl import OpenLIFUSonicationControlLogic

        logic = OpenLIFUSonicationControlLogic()
        timer = qt.QTimer()
        timer.setInterval(60000)
        timer.timeout.connect(logic._pumpMonitoringLoop)
        timer.start()
        logic.monitoring_timer = timer
        events = []
        subscriptions = []
        devices = []
        for _ in range(2):
            device = SimpleNamespace()
            for name in ("signal_connected", "signal_disconnected", "signal_data_received", "signal_error"):
                signal = OWSignal()
                subscriber = Mock()
                signal.connect(subscriber)
                setattr(device, name, signal)
                subscriptions.append((signal, subscriber))
            devices.append(device)

        def stop_monitoring():
            self.assertFalse(timer.isActive())
            events.append("stop monitoring")
            if stop_error is not None:
                raise stop_error

        def close():
            self.assertFalse(timer.isActive())
            events.append("close")

        interface = SimpleNamespace(
            hvcontroller=devices[0], txdevice=devices[1],
            stop_monitoring=Mock(side_effect=stop_monitoring), close=Mock(side_effect=close),
        )
        logic.cur_lifu_interface = interface
        logic._connect_owsignals()

        def cleanup():
            logic.cleanup()
            timer.stop()
            timer.deleteLater()

        self.addCleanup(cleanup)
        self.assertTrue(timer.isActive())
        for signal, subscriber in subscriptions:
            self.assertEqual(2, len(signal._slots))
            self.assertIn(subscriber, signal._slots)
        return logic, timer, interface, events, subscriptions

    def test_control_shutdown_disconnects_only_its_callbacks_and_is_idempotent(self):
        logic, timer, interface, events, subscriptions = self._control_shutdown_fixture()
        logic.cleanup()
        self.assertFalse(timer.isActive())
        self.assertEqual(["stop monitoring", "close"], events)
        self.assertIsNone(logic.cur_lifu_interface)
        self.assertIsNone(logic.qt_signals)
        for signal, subscriber in subscriptions:
            self.assertEqual([subscriber], signal._slots)
            signal.emit("unrelated notification")
            subscriber.assert_called_once_with("unrelated notification")
        logic.cleanup()
        interface.stop_monitoring.assert_called_once()
        interface.close.assert_called_once()

    def test_control_shutdown_closes_interface_when_monitoring_stop_fails(self):
        logic, timer, interface, events, subscriptions = self._control_shutdown_fixture(
            stop_error=RuntimeError("Monitoring stop failed"),
        )
        with self.assertLogs(level="WARNING") as logs:
            logic.cleanup()
        self.assertTrue(any("Could not stop interface monitoring" in message for message in logs.output))
        self.assertFalse(timer.isActive())
        self.assertEqual(["stop monitoring", "close"], events)
        interface.close.assert_called_once()
        self.assertIsNone(logic.cur_lifu_interface)
        self.assertIsNone(logic.qt_signals)
        for signal, subscriber in subscriptions:
            self.assertEqual([subscriber], signal._slots)

    def _preview(self, obj, source_dir):
        main_view_count = slicer.app.layoutManager().threeDViewCount
        dialog = manager.TransducerPreviewDialog(
            obj,
            body_abspath=str(source_dir / obj.transducer_body_filename),
            registration_surface_abspath=str(source_dir / obj.registration_surface_filename),
        )
        self.dialogs.append(dialog)
        dialog.show()
        slicer.app.processEvents()
        self.assertEqual(main_view_count, slicer.app.layoutManager().threeDViewCount)
        self.assertIsNotNone(dialog._model_node)
        self.assertIsNotNone(dialog._body_model_node)
        self.assertIsNotNone(dialog._registration_surface_model_node)
        self.assertNotEqual(slicer.mrmlScene, dialog._scene)
        view = dialog.viewWidget.threeDView()
        view.forceRender()
        model_manager = view.displayableManagerByClassName("vtkMRMLModelDisplayableManager")
        for model in (dialog._model_node, dialog._body_model_node, dialog._registration_surface_model_node):
            self.assertEqual(dialog._scene, model.GetScene())
            actor = model_manager.GetActorByID(model.GetDisplayNode().GetID())
            self.assertIsNotNone(actor)
            self.assertTrue(actor.GetVisibility())
        display = dialog._model_node.GetDisplayNode()
        self.assertEqual(1, display.GetNumberOfViewNodeIDs())
        self.assertEqual((dialog._view_node.GetID(),), display.GetViewNodeIDs())
        self.assertGreater(dialog.infoTree.topLevelItemCount, 0)
        self.assertNotEqual("error", dialog.infoTree.topLevelItem(0).text(0))
        return dialog

    def _main_scene_view_state(self):
        displays = {}
        for node in slicer.util.getNodesByClass("vtkMRMLDisplayNode"):
            displays[node.GetID()] = (
                tuple(node.GetViewNodeIDs()), node.GetVisibility(),
                node.GetVisibility2D(), node.GetVisibility3D(), node.GetOpacity(),
                node.GetEditorVisibility() if node.IsA("vtkMRMLTransformDisplayNode") else None,
            )
        slices = {
            node.GetID(): (tuple(node.GetThreeDViewIDs()), node.GetSliceVisible())
            for node in slicer.util.getNodesByClass("vtkMRMLSliceNode")
        }
        camera = slicer.app.layoutManager().threeDWidget(0).threeDView().cameraNode().GetCamera()
        return displays, slices, (
            camera.GetPosition(), camera.GetFocalPoint(), camera.GetViewUp(),
            camera.GetParallelScale(), camera.GetParallelProjection(),
        )

    def test_preview_isolates_session_objects_without_changing_main_views(self):
        main_view = slicer.app.layoutManager().threeDWidget(0).threeDView()
        main_view_id = main_view.mrmlViewNode().GetID()
        sphere = vtk.vtkSphereSource()
        sphere.SetCenter(1000, 1000, 1000)
        sphere.SetRadius(2)
        sphere.Update()
        model = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLModelNode", "Session model")
        model.SetAndObservePolyData(sphere.GetOutput())
        model.CreateDefaultDisplayNodes()
        model.GetDisplayNode().SetVisibility(True)
        extra_display = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLModelDisplayNode")
        extra_display.SetViewNodeIDs([main_view_id])
        model.AddAndObserveDisplayNodeID(extra_display.GetID())

        markups = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode")
        markups.AddControlPoint(vtk.vtkVector3d(1000, 1000, 1000))
        markups.GetDisplayNode().SetPointLabelsVisibility(False)
        segmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode")
        segmentation.CreateDefaultDisplayNodes()
        segmentation.AddSegmentFromClosedSurfaceRepresentation(sphere.GetOutput(), "Session segment", [1, 0, 0])

        volume = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode")
        voxels = np.zeros((6, 6, 6), dtype=np.uint8)
        voxels[1:5, 1:5, 1:5] = 100
        slicer.util.updateVolumeFromArray(volume, voxels)
        volume.SetOrigin(1000, 1000, 1000)
        volume.CreateDefaultDisplayNodes()
        rendering = slicer.modules.volumerendering.logic().CreateDefaultVolumeRenderingNodes(volume)
        rendering.SetVisibility(True)
        slicer.util.setSliceViewerLayers(background=volume)
        for index, node in enumerate(slicer.util.getNodesByClass("vtkMRMLSliceNode")):
            node.SetSliceVisible(True)
            if index == 0:
                node.AddThreeDViewID(main_view_id)

        transform = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLinearTransformNode")
        transform.CreateDefaultDisplayNodes()
        transform.GetDisplayNode().SetEditorVisibility(True)
        main_view.resetFocalPoint()
        main_view.resetCamera()
        slicer.app.processEvents()
        main_view.forceRender()
        before_ids = self._scene_ids()
        before_state = self._main_scene_view_state()
        path, obj = self._file_definition("isolated_preview", array=True)
        dialog = self._preview(obj, path.parent)
        view = dialog.viewWidget.threeDView()
        renderer = view.renderWindow().GetRenderers().GetFirstRenderer()
        bounds = renderer.ComputeVisiblePropBounds()
        self.assertLess(max(bounds), 100)
        self.assertGreater(min(bounds), -100)
        for class_name in ("vtkMRMLVolumeNode", "vtkMRMLMarkupsNode", "vtkMRMLSegmentationNode", "vtkMRMLSliceNode"):
            self.assertEqual(0, dialog._scene.GetNumberOfNodesByClass(class_name))
        self.assertEqual(before_ids, self._scene_ids())
        self.assertEqual(before_state, self._main_scene_view_state())

        view.rotateToViewAxis(0)
        view.cameraNode().GetCamera().Dolly(1.5)
        view.forceRender()
        self.assertEqual(before_state, self._main_scene_view_state())
        dialog.reject()
        slicer.app.processEvents()
        self.assertEqual(before_ids, self._scene_ids())
        self.assertEqual(before_state, self._main_scene_view_state())

    def test_repeated_preview_removes_all_temporary_nodes(self):
        for array in (False, True):
            with self.subTest(array=array):
                path, obj = self._file_definition(f"preview_{int(array)}", array=array)
                before = self._scene_ids()
                for close_method in ("accept", "reject", "close"):
                    dialog = self._preview(obj, path.parent)
                    self.assertEqual(before, self._scene_ids())
                    getattr(dialog, close_method)()
                    slicer.app.processEvents()
                    self.assertEqual(0, dialog._scene.GetNumberOfNodes())
                    self.assertIsNone(dialog._scene_close_tag)
                    self.assertIsNone(dialog.viewWidget.mrmlScene())
                    dialog._cleanup()
                    self.assertEqual(before, self._scene_ids())
        self.assertEqual({}, dict(self.data_logic.getParameterNode().loaded_transducers))

    def test_preview_disposal_releases_private_scene_and_nodes(self):
        path, obj = self._file_definition("disposed_preview", array=True)
        dialog = self._preview(obj, path.parent)
        references = [weakref.ref(dialog), weakref.ref(dialog._scene)]
        references.extend(weakref.ref(node) for node in dialog._owned_nodes)
        dialog.reject()
        dialog.deleteLater()
        self.dialogs.remove(dialog)
        del dialog
        qt.QCoreApplication.sendPostedEvents(None, qt.QEvent.DeferredDelete)
        slicer.app.processEvents()
        gc.collect()
        for reference in references:
            self.assertIsNone(reference())

    def test_failed_preview_creation_removes_temporary_nodes(self):
        path, obj = self._file_definition("missing_preview_mesh")
        (path.parent / obj.transducer_body_filename).unlink()
        before = self._scene_ids()
        before_state = self._main_scene_view_state()
        scenes = []
        original_setup = manager.TransducerPreviewDialog._setup

        def capture_scene(dialog):
            scenes.append(dialog._scene)
            original_setup(dialog)

        with patch.object(manager.TransducerPreviewDialog, "_setup", capture_scene):
            with self.assertRaisesRegex(ValueError, "mesh does not exist"):
                manager.TransducerPreviewDialog(
                    obj, body_abspath=str(path.parent / obj.transducer_body_filename),
                )
        slicer.app.processEvents()
        self.assertEqual(1, len(scenes))
        self.assertEqual(0, scenes[0].GetNumberOfNodes())
        self.assertEqual(before, self._scene_ids())
        self.assertEqual(before_state, self._main_scene_view_state())
        (path.parent / obj.transducer_body_filename).write_text(MESH)
        self._preview(obj, path.parent).reject()
        self.assertEqual(before, self._scene_ids())

    def test_preview_converts_body_and_registration_mesh_units_to_mm(self):
        path, obj = self._file_definition("centimeter_preview")
        obj.units = "cm"
        before = self._scene_ids()
        dialog = self._preview(obj, path.parent)
        for model in (dialog._body_model_node, dialog._registration_surface_model_node):
            bounds = model.GetPolyData().GetBounds()
            np.testing.assert_allclose([bounds[2 * axis + 1] - bounds[2 * axis] for axis in range(3)], (100, 100, 0))
        dialog.reject()
        slicer.app.processEvents()
        self.assertEqual(before, self._scene_ids())

    def test_precreated_widgets_survive_scene_clear_with_open_preview(self):
        with patch.object(slicer.util, "getModuleLogic", side_effect=self.original_get_logic):
            for name in (
                "OpenLIFUHome", "OpenLIFUDatabase", "OpenLIFULogin", "OpenLIFUData",
                "OpenLIFUPrePlanning", "OpenLIFUTransducerLocalization",
                "OpenLIFUSonicationPlanner", "OpenLIFUSonicationControl", "OpenLIFUProtocolConfig",
            ):
                slicer.util.getModule(name).widgetRepresentation()
        path, obj = self._file_definition("scene_clear_preview", array=True)
        dialog = self._preview(obj, path.parent)
        slicer.mrmlScene.Clear()
        self.assertFalse(dialog.isVisible())
        self.assertEqual(0, dialog._scene.GetNumberOfNodes())
        self.assertIsNone(dialog._scene_close_tag)
        slicer.app.processEvents()
        self.data_logic.getParameterNode()
        slicer.vtkMRMLSubjectHierarchyNode.GetSubjectHierarchyNode(slicer.mrmlScene)
        before = self._scene_ids()
        next_dialog = self._preview(obj, path.parent)
        next_dialog.reject()
        slicer.app.processEvents()
        self.assertEqual(before, self._scene_ids())
