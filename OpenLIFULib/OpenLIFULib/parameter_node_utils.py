"""Some of the underlying parameter node infrastructure"""

from typing import TYPE_CHECKING, Optional, Any
import numpy as np
import slicer
from slicer.parameterNodeWrapper import (
    parameterNodeSerializer,
    Serializer,
    ValidatedSerializer,
    validators,
)
from slicer.parameterNodeWrapper.serializers import createSerializerFromAnnotatedType
import zlib
import io
import base64
from OpenLIFULib.lazyimport import openlifu_lz, xarray_lz

if TYPE_CHECKING:
    import openlifu # This import is deferred at runtime, but it is done here for IDE and static analysis purposes
    import openlifu.db
    import xarray


# This very thin wrapper around openlifu.Protocol is needed to do our lazy importing of openlifu
# while still providing type annotations that the parameter node wrapper can use.
# If we tried to make openlifu.Protocol directly supported as a type by parameter nodes, we would
# get errors from parameterNodeWrapper as it tries to use typing.get_type_hints. This fails because
# get_type_hints tries to *evaluate* the type annotations like "openlifu.Protocol" possibly before
# the user has installed openlifu, and possibly before the main window widgets exist that would allow
# an install prompt to even show up.
class SlicerOpenLIFUProtocol:
    """Ultrathin wrapper of openlifu.Protocol. This exists so that protocols can have parameter node
    support while we still do lazy-loading of openlifu."""
    def __init__(self, protocol: "Optional[openlifu.Protocol]" = None):
        self.protocol = protocol

# For the same reason we have a thin wrapper around openlifu.Transducer. But the name SlicerOpenLIFUTransducer
# is reserved for the upcoming parameter pack.
class SlicerOpenLIFUTransducerWrapper:
    """Ultrathin wrapper of openlifu.Transducer. This exists so that transducers can have parameter node
    support while we still do lazy-loading of openlifu."""
    def __init__(self, transducer: "Optional[openlifu.Transducer]" = None):
        self.transducer = transducer

# For the same reason we have a thin wrapper around openlifu.Point
class SlicerOpenLIFUPoint:
    """Ultrathin wrapper of openlifu.Point. This exists so that points can have parameter node
    support while we still do lazy-loading of openlifu."""
    def __init__(self, point: "Optional[openlifu.Point]" = None):
        self.point = point

# For the same reason we have a thin wrapper around openlifu.Session
class SlicerOpenLIFUSessionWrapper:
    """Ultrathin wrapper of openlifu.Session. This exists so that sessions can have parameter node
    support while we still do lazy-loading of openlifu."""
    def __init__(self, session: "Optional[openlifu.db.Session]" = None):
        self.session = session

# For the same reason we have a thin wrapper around xarray.Dataset
class SlicerOpenLIFUXADataset:
    """Ultrathin wrapper of xarray.Dataset, so that it can have parameter node
    support while we still do lazy-loading of xarray (a dependency that is installed alongside openlifu)."""
    def __init__(self, dataset: "Optional[xarray.Dataset]" = None):
        self.dataset = dataset

def SlicerOpenLIFUSerializerBaseMaker(
        serialized_type:type,
        default_args:Optional[list[Any]] = None,
        default_kwargs:Optional[dict[Any,Any]] = None,
    ) -> type[Serializer]:
    """Factory for parameter node serializer base class to handle boilerplate aspects of
    the implementation of a serializer.

    Args:
        serialized_type: The type that is being serialized. To check whether an object can be
            serialized with this serializer, the object's type is compared with this type.
        default_args: args list to pass into the constructor of the serialized type to construct
            a default object. If None then no args are passed.
        default_kwargs: kwargs dict to pass into the constructor of the serialized type to construct
            a default object. If None then no kwargs are passed.

    Returns: An abstract base class deriving from Serializer which has implementations of boilerplate
        methods in place. Only read and write methods need to be implemented from here.

    """
    if default_args is None:
        default_args = []
    if default_kwargs is None:
        default_kwargs = {}
    class SlicerOpenLIFUSerializerBase(Serializer):
        @staticmethod
        def canSerialize(type_) -> bool:
            """
            Whether the serializer can serialize the given type if it is properly instantiated.
            """
            return type_ == serialized_type

        @classmethod
        def create(cls, type_):
            """
            Creates a new serializer object based on the given type. If this class does not support the given type,
            None is returned.
            """
            if SlicerOpenLIFUSerializerBase.canSerialize(type_):
                # Add custom validators as we need them to the list here. For now just IsInstance.
                return ValidatedSerializer(cls(), [validators.IsInstance(serialized_type)])
            return None

        def default(self):
            """
            The default value to use if another default is not specified.
            """
            return serialized_type(*default_args, **default_kwargs)

        def isIn(self, parameterNode: slicer.vtkMRMLScriptedModuleNode, name: str) -> bool:
            """
            Whether the parameterNode contains a parameter of the given name.
            Note that most implementations can just use parameterNode.HasParameter(name).
            """
            return parameterNode.HasParameter(name)

        def remove(self, parameterNode: slicer.vtkMRMLScriptedModuleNode, name: str) -> None:
            """
            Removes the value of the given name from the parameterNode.
            """
            parameterNode.UnsetParameter(name)
    return SlicerOpenLIFUSerializerBase

