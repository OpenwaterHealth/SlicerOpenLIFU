# Standard library imports
import inspect
from dataclasses import fields, is_dataclass, MISSING
from typing import (
    Annotated,
    Any,
    List,
    Optional,
    Type,
    get_args,
    get_origin,
)

# Third-party imports
import ctk
import qt
import slicer

# Slicer imports
import slicer
from slicer.i18n import tr as _

# OpenLIFULib imports
from OpenLIFULib.util import display_errors, get_hints


def instantiate_without_post_init(cls: Type, **kwargs) -> Any:
    """
    Creates an instance of a dataclass without invoking its __init__ or __post_init__ methods.

    This is useful when you need to construct an object for intermediate or invalid states
    (e.g., during GUI editing) where strict validation in __post_init__ would raise exceptions.

    Args:
        cls (Type): The dataclass type to instantiate.
        **kwargs: Field values to assign directly to the instance.

    Returns:
        Any: An instance of the given dataclass with fields populated.

    Raises:
        TypeError: If a required field is not provided and has no default or default_factory.
    """
    obj = cls.__new__(cls)  # type: ignore
    for field in fields(cls):
        if field.name in kwargs:
            setattr(obj, field.name, kwargs[field.name])
        elif field.default is not MISSING:
            setattr(obj, field.name, field.default)
        elif field.default_factory is not MISSING:  # type: ignore
            setattr(obj, field.name, field.default_factory())  # type: ignore
        else:
            raise TypeError(f"Missing required field: {field.name}")
    return obj

class CreateStringDialog(qt.QDialog):
    """
    Dialog for entering a strings, typically used for adding to a list of
    strings.
    """

    def __init__(self, name: str, parent="mainWindow"):
        """
        Args:
            name (str): Label for the input field (what the string represents).
            parent (QWidget or str): Parent widget or "mainWindow". Defaults to "mainWindow".
        """
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        self.setWindowTitle(f"Add {name}")
        self.setWindowModality(qt.Qt.ApplicationModal)
        self.name = name
        self.setup()

    def setup(self):
        self.setMinimumWidth(300)
        self.setContentsMargins(15, 15, 15, 15)

        formLayout = qt.QFormLayout()
        formLayout.setSpacing(10)
        self.setLayout(formLayout)

        self.input = qt.QLineEdit()
        formLayout.addRow(_(f"{self.name}:"), self.input)

        self.buttonBox = qt.QDialogButtonBox()
        self.buttonBox.setStandardButtons(qt.QDialogButtonBox.Ok |
                                          qt.QDialogButtonBox.Cancel)
        formLayout.addWidget(self.buttonBox)

        self.buttonBox.rejected.connect(self.reject)
        self.buttonBox.accepted.connect(self.validateInputs)

    def validateInputs(self):
        typed = self.input.text

        if not typed:
            slicer.util.errorDisplay(f"{self.name} field cannot be empty.", parent=self)
            return

        self.accept()

    def customexec_(self):
        returncode = self.exec_()
        if returncode == qt.QDialog.Accepted:
            return (returncode, self.input.text)
        return (returncode, None)

class ListTableWidget(qt.QWidget):
    """
    A widget for displaying and editing a list of items in a single-column
    table.

    Each row represents an item. Items are stored using Qt's UserRole for
    internal retrieval. Includes a button to add new entries via a dialog.
    """
    
    def __init__(self, parent=None, object_name: str = "Item", object_type: Type = str):
        """
        Args:
            parent (QWidget, optional): Parent widget.
            object_name (str): Label for the table column and button. Defaults to "Item".
            object_type (Type): Type of the items stored in the list. Defaults to str.
        """
        super().__init__(parent)
        self.object_name = object_name
        self.object_type = object_type

        top_level_layout = qt.QVBoxLayout(self)

        # Add the table representing the list

        self.table = qt.QTableWidget(self)
        self.table.setColumnCount(1)
        self.table.setHorizontalHeaderLabels([object_name])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(150)

        top_level_layout.addWidget(self.table)

        # Add and Remove buttons
        buttons_layout = qt.QHBoxLayout()

        self.add_button = qt.QPushButton(f"Add {object_name}", self)
        self.remove_button = qt.QPushButton(f"Remove {object_name}", self)

        for button in [self.add_button, self.remove_button]:
            button.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Preferred)
            buttons_layout.addWidget(button)

        self.add_button.clicked.connect(self._open_add_dialog)
        self.remove_button.clicked.connect(self._remove_selected_item)

        top_level_layout.addLayout(buttons_layout)

    def _open_add_dialog(self):
        if self.object_type is str:
            createDlg = CreateStringDialog(self.object_name)
        else:
            createDlg = CreateAbstractClassDialog(self.object_name, self.object_type)
        returncode, new_object = createDlg.customexec_()

        if not returncode:
            return

        self._add_row(new_object)

    def _remove_selected_item(self):
        selected_items = self.table.selectedItems()
        if not selected_items:
            slicer.util.errorDisplay(f"Please select a {self.object_name} to delete.")
            return

        selected_row = selected_items[0].row()
        self.table.removeRow(selected_row)

    def _add_row(self, new_object):
        row_position = self.table.rowCount
        self.table.insertRow(row_position)

        # Within the table itself, a string representation is required.
        # However, to associate custom user data, Qt.UserRole is used, which is
        # a predefined constant in Qt used to store custom,
        # application-specific data in the table, set with setData and
        # retrieved with .data(Qt.UserRole).
        new_object_item = qt.QTableWidgetItem(str(new_object))
        new_object_item.setData(qt.Qt.UserRole, new_object)
        new_object_item.setFlags(new_object_item.flags() & ~qt.Qt.ItemIsEditable)
        self.table.setItem(row_position, 0, new_object_item)

    def to_list(self):
        """
        Returns:
            list: A list of items currently stored in the table.
        """
        result = []
        for row in range(self.table.rowCount):
            object_item = self.table.item(row, 0)
            if object_item:
                result.append(object_item.data(qt.Qt.UserRole))
        return result

    def from_list(self, data: list):
        """
        Populates the table from a given list of items.

        Args:
            data (list): List of items to display in the table.
        """
        self.table.setRowCount(0)
        for obj in data:
            self._add_row(obj)

