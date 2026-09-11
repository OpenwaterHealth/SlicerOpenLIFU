"""Run each scenario in a fresh Slicer process and check its exit status (#463).

Example from the repository root:
  xvfb-run -a ./build/SlicerWithOpenLIFU --no-splash --disable-settings \
    --ignore-slicerrc --python-script \
    OpenLIFUHome/Testing/Python/OpenLIFUTeardownTest.py automatic-all
"""

import asyncio
import gc
import signal
import sys
import threading
import time
import unittest
import weakref
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

import qt
import slicer
import vtk
from slicer.ScriptedLoadableModule import ScriptedLoadableModuleTest


def process_events(duration=0):
    deadline = time.monotonic() + duration
    while True:
        slicer.app.processEvents()
        qt.QCoreApplication.sendPostedEvents(None, qt.QEvent.DeferredDelete)
        if time.monotonic() >= deadline:
            break
        time.sleep(0.01)


class HomeTeardownTest(ScriptedLoadableModuleTest):
    def test_toolbar_cleanup_and_recreation(self):
        widget = slicer.util.getModuleWidget("OpenLIFUHome")
        self.addCleanup(widget.cleanup)
        first_toolbar = widget.openLIFUToolBar
        first_button = widget.syncAction
        self.assertIsNotNone(first_toolbar)
        self.assertIs(first_button.parent(), first_toolbar)
        widget.setupCloudSyncToolBar()
        self.assertIs(widget.syncAction, first_button)
        with patch.object(slicer.util, "selectModule") as select_module:
            first_button.click()
            select_module.assert_called_once_with("OpenLIFUCloudSync")
            widget.cleanup()
            widget.cleanup()
            first_button.click()
            select_module.assert_called_once_with("OpenLIFUCloudSync")
        self.assertIsNone(widget.syncAction)
        self.assertIsNone(widget.openLIFUToolBar)
        # Recreate before Qt processes the old toolbar's deferred deletion.
        widget.setupCloudSyncToolBar()
        self.assertIsNot(widget.openLIFUToolBar, first_toolbar)
        process_events()
        with patch.object(slicer.util, "selectModule") as select_module:
            widget.syncAction.click()
            select_module.assert_called_once_with("OpenLIFUCloudSync")