@parameterNodeSerializer
class OpenLIFUProtocolSerializer(SlicerOpenLIFUSerializerBaseMaker(SlicerOpenLIFUProtocol)):
    def write(self, parameterNode: slicer.vtkMRMLScriptedModuleNode, name: str, value: SlicerOpenLIFUProtocol) -> None:
        """
        Writes the value to the parameterNode under the given name.
        """
        parameterNode.SetParameter(
            name,
            value.protocol.to_json(compact=True)
        )

    def read(self, parameterNode: slicer.vtkMRMLScriptedModuleNode, name: str) -> SlicerOpenLIFUProtocol:
        """
        Reads and returns the value with the given name from the parameterNode.
        """
        json_string = parameterNode.GetParameter(name)
        return SlicerOpenLIFUProtocol(openlifu_lz().Protocol.from_json(json_string))

@parameterNodeSerializer
class OpenLIFUTransducerSerializer(SlicerOpenLIFUSerializerBaseMaker(SlicerOpenLIFUTransducerWrapper)):
    def write(self, parameterNode: slicer.vtkMRMLScriptedModuleNode, name: str, value: SlicerOpenLIFUTransducerWrapper) -> None:
        """
        Writes the value to the parameterNode under the given name.
        """
        parameterNode.SetParameter(
            name,
            value.transducer.to_json(compact=True)
        )

    def read(self, parameterNode: slicer.vtkMRMLScriptedModuleNode, name: str) -> SlicerOpenLIFUTransducerWrapper:
        """
        Reads and returns the value with the given name from the parameterNode.
        """
        json_string = parameterNode.GetParameter(name)
        return SlicerOpenLIFUTransducerWrapper(openlifu_lz().Transducer.from_json(json_string))

@parameterNodeSerializer
class OpenLIFUSessionSerializer(SlicerOpenLIFUSerializerBaseMaker(SlicerOpenLIFUSessionWrapper)):
    def write(self, parameterNode: slicer.vtkMRMLScriptedModuleNode, name: str, value: SlicerOpenLIFUSessionWrapper) -> None:
        parameterNode.SetParameter(
            name,
            value.session.to_json(compact=True)
        )

    def read(self, parameterNode: slicer.vtkMRMLScriptedModuleNode, name: str) -> SlicerOpenLIFUSessionWrapper:
        json_string = parameterNode.GetParameter(name)
        return SlicerOpenLIFUSessionWrapper(openlifu_lz().db.Session.from_json(json_string))

@parameterNodeSerializer
class OpenLIFUPointSerializer(SlicerOpenLIFUSerializerBaseMaker(SlicerOpenLIFUPoint)):
    def write(self, parameterNode: slicer.vtkMRMLScriptedModuleNode, name: str, value: SlicerOpenLIFUPoint) -> None:
        """
        Writes the value to the parameterNode under the given name.
        """
        parameterNode.SetParameter(
            name,
            value.point.to_json(compact=True)
        )

    def read(self, parameterNode: slicer.vtkMRMLScriptedModuleNode, name: str) -> SlicerOpenLIFUPoint:
        """
        Reads and returns the value with the given name from the parameterNode.
        """
        json_string = parameterNode.GetParameter(name)
        return SlicerOpenLIFUPoint(openlifu_lz().Point.from_json(json_string))

@parameterNodeSerializer
class XarraydatasetSerializer(SlicerOpenLIFUSerializerBaseMaker(SlicerOpenLIFUXADataset)):
    def write(self, parameterNode: slicer.vtkMRMLScriptedModuleNode, name: str, value: SlicerOpenLIFUXADataset) -> None:
        """
        Writes the value to the parameterNode under the given name.
        """
        ds = value.dataset
        ds_serialized = base64.b64encode(ds.to_netcdf()).decode('utf-8')
        parameterNode.SetParameter(
            name,
            ds_serialized,
        )

    def read(self, parameterNode: slicer.vtkMRMLScriptedModuleNode, name: str) -> SlicerOpenLIFUXADataset:
        """
        Reads and returns the value with the given name from the parameterNode.
        """
        ds_serialized = parameterNode.GetParameter(name)
        ds_deserialized = xarray_lz().open_dataset(base64.b64decode(ds_serialized.encode('utf-8')))
        return SlicerOpenLIFUXADataset(ds_deserialized)