class CreateKeyValueDialog(qt.QDialog):
    """
    Dialog for entering a key-value pair as strings, typically used for
    dictionary inputs.
    """

    def __init__(self, key_name: str, val_name: str, existing_keys: List[str], parent="mainWindow"):
        """
        Args:
            key_name (str): Label for the key input field.
            val_name (str): Label for the value input field.
            existing_keys (List[str]): List of keys to prevent duplicates.
            parent (QWidget or str): Parent widget or "mainWindow". Defaults to "mainWindow".
        """
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        self.setWindowTitle("Add Entry")
        self.setWindowModality(qt.Qt.ApplicationModal)
        self.key_name = key_name
        self.val_name = val_name
        self.existing_keys = existing_keys
        self.setup()

    def setup(self):
        self.setMinimumWidth(300)
        self.setContentsMargins(15, 15, 15, 15)

        formLayout = qt.QFormLayout()
        formLayout.setSpacing(10)
        self.setLayout(formLayout)

        self.key_input = qt.QLineEdit()
        formLayout.addRow(_(f"{self.key_name}:"), self.key_input)

        self.val_input = qt.QLineEdit()
        formLayout.addRow(_(f"{self.val_name}:"), self.val_input)

        self.buttonBox = qt.QDialogButtonBox()
        self.buttonBox.setStandardButtons(qt.QDialogButtonBox.Ok |
                                          qt.QDialogButtonBox.Cancel)
        formLayout.addWidget(self.buttonBox)

        self.buttonBox.rejected.connect(self.reject)
        self.buttonBox.accepted.connect(self.validateInputs)

    def validateInputs(self):
        """
        Ensure a key does not exist and that inputs are valid
        """
        typed_key = self.key_input.text
        typed_val = self.val_input.text

        if not typed_key:
            slicer.util.errorDisplay(f"{self.key_name} field cannot be empty.", parent=self)
            return
        if not typed_val:
            slicer.util.errorDisplay(f"{self.val_name} field cannot be empty.", parent=self)
            return
        if any(k == typed_key for k in self.existing_keys):
            slicer.util.errorDisplay(f"You cannot add duplicate {self.key_name} entries.", parent=self)
            return

        self.accept()

    def customexec_(self):
        returncode = self.exec_()
        if returncode == qt.QDialog.Accepted:
            return (returncode, self.key_input.text, self.val_input.text)
        return (returncode, None, None)