class SonicationTeardownTest(ScriptedLoadableModuleTest):
    def make_logic(self):
        from OpenLIFUSonicationControl import OpenLIFUSonicationControlLogic
        logic = OpenLIFUSonicationControlLogic()
        self.addCleanup(logic.cleanup)
        return logic

    def test_cleanup_releases_logic_and_ignores_late_callbacks(self):
        from OpenLIFUSonicationControl import OpenLIFUSonicationControlLogic
        logic = OpenLIFUSonicationControlLogic()
        callback = Mock()
        logic.call_on_lifu_device_connected(callback)
        logic.qt_signals.signal_connected.emit("TX", "test")
        callback.assert_called_once_with()
        logic.cleanup()
        logic.cleanup()
        logic.on_lifu_device_connected("TX", "late")
        logic.on_lifu_data_received("TX", "late")
        callback.assert_called_once_with()
        with self.assertRaises(RuntimeError):
            logic.initialize_lifu_interface(test_mode=True)
        ref = weakref.ref(logic)
        del logic
        gc.collect()
        self.assertIsNone(ref())

    def test_interface_replacement_disconnects_only_owned_callbacks(self):
        from openlifu_sdk.ui import SimulatedLIFUInterface
        logic = self.make_logic()
        callback = Mock()
        unrelated = Mock()
        logic.call_on_lifu_device_connected(callback)
        with patch.object(logic, "_get_current_session_transducer", return_value=None):
            logic.initialize_lifu_interface(test_mode=True)
            old = logic.cur_lifu_interface
            self.assertIsInstance(old, SimulatedLIFUInterface)
            old.txdevice.signal_connected.connect(unrelated)
            logic.reinitialize_lifu_interface(test_mode=True)
        old.txdevice.signal_connected.emit("TX", "old")
        unrelated.assert_called_once_with("TX", "old")
        callback.assert_not_called()
        logic.cur_lifu_interface.txdevice.signal_connected.emit("TX", "new")
        callback.assert_called_once_with()
        logic.cleanup()
        process_events(1.2)  # Includes the simulator's delayed auto-connect callbacks.
        callback.assert_called_once_with()

    def test_failed_interface_replacement_resets_widget(self):
        import OpenLIFUSonicationControl as module

        parent = slicer.qMRMLWidget(slicer.util.mainWindow())
        parent.setLayout(qt.QVBoxLayout())
        widget = module.OpenLIFUSonicationControlWidget(parent)
        self.addCleanup(parent.deleteLater)
        self.addCleanup(widget.cleanup)
        widget.setup()
        logic = widget.logic
        widget.ui.testWithoutHardwareCheckBox.setChecked(True)
        data = SimpleNamespace(loaded_solution=SimpleNamespace(is_approved=lambda: True))
        with patch.object(module, "get_openlifu_data_parameter_node", return_value=data), \
             patch.object(logic, "_get_current_session_transducer", return_value=None):
            logic.initialize_lifu_interface(test_mode=True)
            process_events(1.1)
            logic.cur_solution_on_hardware = SimpleNamespace(id="test-solution")
            widget.updateWidgetSolutionOnHardwareState(module.SolutionOnHardwareState.SUCCESSFUL_SEND)
            widget.updateAllButtonsEnabled()
            widget.updateAllButtons()
            self.assertEqual(module.DeviceConnectedState.CONNECTED, widget._cur_device_connected_state)
            for button in (widget.ui.manuallyGetDeviceStatusPushButton,
                           widget.ui.sendSonicationSolutionToDevicePushButton, widget.ui.runPushButton):
                self.assertTrue(button.isEnabled())

            with patch("openlifu_sdk.ui.SimulatedLIFUInterface", side_effect=RuntimeError("replacement failed")), \
                 patch.object(slicer.util, "errorDisplay"), \
                 self.assertRaisesRegex(RuntimeError, "replacement failed"):
                widget.onReinitializeLIFUInterfacePushButtonClicked(False)

            self.assertIsNone(logic.cur_lifu_interface)
            self.assertIsNone(logic.cur_solution_on_hardware)
            self.assertEqual(module.DeviceConnectedState.NOT_CONNECTED, widget._cur_device_connected_state)
            self.assertEqual(module.SolutionOnHardwareState.NOT_SENT, widget.cur_solution_on_hardware_state)
            self.assertIn("not connected", widget.ui.connectedStateLabel.text)
            self.assertNotEqual("Solution sent to device.", widget.ui.solutionStateLabel.text)
            self.assertEqual("Initialize LIFUInterface", widget.ui.reinitializeLIFUInterfacePushButton.text)
            self.assertTrue(widget.ui.reinitializeLIFUInterfacePushButton.isEnabled())
            for button in (widget.ui.manuallyGetDeviceStatusPushButton,
                           widget.ui.sendSonicationSolutionToDevicePushButton, widget.ui.runPushButton):
                self.assertFalse(button.isEnabled())
            for label in (widget.ui.sdkVersionLabel, widget.ui.consoleVersionLabel, widget.ui.txVersionLabel):
                self.assertEqual("", label.text)

    def test_monitor_shutdown_during_startup(self):
        entered = threading.Event()

        async def start_monitoring(interval):
            entered.set()
            await asyncio.sleep(60)

        logic = self.make_logic()
        interface = Mock(start_monitoring=start_monitoring)
        logic.cur_lifu_interface = interface
        logic._start_real_hardware_monitoring()
        worker, loop, timer = logic._monitor_thread, logic._monitor_loop, logic.monitoring_timer
        self.assertTrue(entered.wait(2))
        logic.cleanup()
        self.assertFalse(worker.is_alive())
        self.assertTrue(loop.is_closed())
        self.assertFalse(timer.isActive())
        interface.close.assert_called_once_with()

    def test_monitor_shutdown_before_worker_starts(self):
        logic = self.make_logic()
        loop = asyncio.new_event_loop()
        stopping = threading.Event()
        stopping.set()
        interface = Mock()
        try:
            logic._run_monitor_loop(loop, interface, stopping)
            interface.start_monitoring.assert_not_called()
        finally:
            loop.close()

    def test_failed_reinitialization_preserves_live_worker(self):
        logic = self.make_logic()
        worker = Mock()
        worker.is_alive.return_value = True
        logic._monitor_thread = worker
        with self.assertLogs(level="WARNING"), self.assertRaises(RuntimeError):
            logic.reinitialize_lifu_interface(test_mode=True)
        with self.assertRaises(RuntimeError):
            logic.initialize_lifu_interface(test_mode=True)
        self.assertIs(logic._monitor_thread, worker)
        worker.is_alive.return_value = False

    def test_worker_timeout_preserves_live_handle_and_releases_bridge(self):
        logic = self.make_logic()
        worker = Mock()
        worker.is_alive.return_value = True
        loop = Mock()
        loop.is_running.return_value = False
        logic._monitor_thread = worker
        logic._monitor_loop = loop
        with self.assertLogs(level="WARNING") as messages:
            logic.cleanup()
        self.assertTrue(any("did not stop" in message for message in messages.output))
        self.assertIs(logic._monitor_thread, worker)
        loop.close.assert_not_called()
        self.assertIsNone(logic.qt_signals)
        worker.is_alive.return_value = False
        logic.cleanup()
        loop.close.assert_called_once_with()

    def test_interface_error_does_not_retain_bridge(self):
        logic = self.make_logic()
        logic.cur_lifu_interface = Mock()
        logic.cur_lifu_interface.stop_monitoring.side_effect = RuntimeError("test stop failure")
        logic.cur_lifu_interface.close.side_effect = RuntimeError("test close failure")
        with self.assertLogs(level="WARNING"):
            logic.cleanup()
        self.assertIsNone(logic.qt_signals)
        self.assertIsNone(logic.cur_lifu_interface)