@parameterNodeSerializer
class NumpyArraySerializer(Serializer):
    @staticmethod
    def canSerialize(type_) -> bool:
        """
        Whether the serializer can serialize the given type if it is properly instantiated.
        """
        return type_ == np.ndarray

    @staticmethod
    def create(type_):
        """
        Creates a new serializer object based on the given type. If this class does not support the given type,
        None is returned.
        """
        if NumpyArraySerializer.canSerialize(type_):
            # Add custom validators as we need them to the list here. For now just IsInstance.
            return ValidatedSerializer(NumpyArraySerializer(), [validators.IsInstance(np.ndarray)])
        return None

    def default(self):
        """
        The default value to use if another default is not specified.
        """
        return np.array([])

    def isIn(self, parameterNode: slicer.vtkMRMLScriptedModuleNode, name: str) -> bool:
        """
        Whether the parameterNode contains a parameter of the given name.
        Note that most implementations can just use parameterNode.HasParameter(name).
        """
        return parameterNode.HasParameter(name)

    def write(self, parameterNode: slicer.vtkMRMLScriptedModuleNode, name: str, value: np.ndarray) -> None:
        """
        Writes the value to the parameterNode under the given name.
        """
        buffer = io.BytesIO()
        np.save(buffer, value)
        array_serialized = base64.b64encode(zlib.compress(buffer.getvalue())).decode('utf-8')
        parameterNode.SetParameter(
            name,
            array_serialized,
        )

    def read(self, parameterNode: slicer.vtkMRMLScriptedModuleNode, name: str) -> np.ndarray:
        """
        Reads and returns the value with the given name from the parameterNode.
        """
        array_serialized = parameterNode.GetParameter(name)
        array_deserialized = np.load(io.BytesIO(zlib.decompress(base64.b64decode(array_serialized.encode('utf-8')))))
        return array_deserialized

    def remove(self, parameterNode: slicer.vtkMRMLScriptedModuleNode, name: str) -> None:
        """
        Removes the value of the given name from the parameterNode.
        """
        parameterNode.UnsetParameter(name)

@parameterNodeSerializer
class NamedTupleSerializer(Serializer):
    """Serializer for NamedTuple largely copied from slicer.util.parameterNodeWrapper.serializers.TupleSerializer.
    """
    @staticmethod
    def canSerialize(type_) -> bool:
        return issubclass(type_, tuple) and hasattr(type_, '_fields') and callable(type_) and isinstance(type_,type)

    @staticmethod
    def create(type_):
        if NamedTupleSerializer.canSerialize(type_):
            args = tuple(type_.__annotations__[f] for f in type_._fields)
            if len(args) == 0:
                raise Exception("Unsure how to handle a typed tuple with no discernible type")
            serializers = [createSerializerFromAnnotatedType(arg) for arg in args]
            return NamedTupleSerializer(serializers, type_)
        return None

    def __init__(self, serializers, cls):
        self._len = len(serializers)
        self._serializers = serializers
        self._fields = cls._fields
        self._cls = cls

    def default(self):
        return self._cls(**{f:s.default() for f,s in zip(self._fields,self._serializers)})

    @staticmethod
    def _paramName(name, field):
        return f"{name}.{field}"

    def isIn(self, parameterNode, name: str) -> bool:
        return self._serializers[0].isIn(parameterNode, self._paramName(name, self._fields[0]))

    def write(self, parameterNode, name: str, value) -> None:
        with slicer.util.NodeModify(parameterNode):
            for field, serializer in zip(self._fields, self._serializers):
                serializer.write(parameterNode, self._paramName(name, field), getattr(value,field))

    def read(self, parameterNode, name: str):
        return self._cls(
            **{
                field : serializer.read(parameterNode, self._paramName(name, field))
                for field, serializer in zip(self._fields, self._serializers)
            }
        )

    def remove(self, parameterNode, name: str) -> None:
        with slicer.util.NodeModify(parameterNode):
            for field, serializer in zip(self._fields,self._serializers):
                serializer.remove(parameterNode, self._paramName(name, field))

    def supportsCaching(self):
        return all([s.supportsCaching() for s in self._serializers])