class CreateKeyAbstractClassValueDialog(CreateKeyValueDialog):
    """
    Dialog for entering a key-class pair, where class can be entered
    through the form generated by OpenLIFUAbstractDataclassDefinitionFormWidget;
    ideal for adding entries with arbitrary value types (e.g. custom
    classes) into dictionaries
    """

    def __init__(self, key_name: str, val_name: str, val_type: Type, existing_keys: List[str], use_post_init: bool = True, parent="mainWindow"):
        """
        Args:
            key_name (str): Label for the key input field.
            val_name (str): Label for the value input section.
            val_type (Type): Class type used to generate the form for value input.
            existing_keys (List[str]): List of keys to prevent duplicates.
            parent (QWidget or str): Parent widget or "mainWindow". Defaults to "mainWindow".
        """
        self.val_type = val_type
        self.use_post_init = use_post_init
        super().__init__(key_name, val_name, existing_keys, slicer.util.mainWindow() if parent == "mainWindow" else parent)

    def setup(self):
        self.setMinimumWidth(300)
        self.setContentsMargins(15, 15, 15, 15)

        formLayout = qt.QFormLayout()
        formLayout.setSpacing(10)
        self.setLayout(formLayout)

        self.key_input = qt.QLineEdit()
        formLayout.addRow(_(f"{self.key_name}:"), self.key_input)

        self.val_input = OpenLIFUAbstractDataclassDefinitionFormWidget(self.val_type, parent=self, is_collapsible=False)
        formLayout.addRow(_(f"{self.val_name}:"), self.val_input)

        self.buttonBox = qt.QDialogButtonBox()
        self.buttonBox.setStandardButtons(qt.QDialogButtonBox.Ok |
                                          qt.QDialogButtonBox.Cancel)
        formLayout.addWidget(self.buttonBox)

        self.buttonBox.rejected.connect(self.reject)
        self.buttonBox.accepted.connect(self.validateInputs)

    def validateInputs(self):
        """
        Ensure a key does not exist and that inputs are valid
        """
        typed_key = self.key_input.text
        typed_val = self.val_input.get_form_as_class(post_init=self.use_post_init)

        if not typed_key:
            slicer.util.errorDisplay(f"{self.key_name} field cannot be empty.", parent=self)
            return
        if typed_val is None:
            raise ValueError(f"{self.val_name} field cannot be None.")
        if any(k == typed_key for k in self.existing_keys):
            slicer.util.errorDisplay(f"You cannot add duplicate {self.key_name} entries.", parent=self)
            return

        self.accept()

    def customexec_(self):
        returncode = self.exec_()
        if returncode == qt.QDialog.Accepted:
            return (returncode, self.key_input.text, self.val_input.get_form_as_class(post_init=self.use_post_init))
        return (returncode, None, None)

class DictTableWidget(qt.QWidget):
    """
    A widget for displaying and editing dictionary entries in a two-column
    table.

    Each row represents a key-value pair. Values can be of any specified type
    and are stored using Qt's UserRole for internal retrieval. Includes a
    button to add new entries via a dialog.
    """

    def __init__(self, parent=None, key_name: str = "Key", val_name: str = "Value", val_type: Type = str):
        """
        Args:
            parent (QWidget, optional): Parent widget.
            key_name (str): Label for the key column. Defaults to "Key".
            val_name (str): Label for the value column. Defaults to "Value".
            val_type (Type): Type of the values stored in the dictionary. Defaults to str.
        """
        super().__init__(parent)
        self.key_name = key_name
        self.val_name = val_name
        self.val_type = val_type

        top_level_layout = qt.QVBoxLayout(self)

        # Add the table representing the dictionary

        self.table = qt.QTableWidget(self)
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels([key_name, val_name])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(150)

        top_level_layout.addWidget(self.table)

        # Add and Remove buttons
        buttons_layout = qt.QHBoxLayout()

        self.add_button = qt.QPushButton(f"Add entry", self)
        self.remove_button = qt.QPushButton(f"Remove entry", self)

        for button in [self.add_button, self.remove_button]:
            button.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Preferred)
            buttons_layout.addWidget(button)

        self.add_button.clicked.connect(self._open_add_dialog)
        self.remove_button.clicked.connect(self._remove_selected_item)

        top_level_layout.addLayout(buttons_layout)

    def _open_add_dialog(self):
        """
        Opens a dialog for adding a new key-value pair to the data structure.

        This function creates and displays a dialog based on the type of values
        expected (`self.val_type`). The user is prompted to enter a key and
        corresponding value. 

        - If `self.val_type` is `str`, a standard dialog
          (`CreateKeyValueDialog`) is used to collect string inputs for both the
          key and the value.
        - If `self.val_type` is not `str`, a specialized dialog
          (`CreateKeyAbstractClassValueDialog`) is shown. This dialog allows the
          user to define the value as an instance of an abstract class, using a
          form driven by `OpenLIFUAbstractDataclassDefinitionFormWidget`.

        If the dialog is accepted (i.e., the user clicks "OK"), the entered
        key-value pair is added to the internal data via `self._add_row`.
        """
        existing_keys = list(self.to_dict().keys())
        if self.val_type is str:
            createDlg = CreateKeyValueDialog(self.key_name, self.val_name, existing_keys)
        else:
            createDlg = CreateKeyAbstractClassValueDialog(self.key_name, self.val_name, self.val_type, existing_keys)
        returncode, key, val = createDlg.customexec_()
        if not returncode:
            return

        self._add_row(key, val)

    def _remove_selected_item(self):
        selected_items = self.table.selectedItems()
        if not selected_items:
            slicer.util.errorDisplay(f"Please select an entry to delete.")
            return

        selected_row = selected_items[0].row()
        self.table.removeRow(selected_row)

    def _add_row(self, key, val):
        row_position = self.table.rowCount
        self.table.insertRow(row_position)

        key_item = qt.QTableWidgetItem(key)
        key_item.setFlags(key_item.flags() & ~qt.Qt.ItemIsEditable)
        self.table.setItem(row_position, 0, key_item)

        # Within the table itself, a string representation is required.
        # However, to associate custom user data, Qt.UserRole is used, which is
        # a predefined constant in Qt used to store custom,
        # application-specific data in the table, set with setData and
        # retrieved with .data(Qt.UserRole).
        val_item = qt.QTableWidgetItem(str(val))
        val_item.setData(qt.Qt.UserRole, val)
        val_item.setFlags(val_item.flags() & ~qt.Qt.ItemIsEditable)
        self.table.setItem(row_position, 1, val_item)

    def to_dict(self, post_init: bool = True):
        """
        Extracts the key-value pairs from the DictTableWidget as a dictionary.

        Args:
            post_init (bool): If True (default), this method attempts to reconstruct 
                each value using the class constructor (i.e., `self.val_type`) by 
                passing the value's `__dict__` as keyword arguments. This is useful 
                for verifying that the stored value is valid and can be reconstructed 
                with the expected class initialization.

                If False, values are returned as-is from qt.UserRole without any 
                re-instantiation.

        Notes:
            In workflows where DictTableWidget is used to capture complex value
            types (e.g., dataclasses), it may be necessary to temporarily skip
            post-init behavior to allow users to interactively build up values.
            In such cases, the table may have qt.UserRole entries in the value
            column that may not pass post init checks. Validation or object
            creation can be deferred until a final `to_dict` is called. Setting
            `post_init=True` ensures that these values are checked for validity
            before export or downstream use.
        """
        result = {}

        for row in range(self.table.rowCount):
            key_item = self.table.item(row, 0)
            val_item = self.table.item(row, 1)
            if key_item and val_item:
                key = key_item.text()
                val = val_item.data(qt.Qt.UserRole)

                if post_init and self.val_type is not str:
                    val = self.val_type(**val.__dict__)

                result[key] = val
        return result

    def from_dict(self, data: dict):
        self.table.setRowCount(0)
        for key, val in data.items():
            self._add_row(str(key), val)