class CloudTeardownTest(ScriptedLoadableModuleTest):
    def make_logic(self):
        from OpenLIFUCloudSync import OpenLIFUCloudSyncLogic
        logic = OpenLIFUCloudSyncLogic()
        self.addCleanup(logic.cleanup)
        return logic

    def test_cleanup_cancels_pending_startup_and_releases_logic(self):
        from OpenLIFUCloudSync import OpenLIFUCloudSyncLogic
        previous = signal.getsignal(signal.SIGINT)
        logic = OpenLIFUCloudSyncLogic()
        startup = logic.startupTimer
        ref = weakref.ref(logic)
        logic.cleanup()
        logic.cleanup()
        self.assertFalse(startup.isActive())
        self.assertEqual(previous, signal.getsignal(signal.SIGINT))
        del logic
        process_events(1.1)
        gc.collect()
        self.assertIsNone(ref())

    def test_cleanup_after_heartbeat_and_stopped_process(self):
        logic = self.make_logic()
        logic.startHeartbeat()
        periodic = logic.monitorTimer
        self.assertTrue(periodic.isActive())
        process = qt.QProcess()
        callback = logic.onProcessOutput
        process.readyReadStandardOutput.connect(callback)
        logic._process_connections.append((process.readyReadStandardOutput, callback))
        logic.syncProcess = process
        logic.cleanup()
        self.assertFalse(periodic.isActive())
        self.assertIsNone(logic.statusHelper)
        self.assertIsNone(logic.syncProcess)
        logic.startHeartbeat()
        logic.heartbeat()
        logic._safeStatusUpdate("late")

    def test_running_process_is_stopped(self):
        import shutil
        logic = self.make_logic()
        process = qt.QProcess()
        logic.syncProcess = process
        process.start(shutil.which("PythonSlicer"), ["-c", "import time; time.sleep(30)"])
        self.assertTrue(process.waitForStarted(3000))
        logic.cleanup()
        self.assertEqual(qt.QProcess.NotRunning, process.state())

    def test_process_kill_fallback(self):
        logic = self.make_logic()
        process = Mock()
        process.state.return_value = qt.QProcess.Running
        process.waitForFinished.side_effect = [False, True]
        logic.syncProcess = process
        logic.cleanup()
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(2, process.waitForFinished.call_count)
        process.deleteLater.assert_called_once_with()

    def test_logout_allows_login_and_preserves_other_sigint_handler(self):
        original_sigint = signal.getsignal(signal.SIGINT)
        logic = self.make_logic()
        bridge = logic.statusHelper
        response = Mock()
        response.json.return_value = {"idToken": "test", "refreshToken": "test", "expiresIn": "3600"}
        with patch("requests.post", return_value=response):
            self.assertTrue(logic.login("test", "test")[0])
            logic.logout()
            self.assertTrue(logic.login("test", "test")[0])
            self.assertIs(bridge, logic.statusHelper)
        replacement = lambda *_: None
        signal.signal(signal.SIGINT, replacement)
        try:
            logic.cleanup()
            self.assertIs(replacement, signal.getsignal(signal.SIGINT))
        finally:
            signal.signal(signal.SIGINT, original_sigint)
            qt.QSettings().remove("OpenLIFU/CloudRefreshToken")

    def test_shared_logic_can_be_recreated(self):
        from OpenLIFUCloudSync import getCloudSyncLogic
        first = getCloudSyncLogic()
        first.cleanup()
        second = getCloudSyncLogic()
        self.addCleanup(second.cleanup)
        self.assertIsNot(first, second)
        self.assertFalse(second._closed)


