"""Session-picker and session-create dialogs.

Used by both Home (SlicerOpenLIFU#635) and Data Manager. Kept in
:mod:`OpenLIFUApp.dialogs` per ``docs/coding-standards.md`` rule 7:
any dialog used from more than one place lives in its own file
outside the page files.

Four dialogs, matching Home's four launch buttons:

* :class:`NewPlanningSessionDialog` -- id + name + volume + protocol
  + transducer.
* :class:`ContinuePlanningSessionDialog` -- subject + planning
  session picker; no create action.
* :class:`NewSonicationSessionDialog` -- id + name + plan.
* :class:`ContinueSonicationSessionDialog` -- subject + sonication
  session picker; no create action.

Plus one prerequisite picker:

* :class:`SubjectPickerDialog` -- subject-only picker Home opens
  before the two New-* dialogs (Data Manager has its own subject
  combo so it does not need this).

None of these dialogs touches the app state or Slicer scene. They
collect user input and expose it as attributes (``session_id``,
``subject_id``, etc.); the caller runs the actual create / load
action against :mod:`OpenLIFUApp.logic.session_actions`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

import qt
import slicer

if TYPE_CHECKING:
    import openlifu.db


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def show_info_dialog(text: str) -> None:
    """Show a modal info dialog scoped to OpenLIFU.

    Duplicated here rather than imported from ``data_manager_page`` so
    that dialogs stay a leaf module -- no circular import risk if a
    dialog is ever pulled in during page load ordering shakeups.
    """
    slicer.util.infoDisplay(text, windowTitle="OpenLIFU")


# ---------------------------------------------------------------------------
# New-session dialogs
# ---------------------------------------------------------------------------

class NewPlanningSessionDialog(qt.QDialog):
    """Collect id / name / volume / protocol / transducer for a new PlanningSession.

    On accept, exposes:

    * ``session_id`` (str)
    * ``session_name`` (str)
    * ``volume_id`` (str)
    * ``protocol_id`` (str)
    * ``transducer_id`` (str)

    Does NOT write to the database. Caller runs
    :func:`OpenLIFUApp.logic.session_actions.create_planning_session`
    after :meth:`exec_` returns :attr:`QDialog.Accepted`.

    The button box lives OUTSIDE the ``QFormLayout`` (added directly
    to a wrapping ``QVBoxLayout``) -- a ``QDialogButtonBox`` added as
    a ``QFormLayout`` row has intermittently swallowed the Ok button
    under PythonQt (see ``plan_finalization.FinalizePlanDialog`` for
    the same mitigation on the same class of symptom).
    """

    def __init__(self, database: "openlifu.db.Database", subject_id: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Planning Session")
        self.session_id = ""
        self.session_name = ""
        self.volume_id = ""
        self.protocol_id = ""
        self.transducer_id = ""

        outer = qt.QVBoxLayout(self)
        form_widget = qt.QWidget()
        form = qt.QFormLayout(form_widget)

        self.id_edit = qt.QLineEdit()
        self.name_edit = qt.QLineEdit()
        self.volume_combo = qt.QComboBox()
        self.protocol_combo = qt.QComboBox()
        self.transducer_combo = qt.QComboBox()
        for combo, populate in (
            (self.volume_combo,     lambda: database.get_volume_ids(subject_id) if database else []),
            (self.protocol_combo,   lambda: database.get_protocol_ids() if database else []),
            (self.transducer_combo, lambda: database.get_transducer_ids() if database else []),
        ):
            try:
                combo.addItems(populate())
            except Exception:  # noqa: BLE001
                pass

        form.addRow("ID:", self.id_edit)
        form.addRow("Name:", self.name_edit)
        form.addRow("Volume:", self.volume_combo)
        form.addRow("Protocol:", self.protocol_combo)
        form.addRow("Transducer:", self.transducer_combo)
        outer.addWidget(form_widget)

        buttons = qt.QDialogButtonBox(
            qt.QDialogButtonBox.Ok | qt.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.on_ok_clicked)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def on_ok_clicked(self) -> None:
        session_id = self.id_edit.text.strip()
        if not session_id:
            show_info_dialog("An ID is required.")
            return
        self.session_id = session_id
        self.session_name = self.name_edit.text.strip() or session_id
        self.volume_id = self.volume_combo.currentText
        self.protocol_id = self.protocol_combo.currentText
        self.transducer_id = self.transducer_combo.currentText
        if not (self.volume_id and self.protocol_id and self.transducer_id):
            show_info_dialog("Volume, protocol, and transducer are required.")
            return
        self.accept()


class NewSonicationSessionDialog(qt.QDialog):
    """Collect id / name / plan for a new SonicationSession.

    Only shown when the caller has already verified that at least
    one Plan exists for the subject. Exposes ``session_id``,
    ``session_name``, and ``plan_id`` on accept.
    """

    def __init__(self, subject_id: str, plan_ids: List[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Sonication Session")
        self.session_id = ""
        self.session_name = ""
        self.plan_id = ""

        outer = qt.QVBoxLayout(self)
        form_widget = qt.QWidget()
        form = qt.QFormLayout(form_widget)

        self.id_edit = qt.QLineEdit()
        self.name_edit = qt.QLineEdit()
        self.plan_combo = qt.QComboBox()
        self.plan_combo.addItems(plan_ids)

        form.addRow("ID:", self.id_edit)
        form.addRow("Name:", self.name_edit)
        form.addRow("Plan:", self.plan_combo)
        outer.addWidget(form_widget)

        buttons = qt.QDialogButtonBox(
            qt.QDialogButtonBox.Ok | qt.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.on_ok_clicked)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def on_ok_clicked(self) -> None:
        session_id = self.id_edit.text.strip()
        if not session_id:
            show_info_dialog("An ID is required.")
            return
        self.session_id = session_id
        self.session_name = self.name_edit.text.strip() or session_id
        self.plan_id = self.plan_combo.currentText
        if not self.plan_id:
            show_info_dialog("A plan is required.")
            return
        self.accept()


# ---------------------------------------------------------------------------
# Subject picker (used before opening a New-* dialog from Home)
# ---------------------------------------------------------------------------

class SubjectPickerDialog(qt.QDialog):
    """Modal picker: choose a subject from the current database.

    On accept, ``subject_id`` holds the chosen id. Cancel leaves it
    an empty string.

    Home uses this before opening :class:`NewPlanningSessionDialog` /
    :class:`NewSonicationSessionDialog`, since Home has no persistent
    subject combo (that lives on the Data Manager). We use a bespoke
    dialog rather than ``qt.QInputDialog.getItem`` because the latter
    has an inconsistent return-value shape under PythonQt (returns a
    bare ``str`` on some builds instead of the documented ``(text,
    ok)`` tuple), which caused a
    ``ValueError: too many values to unpack`` regression when Home's
    launch buttons first shipped.
    """

    def __init__(
        self,
        database: "openlifu.db.Database",
        title: str = "Choose a subject",
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.subject_id: str = ""
        try:
            subject_ids = list(database.get_subject_ids())
        except Exception:  # noqa: BLE001
            subject_ids = []

        outer = qt.QVBoxLayout(self)

        row = qt.QHBoxLayout()
        row.addWidget(qt.QLabel("Subject:"))
        self.subject_combo = qt.QComboBox()
        self.subject_combo.setMinimumWidth(240)
        self.subject_combo.addItems(subject_ids)
        row.addWidget(self.subject_combo, 1)
        outer.addLayout(row)

        if not subject_ids:
            self.hint_label = qt.QLabel(
                "The loaded database has no subjects. Create one in "
                "the Data Manager."
            )
            self.hint_label.setWordWrap(True)
            self.hint_label.setStyleSheet("color: #888;")
            outer.addWidget(self.hint_label)

        # PythonQt does not expose ``QDialogButtonBox.button(role)``
        # reliably (returns None), so we build the Ok / Cancel buttons
        # by hand and add them via ``addButton(button, role)``. Same
        # workaround is used in :class:`SessionPickerDialogBase`.
        buttons = qt.QDialogButtonBox()
        ok_button = qt.QPushButton("OK")
        ok_button.setEnabled(bool(subject_ids))
        buttons.addButton(ok_button, qt.QDialogButtonBox.AcceptRole)
        cancel_button = qt.QPushButton("Cancel")
        buttons.addButton(cancel_button, qt.QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self.on_ok_clicked)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def on_ok_clicked(self) -> None:
        picked = self.subject_combo.currentText
        if not picked:
            show_info_dialog("Select a subject.")
            return
        self.subject_id = picked
        self.accept()


# ---------------------------------------------------------------------------
# Continue-session pickers
# ---------------------------------------------------------------------------

class SessionPickerDialogBase(qt.QDialog):
    """Base picker: subject combo on top, session-id list below,
    Load / Cancel buttons at the bottom.

    Subclasses override :meth:`list_session_ids_for_subject` to select
    the collection to pick from. On accept, ``subject_id`` and
    ``session_id`` are populated. Cancel leaves them empty strings.

    Kept public (no leading underscore) per ``docs/coding-standards.md``
    rule 2 -- the "hide it from outside" cue in this codebase is
    "not in any page's public docstring", not a name prefix.
    """

    #: Overridden by subclasses.
    window_title: str = "Choose a Session"
    #: Overridden by subclasses.
    load_button_label: str = "Load"
    #: Message shown when the current subject has no matching sessions.
    empty_message: str = "This subject has no sessions of this kind."

    def __init__(self, database: "openlifu.db.Database", parent=None):
        super().__init__(parent)
        self.database = database
        self.setWindowTitle(self.window_title)
        self.subject_id: str = ""
        self.session_id: str = ""

        outer = qt.QVBoxLayout(self)

        row = qt.QHBoxLayout()
        row.addWidget(qt.QLabel("Subject:"))
        self.subject_combo = qt.QComboBox()
        self.subject_combo.setMinimumWidth(240)
        try:
            self.subject_combo.addItems(database.get_subject_ids())
        except Exception:  # noqa: BLE001
            pass
        self.subject_combo.currentIndexChanged.connect(self.on_subject_changed)
        row.addWidget(self.subject_combo, 1)
        outer.addLayout(row)

        self.session_list = qt.QListWidget()
        self.session_list.setMinimumHeight(160)
        # Double-clicking a session row is a natural "Load" shortcut.
        self.session_list.itemDoubleClicked.connect(lambda *_: self.on_ok_clicked())
        outer.addWidget(self.session_list, 1)

        self.hint_label = qt.QLabel("")
        self.hint_label.setStyleSheet("color: #888;")
        outer.addWidget(self.hint_label)

        # See note in ``SubjectPickerDialog``: PythonQt does not
        # expose ``QDialogButtonBox.button(role)`` reliably, so we
        # build the Load / Cancel buttons manually.
        buttons = qt.QDialogButtonBox()
        ok_button = qt.QPushButton(self.load_button_label)
        buttons.addButton(ok_button, qt.QDialogButtonBox.AcceptRole)
        cancel_button = qt.QPushButton("Cancel")
        buttons.addButton(cancel_button, qt.QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self.on_ok_clicked)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        # Populate the session list from the initial subject selection.
        self.on_subject_changed()

    # -- override -------------------------------------------------------

    def list_session_ids_for_subject(self, subject_id: str) -> List[str]:
        """Return session ids for the given subject. Overridden per session kind."""
        raise NotImplementedError

    # -- handlers -------------------------------------------------------

    def on_subject_changed(self, *_) -> None:
        self.session_list.clear()
        subject_id = self.subject_combo.currentText
        if not subject_id:
            self.hint_label.text = "No subject selected."
            return
        try:
            session_ids = self.list_session_ids_for_subject(subject_id)
        except Exception as exc:  # noqa: BLE001
            self.hint_label.text = f"Could not list sessions: {exc}"
            return
        if not session_ids:
            self.hint_label.text = self.empty_message
            return
        self.hint_label.text = f"{len(session_ids)} session(s)."
        for session_id in session_ids:
            self.session_list.addItem(session_id)

    def on_ok_clicked(self) -> None:
        subject_id = self.subject_combo.currentText
        item = self.session_list.currentItem()
        if not subject_id:
            show_info_dialog("Select a subject.")
            return
        if item is None:
            show_info_dialog("Select a session.")
            return
        self.subject_id = subject_id
        self.session_id = item.text()
        self.accept()


class ContinuePlanningSessionDialog(SessionPickerDialogBase):
    """Subject + PlanningSession picker for the Home page's Continue button."""

    window_title = "Continue Planning Session"
    load_button_label = "Load Planning Session"
    empty_message = "This subject has no planning sessions."

    def list_session_ids_for_subject(self, subject_id: str) -> List[str]:
        return list(self.database.get_planning_session_ids(subject_id))


class ContinueSonicationSessionDialog(SessionPickerDialogBase):
    """Subject + SonicationSession picker for the Home page's Continue button."""

    window_title = "Continue Sonication Session"
    load_button_label = "Load Sonication Session"
    empty_message = "This subject has no sonication sessions."

    def list_session_ids_for_subject(self, subject_id: str) -> List[str]:
        return list(self.database.get_sonication_session_ids(subject_id))