class OpenLIFUAbstractDataclassDefinitionFormWidget(qt.QWidget):
    DEFAULT_INT_VALUE = 0
    DEFAULT_INT_RANGE = (-1_000_000, 1_000_000)
    DEFAULT_FLOAT_VALUE = 0.
    DEFAULT_FLOAT_RANGE = (-1e6, 1e6)
    DEFAULT_FLOAT_NUM_DECIMALS = 8

    def __init__(self, cls: Type[Any], parent: Optional[qt.QWidget] = None, is_collapsible: bool = True, collapsible_title: Optional[str] = None):
        """
        Initializes a QWidget containing a form layout with labeled inputs for
        each attribute of an instance created from the specified dataclass. Input
        widgets are generated based on attribute types:

        - int: QSpinBox
        - float: QDoubleSpinBox
        - str: QLineEdit
        - bool: QCheckBox
        - dict: DictTableWidget (2 columns for key-value pairs)
        - Tuple[primitive_type, ...]: Horizontal of widgets for filling out all the values in the tuple
        - Tuple[Tuple[primitive_type] | primitive_type ...]: Vertical container of horizontal containers of widgets for filling out all the values in the nested tuple

        If is_collapsible is True, the form is enclosed in a collapsible container
        with an optional title.

        Args:
            cls: A dataclass (not an instance) whose attributes will populate the form.
            parent: Optional parent widget.
            is_collapsible: Whether to enclose the form in a collapsible container.
            collapsible_title: Optional title for the collapsible section.
        """

        if not inspect.isclass(cls) or cls in (int, float, str, bool, dict, list, tuple, set):
            raise TypeError(f"'cls' must be a user-defined class with type annotations, not a built-in type like {cls.__name__}")

        if not is_dataclass(cls):
            raise TypeError(f"{cls.__name__} is not a dataclass. This class form widget only works for dataclasses.")

        super().__init__(parent)
        self._field_widgets: dict[str, qt.QWidget] = {}
        self._cls = cls

        if is_collapsible:
            # self (QWidget) has a QVBoxLayout layout
            top_level_layout = qt.QVBoxLayout(self)

            # Create collapsible button and add it to the top level layout
            collapsible = ctk.ctkCollapsibleButton()
            collapsible.text = f"Parameters for {cls.__name__}" if collapsible_title is None else collapsible_title
            top_level_layout.addWidget(collapsible)

            # collapsible (ctkCollapsibleButton) has a QVBoxLayout layout
            collapsible_layout = qt.QVBoxLayout(collapsible)

            # Create the inner form widget and add it to the collapsible layout
            form_widget = qt.QWidget()
            form_layout = qt.QFormLayout(form_widget)
            collapsible_layout.addWidget(form_widget)
        else:
            form_layout = qt.QFormLayout(self)

        type_hints = get_hints(cls, include_extras=True)
        dataclass_fields = {f.name: f for f in fields(cls)}

        for name, annotated_type in type_hints.items():

            # Some dataclass fields cannot be initialized
            field_info = dataclass_fields.get(name)
            if field_info is None or not field_info.init:
                continue # Skip fields not meant to be initialized. e.g., openlifu.seg.material.Material.param_ids

            # Now, for each member of cls, create widgets and tooltips
            origin = get_origin(annotated_type)
            args = get_args(annotated_type)

            if origin is Annotated and len(args) > 1:  # If field has Annotated[]
                base_type = args[0]
                metadata = args[1]
                label_text = metadata.name if metadata.name is not None else name
                tooltip_text = metadata.description if metadata.description is not None else f"Write a description for {name}"
            else:  # Field was not Annotated[]
                base_type = annotated_type
                label_text = name
                tooltip_text = f"Write a description for {name}"

            widget = self._create_widget_for_type(base_type)
            if widget:
                label = qt.QLabel(label_text)
                label.setToolTip(tooltip_text)
                widget.setToolTip(tooltip_text)

                form_layout.addRow(label, widget)
                self._field_widgets[name] = widget

    def _create_widget_for_type(self, annotated_type: Any) -> Optional[qt.QWidget]:
        origin = get_origin(annotated_type)
        args = get_args(annotated_type)

        def create_basic_widget(typ: Any) -> Optional[qt.QWidget]:
            if typ is int:
                w = qt.QSpinBox()
                w.setRange(*self.DEFAULT_INT_RANGE)
                w.setValue(self.DEFAULT_INT_VALUE)
                return w
            elif typ is float:
                w = qt.QDoubleSpinBox()
                w.setDecimals(self.DEFAULT_FLOAT_NUM_DECIMALS)
                w.setRange(*self.DEFAULT_FLOAT_RANGE)
                w.setValue(self.DEFAULT_FLOAT_VALUE)
                return w
            elif typ is str:
                return qt.QLineEdit()
            elif typ is bool:
                return qt.QCheckBox()
            elif typ is dict:
                # raw dict does not have origin or args. We assume dict[str,str]
                return DictTableWidget()
            return None

        if origin is None:
            return create_basic_widget(annotated_type)

        if origin is dict:
            if len(args) == 2:
                key_type, val_type = args
                if key_type is str and val_type is str:
                    return DictTableWidget()
                elif key_type is str and hasattr(val_type, "__annotations__"):
                    return DictTableWidget(val_type=val_type)
            return DictTableWidget()

        # non-nested tuple
        if origin is tuple and all(get_origin(t) is None for t in args):
            # if making a form entry for a tuple, it's a container of widgets
            container = qt.QWidget()
            layout = qt.QHBoxLayout(container)
            layout.setContentsMargins(0, 0, 0, 0)

            for typ in args:
                if typ is dict:
                    raise TypeError(f"Invalid tuple field: dict inside a tuple structure not yet supported.")

                widget = create_basic_widget(typ)
                if widget is None:
                    return None  # unsupported tuple element type
                layout.addWidget(widget)

            container.setLayout(layout)
            return container

        # Tuple[Tuple[primitive_type], ...] or Tuple[primitive_type, Tuple[...], ...]
        if origin is tuple:
            container = qt.QWidget()
            layout = qt.QVBoxLayout(container)
            layout.setContentsMargins(0, 0, 0, 0)

            for outer_type in args:
                outer_origin = get_origin(outer_type)
                outer_args = get_args(outer_type)

                # Case: Nested tuple -> horizontal row of widgets
                if outer_origin is tuple:
                    row_widget = qt.QWidget()
                    row_layout = qt.QHBoxLayout(row_widget)
                    row_layout.setContentsMargins(0, 0, 0, 0)

                    for inner_type in outer_args:
                        if inner_type is dict:
                            raise TypeError("Invalid tuple field: dict inside a tuple structure not yet supported.")

                        inner_widget = create_basic_widget(inner_type)
                        if inner_widget is None:
                            return None
                        row_layout.addWidget(inner_widget)

                    layout.addWidget(row_widget)

                # Case: Single type -> single widget in its own row
                else:
                    single_widget = create_basic_widget(outer_type)
                    if single_widget is None:
                        return None
                    layout.addWidget(single_widget)

            container.setLayout(layout)
            return container

        return None # unsupported type

    def update_form_from_values(self, values: dict[str, Any]) -> None:
        """
        Updates form inputs from a dictionary of values.

        Args:
            values: Dictionary mapping attribute names to new values.
        """

        def _set_widget_value(widget: qt.QWidget, value: Any) -> None:
            """
            Helper method to set a value on a widget based on its type, with relaxed type checks.
            """
            if isinstance(widget, qt.QSpinBox) and isinstance(value, int):
                widget.setValue(value)
            elif isinstance(widget, qt.QDoubleSpinBox) and isinstance(value, (int, float)):
                widget.setValue(float(value))
            elif isinstance(widget, qt.QLineEdit):
                widget.setText(str(value))
            elif isinstance(widget, qt.QCheckBox) and isinstance(value, bool):
                widget.setChecked(value)
            elif isinstance(widget, DictTableWidget) and isinstance(value, dict):
                raise TypeError("Invalid tuple field: dict inside a tuple structure not yet supported.")
            else:
                raise TypeError(f"Unsupported widget-value combination: {type(widget)} and {type(value)}")
    
        for name, val in values.items():
            if name not in self._field_widgets:
                continue
            w = self._field_widgets[name]
            if isinstance(w, qt.QSpinBox):
                w.setValue(int(val))
            elif isinstance(w, qt.QDoubleSpinBox):
                w.setValue(float(val))
            elif isinstance(w, qt.QLineEdit):
                w.setText(str(val))
            elif isinstance(w, qt.QCheckBox):
                w.setChecked(bool(val))
            elif isinstance(w, DictTableWidget) and isinstance(val, dict):
                w.from_dict(val)
            elif isinstance(w, qt.QWidget) and isinstance(val, tuple):
                # determine if it's a flat tuple (horizontal row) or a nested one (vertical stack)
                layout = w.layout()
                if isinstance(layout, qt.QHBoxLayout) and layout.count() == len(val):
                    # flat tuple (horizontal row of widgets)
                    for i, item in enumerate(val):
                        child = layout.itemAt(i).widget()
                        _set_widget_value(child, item)

                elif isinstance(layout, qt.QVBoxLayout) and layout.count() == len(val):
                    # nested tuple (vertical stack of rows)
                    for row_idx, row_val in enumerate(val):
                        row_item = layout.itemAt(row_idx)
                        row_widget = row_item.widget()
                        if isinstance(row_val, tuple):
                            # row is a tuple 
                            row_layout = row_widget.layout()
                            if row_layout.count() != len(row_val):
                                continue
                            for col_idx, col_val in enumerate(row_val):
                                child = row_layout.itemAt(col_idx).widget()
                                _set_widget_value(child, col_val)
                                
                        else:
                            # row is a single widget
                            _set_widget_value(row_widget, row_val)

    def get_form_as_dict(self, post_init: bool = True) -> dict[str, Any]:
        """
        Returns the current form values as a dictionary.

        The `post_init` flag is mainly used for DictTableWidgets containing
        complex value types. When True (default), it attempts to reconstruct
        each value using its class constructor to ensure it could be validly
        initialized, before returning its dictionary representation.

        Args:
            post_init (bool): If True, reconstructs values in DictTableWidgets to verify
            they are valid instances of the expected type. Has no effect on simple types
            or non-DictTableWidget fields.
        """        

        def _extract_widget_value(widget: qt.QWidget) -> Any:
            """
            Helper method to get a value from a widget based on its type.
            """
            if isinstance(widget, qt.QSpinBox):
                return widget.value
            elif isinstance(widget, qt.QDoubleSpinBox):
                return widget.value
            elif isinstance(widget, qt.QLineEdit):
                return widget.text
            elif isinstance(widget, qt.QCheckBox):
                return widget.isChecked()
            return None

        values: dict[str, Any] = {}
        for name, w in self._field_widgets.items():
            if isinstance(w, qt.QSpinBox):
                values[name] = w.value
            elif isinstance(w, qt.QDoubleSpinBox):
                values[name] = w.value
            elif isinstance(w, qt.QLineEdit):
                values[name] = w.text
            elif isinstance(w, qt.QCheckBox):
                values[name] = w.isChecked()
            elif isinstance(w, DictTableWidget):
                values[name] = w.to_dict(post_init=post_init)
            elif isinstance(w, qt.QWidget):  # assumed to be flat or nested tuple
                layout = w.layout()
                if isinstance(layout, qt.QHBoxLayout): # flat tuple (hbox)
                    tuple_values = []
                    for i in range(layout.count()):
                        child = layout.itemAt(i).widget()
                        value = _extract_widget_value(child)
                        if value is None:
                            break
                        tuple_values.append(value)
                    else:
                        values[name] = tuple(tuple_values)
                elif isinstance(layout, qt.QVBoxLayout): # nested tuple (vbox)
                    nested_values = []
                    for i in range(layout.count()):
                        row_widget = layout.itemAt(i).widget()
                        if row_widget is None:
                            continue
                        row_layout = row_widget.layout()
                        if isinstance(row_layout, qt.QHBoxLayout):
                            # row is a tuple (hbox)
                            row_values = []
                            for j in range(row_layout.count()):
                                child = row_layout.itemAt(j).widget()
                                value = _extract_widget_value(child)
                                if value is None:
                                    break
                                row_values.append(value)
                            else:
                                nested_values.append(tuple(row_values))
                        else:
                            # row is a singular widget
                            value = _extract_widget_value(row_widget)
                            if value is not None:
                                nested_values.append(value)
                    values[name] = tuple(nested_values)
        return values

    def update_form_from_class(self, instance: Any) -> None:
        """
        Updates the form fields using the attribute values from the provided instance.

        Args:
            instance: An instance of the same class used to create the form.
        """
        values = vars(instance)
        self.update_form_from_values(values)

    def get_form_as_class(self, post_init: bool = True) -> Any:
        """
        Constructs and returns a new instance of the class using the current form values.
        
        Args:
            post_init (bool): If True (default), runs __post_init__. If False, skips it.

        Returns:
            A new instance of the class populated with the form's current values.
        """
        if post_init:
            return self._cls(**self.get_form_as_dict(post_init=post_init))
        else:
            return instantiate_without_post_init(self._cls, **self.get_form_as_dict(post_init=post_init))

    def add_value_changed_signals(self, callback) -> None:
        """
        Connects value change signals of all widgets to a given callback.

        Args:
            callback: Function to call on value change.
        """
        for w in self._field_widgets.values():
            if isinstance(w, qt.QSpinBox):
                w.valueChanged.connect(callback)
            elif isinstance(w, qt.QDoubleSpinBox):
                w.valueChanged.connect(callback)
            elif isinstance(w, qt.QLineEdit):
                w.textChanged.connect(callback)
            elif isinstance(w, qt.QCheckBox):
                w.stateChanged.connect(callback)
            elif isinstance(w, DictTableWidget):
                w.table.itemChanged.connect(lambda *_: callback())
            elif isinstance(w, qt.QWidget): # assumed to be container for tuple
                for child in slicer.util.findChildren(w):
                    if isinstance(child, qt.QSpinBox):
                        child.valueChanged.connect(callback)
                    elif isinstance(child, qt.QDoubleSpinBox):
                        child.valueChanged.connect(callback)
                    elif isinstance(child, qt.QLineEdit):
                        child.textChanged.connect(callback)
                    elif isinstance(child, qt.QCheckBox):
                        child.stateChanged.connect(callback)

    @classmethod
    def modify_widget_spinbox(cls, widget: qt.QWidget, default_value=None, min_value=None, max_value=None, num_decimals=None) -> None:
        """
        Configures a QSpinBox or QDoubleSpinBox widget with specified default, min/max values, and decimal precision.

        Args:
            widget (qt.QWidget): The widget to configure.

        Raises:
            TypeError: If the widget is not a QSpinBox or QDoubleSpinBox.
        """
        if isinstance(widget, qt.QSpinBox):
            if default_value is None:
                default_value = cls.DEFAULT_INT_VALUE
            if min_value is None:
                min_value = cls.DEFAULT_INT_RANGE[0]
            if max_value is None:
                max_value = cls.DEFAULT_INT_RANGE[1]
        elif isinstance(widget, qt.QDoubleSpinBox):
            if default_value is None:
                default_value = cls.DEFAULT_FLOAT_VALUE
            if min_value is None:
                min_value = cls.DEFAULT_FLOAT_RANGE[0]
            if max_value is None:
                max_value = cls.DEFAULT_FLOAT_RANGE[1]
            if num_decimals is None:
                num_decimals = cls.DEFAULT_FLOAT_NUM_DECIMALS
            widget.setDecimals(num_decimals)
        else:
            raise TypeError(
                f"Expected QSpinBox or QDoubleSpinBox, got {type(widget).__name__} instead."
            )

        widget.setRange(min_value, max_value)
        widget.setValue(default_value)