def make_wizard_class():
    from OpenLIFUTransducerLocalization import TransducerTrackingWizard

    class TestWizard(TransducerTrackingWizard):
        """Use small MRML fixtures instead of running skin segmentation and registration."""

        def _setup(self, photoscan, volume, transducer, virtual_fit_result_node):
            self._logic = Mock()
            self.photoscan = SimpleNamespace(get_id=lambda: "test")
            self.transducer_surface = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLModelNode")
            self.photoscanMarkupPage = SimpleNamespace(
                temp_markup_fiducials={}, facial_landmarks_fiducial_node=None, exitPlaceFiducialMode=Mock())
            self.skinSegmentationMarkupPage = SimpleNamespace(
                temp_markup_fiducials={}, facial_landmarks_fiducial_node=None, exitPlaceFiducialMode=Mock())
            temporary_pv = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLTransformNode")
            temporary_tp = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLTransformNode")
            self.temporary_ids = (temporary_pv.GetID(), temporary_tp.GetID())
            self.photoscanVolumeTrackingPage = SimpleNamespace(
                photoscan_roi_submesh=None, photoscan_to_volume_transform_node=temporary_pv, scaling_transform_node=None)
            self.transducerPhotoscanTrackingPage = SimpleNamespace(
                transducer_to_volume_transform_node=temporary_tp, _update_distance_map_visibility=Mock())
            self.photoscan_to_volume_transform_node = None
            self.transducer_to_volume_transform_node = None
            self._logic.add_transducer_tracking_result.side_effect = lambda **_: (
                slicer.mrmlScene.AddNewNodeByClass("vtkMRMLTransformNode"),
                slicer.mrmlScene.AddNewNodeByClass("vtkMRMLTransformNode"))
            self._qt_connections = [
                (self.currentIdChanged, self.setPageSpecificNodeDisplaySettings),
                (self.button(qt.QWizard.FinishButton).clicked, self.onFinish),
                (self.button(qt.QWizard.CancelButton).clicked, self.onCancel),
            ]
            for sender, callback in self._qt_connections:
                sender.connect(callback)
            self.addPage(qt.QWizardPage(self))

        def setPageSpecificNodeDisplaySettings(self, page_id):
            pass

    return TestWizard


class WizardTeardownTest(ScriptedLoadableModuleTest):
    @contextmanager
    def real_markup_page(self, page_id):
        import OpenLIFUTransducerLocalization as module
        widget = slicer.util.getModuleWidget("OpenLIFUTransducerLocalization")

        def model():
            sphere = vtk.vtkSphereSource()
            sphere.Update()
            return slicer.modules.models.logic().AddModel(sphere.GetOutput())

        skin = model()
        transducer = SimpleNamespace(
            surface_model_node=model(), body_model_node=model(),
            transform_node=slicer.mrmlScene.AddNewNodeByClass("vtkMRMLTransformNode"),
            cloned_virtual_fit_model=None, update_color=Mock())
        photoscan = SimpleNamespace(
            model_node=model(), facial_landmarks_fiducial_node=None,
            view_node=module.create_threeD_photoscan_view_node("teardown-test"),
            get_id=lambda: "teardown-test")
        photoscan.set_view_nodes = lambda nodes: photoscan.model_node.GetDisplayNode().SetViewNodeIDs(
            [node.GetID() for node in nodes])
        with ExitStack() as patches:
            patches.enter_context(patch.object(module, "get_skin_segmentation", return_value=skin))
            patches.enter_context(patch.object(widget.logic, "load_openlifu_photoscan", return_value=photoscan))
            patches.enter_context(patch.object(widget.logic, "get_transducer_tracking_result_node", return_value=None))
            patches.enter_context(patch.object(widget.logic, "get_volume_facial_landmarks", return_value=None))
            wizard = module.TransducerTrackingWizard(photoscan, None, transducer, None)
            try:
                wizard._needs_initial_maximize = False
                wizard.setStartId(page_id)
                wizard.show()
                process_events()
                yield wizard, wizard.currentPage()
            finally:
                wizard.clean_up()
                wizard.deleteLater()
                process_events()
                for node in (skin, transducer.surface_model_node, transducer.body_model_node,
                             transducer.transform_node, photoscan.model_node, photoscan.view_node):
                    slicer.mrmlScene.RemoveNode(node)

    def test_cancel_during_landmark_placement_releases_real_pages(self):
        for page_id in (0, 1):
            for action in ("cancel", "close"):
                with self.subTest(page_id=page_id, action=action):
                    with self.real_markup_page(page_id) as (wizard, page):
                        self.assertFalse(page.page_locked)
                        page.markupTableWidgetSelected(page.markupsWidget.tableWidget().item(0, 0))
                        node = page.currently_placing_node
                        tag = page._pointModifiedObserverTag
                        page_ref = weakref.ref(page)
                        self.assertIsNotNone(node.GetCommand(tag))
                        self.assertEqual(0, node.GetNumberOfControlPoints())
                        try:
                            if action == "cancel":
                                wizard.button(qt.QWizard.CancelButton).click()
                            else:
                                wizard.close()
                            self.assertIsNone(node.GetScene())
                            self.assertIsNone(node.GetCommand(tag))
                            self.assertIsNone(page._pointModifiedObserverTag)
                            interaction = slicer.app.applicationLogic().GetInteractionNode()
                            self.assertEqual(interaction.ViewTransform, interaction.GetCurrentInteractionMode())
                        finally:
                            # Release the callback even if a regression makes the assertion fail.
                            node.RemoveObserver(tag)
                    del wizard, page, node
                    gc.collect()
                    self.assertIsNone(page_ref())

    def test_switching_landmarks_disconnects_previous_placement(self):
        for page_id in (0, 1):
            with self.subTest(page_id=page_id):
                with self.real_markup_page(page_id) as (wizard, page):
                    observations = []
                    try:
                        table = page.markupsWidget.tableWidget()
                        # Repeat a selection, switch landmarks, then double-click to replace one.
                        for row, double_click in ((0, False), (0, False), (1, False), (1, True)):
                            callback = page.unsetControlPoint if double_click else page.markupTableWidgetSelected
                            callback(table.item(row, 0))
                            node = page.currently_placing_node
                            tag = page._pointModifiedObserverTag
                            self.assertIsNotNone(node.GetCommand(tag))
                            observations.append((node, tag))
                            for previous_node, previous_tag in observations[:-1]:
                                self.assertIsNone(previous_node.GetCommand(previous_tag))
                        node.AddControlPoint(vtk.vtkVector3d(1, 2, 3))
                        self.assertIsNone(node.GetScene())
                        self.assertIsNone(node.GetCommand(tag))
                        landmarks = page.facial_landmarks_fiducial_node
                        self.assertEqual(landmarks.PositionDefined, landmarks.GetNthControlPointPositionStatus(1))
                        position = [0.0, 0.0, 0.0]
                        landmarks.GetNthControlPointPosition(1, position)
                        self.assertEqual([1.0, 2.0, 3.0], position)
                    finally:
                        for node, tag in observations:
                            node.RemoveObserver(tag)

    def make_wizard(self):
        wizard = make_wizard_class()(None, None, Mock(), None)
        self.addCleanup(wizard.deleteLater)
        self.addCleanup(wizard.clean_up)
        return wizard

    def test_accept_preserves_results_and_removes_temporary_nodes(self):
        wizard = self.make_wizard()
        qt.QTimer.singleShot(0, wizard.button(qt.QWizard.FinishButton).click)
        code, pv, tp = wizard.customexec_()
        self.assertEqual(qt.QDialog.Accepted, code)
        self.assertIs(slicer.mrmlScene.GetNodeByID(pv.GetID()), pv)
        self.assertIs(slicer.mrmlScene.GetNodeByID(tp.GetID()), tp)
        self.assertTrue(all(slicer.mrmlScene.GetNodeByID(node_id) is None for node_id in wizard.temporary_ids))
        wizard.clean_up()
        wizard._logic.add_transducer_tracking_result.assert_called_once()

    def test_cancel_and_window_close(self):
        for action in ("cancel", "close"):
            wizard = self.make_wizard()
            callback = wizard.button(qt.QWizard.CancelButton).click if action == "cancel" else wizard.close
            qt.QTimer.singleShot(0, callback)
            code, _, _ = wizard.customexec_()
            self.assertEqual(qt.QDialog.Rejected, code)
            self.assertTrue(wizard._cleaned_up)
            self.assertTrue(all(slicer.mrmlScene.GetNodeByID(node_id) is None for node_id in wizard.temporary_ids))

    def test_exception_during_execution_cleans_up(self):
        class FailingExecWizard(make_wizard_class()):
            def exec_(self):
                raise RuntimeError("test execution failure")

        wizard = FailingExecWizard(None, None, Mock(), None)
        self.addCleanup(wizard.deleteLater)
        with self.assertRaises(RuntimeError):
            wizard.customexec_()
        self.assertTrue(wizard._cleaned_up)

    def test_partial_setup_disconnects_and_disposes_wizard(self):
        base = make_wizard_class()
        refs = []

        class FailingWizard(base):
            def _setup(self, *args):
                super()._setup(*args)
                refs.append(weakref.ref(self))
                raise RuntimeError("test setup failure")

        with self.assertRaises(RuntimeError):
            FailingWizard(None, None, Mock(), None)
        process_events()
        gc.collect()
        self.assertIsNone(refs[0]())