class OpenLIFUAbstractMultipleABCDefinitionFormWidget(qt.QWidget):
    def __init__(self, cls_list: List[Type[Any]], parent: Optional[qt.QWidget] = None, is_collapsible: bool = True, collapsible_title: Optional[str] = None, custom_abc_title: Optional[str] = None):
        """
        Creates a QWidget that allows multiple implementations of an Abstract
        Base Class to be selected, which after selection will display the
        corresponding form widget (through
        OpenLIFUAbstractDataclassDefinitionFormWidget) allowing the specific ABC to
        be configured

        Args:
            cls_list: A list of classes belonging to the same ABC whose attributes will populate the form.
            parent: Optional parent widget.
        """
        if not cls_list:
            raise ValueError("cls_list cannot be empty.")

        self.cls_list = cls_list
        self.base_class_name = cls_list[0].__bases__[0].__name__
        self.custom_abc_title = self.base_class_name if custom_abc_title is None else custom_abc_title
        
        if not all(cls.__bases__[0].__name__ == self.base_class_name for cls in cls_list):
            raise TypeError("All classes in cls_list must share the same base class name.")

        super().__init__(parent)

        top_level_layout = qt.QFormLayout(self)

        self.selector = qt.QComboBox()
        self.forms = qt.QStackedWidget()

        for cls in cls_list:
            self.selector.addItem(cls.__name__)
            self.forms.addWidget(OpenLIFUAbstractDataclassDefinitionFormWidget(cls, parent, is_collapsible, collapsible_title))

        top_level_layout.addRow(qt.QLabel(f"{self.custom_abc_title} type"), self.selector)
        top_level_layout.addRow(qt.QLabel(f"{self.custom_abc_title} options"), self.forms)

        # Connect to custom handler that updates form from class
        self.selector.currentIndexChanged.connect(self._on_index_changed)

    def _on_index_changed(self, index: int) -> None:
        """
        Called when the combo box index changes.
        Updates the stacked widget and populates it with a new default instance
        (we assume that input data is *not* saved across selections between
        derived classes).
        """
        self.forms.setCurrentIndex(index)
        cls = self.cls_list[index]
        instance = cls()  # Default constructor for cls to populate the form
        self.forms.currentWidget().update_form_from_class(instance)

    def update_form_from_class(self, instance_of_derived: Any) -> None:
        """
        Updates the selected form and form fields using the class name of the instance_of_derived as well as the attribute values in the instance.

        Args:
            instance_of_derived: An instance_of_derived of the same class used to create the form.
        """

        # __name__ is an attribute of a class, not an instance.
        index = self.selector.findText(instance_of_derived.__class__.__name__)
        self.selector.setCurrentIndex(index) # also changes the stacked widget through the signal

        self.forms.currentWidget().update_form_from_class(instance_of_derived)

    def get_form_as_class(self, post_init: bool = True) -> Any:
        """
        Constructs and returns a new instance of the derived class using the current form values.

        Returns:
            A new instance of the derived class populated with the form's current values.
        """
        return self.forms.currentWidget().get_form_as_class(post_init)


    def add_value_changed_signals(self, callback) -> None:
        """
        Connects value change signals of all widgets in all forms to a given
        callback.

        Args:
            callback: Function to call on value change.
        """
        self.selector.currentIndexChanged.connect(callback)
        for w_idx in range(self.forms.count):
            form = self.forms.widget(w_idx)
            form.add_value_changed_signals(callback)