def create_widgets(names):
    for name in names:
        slicer.util.getModuleWidget(name)


def run(scenario):
    if scenario == "automatic-sonication":
        create_widgets(["OpenLIFUSonicationControl"])
    elif scenario == "automatic-cloud":
        create_widgets(["OpenLIFUCloudSync"])
    elif scenario == "automatic-all":
        create_widgets([
            "OpenLIFUHome", "OpenLIFUDatabase", "OpenLIFULogin", "OpenLIFUData",
            "OpenLIFUPrePlanning", "OpenLIFUTransducerLocalization", "OpenLIFUSonicationPlanner",
            "OpenLIFUSonicationControl", "OpenLIFUProtocolConfig", "OpenLIFUCloudSync",
        ])
        process_events(1.1)
    elif scenario == "recreate":
        for name in ("OpenLIFUSonicationControl", "OpenLIFUCloudSync"):
            widget = slicer.util.getModuleWidget(name)
            for _ in range(2):
                widget.cleanup()
                widget.cleanup()
                parent = slicer.qMRMLWidget(slicer.util.mainWindow())
                parent.setLayout(qt.QVBoxLayout())
                widget = type(widget)(parent)
                widget.setup()
            widget.cleanup()
    elif scenario == "wizard-shutdown":
        widget = slicer.util.getModuleWidget("OpenLIFUTransducerLocalization")
        widget.wizard = make_wizard_class()(None, None, Mock(), None, parent=widget.parent)
        widget._running_wizard = True
        widget.wizard.show()
    elif scenario == "transducer-preview-shutdown":
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from openlifu.xdc import Transducer
        from OpenLIFUDataLib.transducer_manager import TransducerPreviewDialog

        widget = slicer.util.getModuleWidget("OpenLIFUData")
        transducer = Transducer.gen_matrix_array(id="shutdown_preview", nx=2, ny=2, units="mm")
        with TemporaryDirectory() as directory:
            mesh_path = str(Path(directory) / "mesh.vtk")
            writer = vtk.vtkPolyDataWriter()
            writer.SetInputData(transducer.get_polydata(units="mm"))
            writer.SetFileName(mesh_path)
            assert writer.Write()
            widget.preview = TransducerPreviewDialog(
                transducer, body_abspath=mesh_path, registration_surface_abspath=mesh_path,
            )
            widget.preview.show()
            process_events()
            widget.preview.viewWidget.threeDView().forceRender()
            assert widget.preview._scene != slicer.mrmlScene
            assert widget.preview._scene.GetNumberOfNodesByClass("vtkMRMLModelNode") == 3
        # Leave the dialog open so application shutdown must release its scene.
    else:
        test_class = {"home": HomeTeardownTest, "sonication": SonicationTeardownTest,
                      "cloud": CloudTeardownTest, "wizard": WizardTeardownTest}[scenario]
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(test_class))
        if not result.wasSuccessful():
            return False
    print(f"TEARDOWN PASSED: {scenario}", flush=True)
    return True


if __name__ == "__main__":
    timeout_reached = False

    def timed_out():
        global timeout_reached
        timeout_reached = True
        print("TEARDOWN FAILED: timed out", flush=True)
        for widget in slicer.app.topLevelWidgets():
            if widget.isVisible():
                print(type(widget).__name__, widget.windowTitle, getattr(widget, "text", ""), flush=True)
        slicer.util.exit(1)

    qt.QTimer.singleShot(30000, timed_out)
    try:
        succeeded = run(sys.argv[1])
    except Exception:
        import traceback
        traceback.print_exc()
        succeeded = False
    slicer.util.exit(0 if succeeded and not timeout_reached else 1)