class CreateAbstractClassDialog(qt.QDialog):
    """
    Dialog for creating a custom object, where the class is entered
    through the form generated by OpenLIFUAbstractDataclassDefinitionFormWidget;
    ideal for adding custom objects into lists
    """

    def __init__(self, object_name: str, object_type: Type, parent="mainWindow"):
        """
        Args:
            object_name (str): Label for the object input field.
            object_type (Type): Class type used to generate the form for object input.
            parent (QWidget or str): Parent widget or "mainWindow". Defaults to "mainWindow".
        """
        super().__init__(slicer.util.mainWindow() if parent == "mainWindow" else parent)
        self.setWindowTitle(f"Add {object_name}")
        self.setWindowModality(qt.Qt.ApplicationModal)
        self.object_name = object_name
        self.object_type = object_type
        self.setup()

    def setup(self):
        self.setMinimumWidth(300)
        self.setContentsMargins(15, 15, 15, 15)

        top_level_layout = qt.QFormLayout(self)
        top_level_layout.setSpacing(10)

        self.object_input = OpenLIFUAbstractDataclassDefinitionFormWidget(self.object_type, parent=self, is_collapsible=False)
        top_level_layout.addRow(_(f"{self.object_name}:"), self.object_input)

        self.buttonBox = qt.QDialogButtonBox()
        self.buttonBox.setStandardButtons(qt.QDialogButtonBox.Ok |
                                          qt.QDialogButtonBox.Cancel)
        top_level_layout.addWidget(self.buttonBox)

        self.buttonBox.rejected.connect(self.reject)
        self.buttonBox.accepted.connect(self.validateInputs)

    def validateInputs(self):
        """
        Ensure object is valid
        """
        typed_object = self.object_input.get_form_as_class()

        if typed_object is None:
            raise ValueError(f"{self.object_name} field cannot be None.")

        self.accept()

    def customexec_(self):
        returncode = self.exec_()
        if returncode == qt.QDialog.Accepted:
            return (returncode, self.object_input.get_form_as_class())
        return (returncode, None)
