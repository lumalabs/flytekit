from __future__ import annotations

import asyncio
import json
import os
import pathlib
import random
import typing
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Dict, Generator, Tuple
from uuid import UUID

import fsspec
import msgpack
from dataclasses_json import DataClassJsonMixin, config
from fsspec.utils import get_protocol
from google.protobuf import json_format as _json_format
from google.protobuf.struct_pb2 import Struct
from marshmallow import fields
from mashumaro.types import SerializableType

from flytekit.core.constants import MESSAGEPACK
from flytekit.core.context_manager import FlyteContext, FlyteContextManager
from flytekit.core.type_engine import (
    AsyncTypeTransformer,
    TypeEngine,
    TypeTransformerFailedError,
    get_batch_size,
)
from flytekit.exceptions.user import FlyteAssertion
from flytekit.extras.pydantic_transformer.decorator import (
    model_serializer,
    model_validator,
)
from flytekit.models import types as _type_models
from flytekit.models.core import types as _core_types
from flytekit.models.core.types import BlobType
from flytekit.models.literals import Binary, Blob, BlobMetadata, Literal, Scalar
from flytekit.models.types import LiteralType
from flytekit.types.file import FileExt, FlyteFile
from flytekit.utils.asyn import loop_manager

T = typing.TypeVar("T")
PathType = typing.Union[str, os.PathLike]


def noop(): ...


@dataclass
class FlyteDirectory(SerializableType, DataClassJsonMixin, os.PathLike, typing.Generic[T]):
    path: PathType = field(default=None, metadata=config(mm_field=fields.String()))  # type: ignore
    """
    > [!WARNING]
    > This class should not be used on very large datasets, as merely listing the dataset will cause
        the entire dataset to be downloaded. Listing on S3 and other backend object stores is not consistent
        and we should not need data to be downloaded to list.

    Please first read through the comments on the {{< py_class_ref flytekit.types.file.FlyteFile >}} class as the
    implementation here is similar.

    One thing to note is that the ``os.PathLike`` type that comes with Python was used as a stand-in for ``FlyteFile``.
    That is, if a task's output signature is an ``os.PathLike``, Flyte takes that to mean ``FlyteFile``. There is no
    easy way to distinguish an ``os.PathLike`` where the user means a File and where the user means a Directory. As
    such, if you want to use a directory, you must declare all types as ``FlyteDirectory``. You'll still be able to
    return a string literal though instead of a full-fledged ``FlyteDirectory`` object assuming the str is a directory.

    **Converting from a Flyte literal value to a Python instance of FlyteDirectory**

    +-----------------------------+------------------------------------------------------------------------------------+
    | Type of Flyte IDL Literal   |    FlyteDirectory                                                                  |
    +=============+===============+====================================================================================+
    | Multipart   | uri matches   | FlyteDirectory object stores the original string                                   |
    | Blob        | http(s)/s3/gs | path, but points to a local file instead.                                          |
    |             |               |                                                                                    |
    |             |               | * [fn] downloader: function that writes to path when open'ed.                      |
    |             |               | * [fn] download: will trigger download                                             |
    |             |               | * path: randomly generated local path that will not exist until downloaded         |
    |             |               | * remote_path: None                                                                |
    |             |               | * remote_source: original http/s3/gs path                                          |
    |             |               |                                                                                    |
    |             +---------------+------------------------------------------------------------------------------------+
    |             | uri matches   | FlyteDirectory object just wraps the string                                        |
    |             | /local/path   |                                                                                    |
    |             |               | * [fn] downloader: noop function                                                   |
    |             |               | * [fn] download: raises exception                                                  |
    |             |               | * path: just the given path                                                        |
    |             |               | * remote_path: None                                                                |
    |             |               | * remote_source: None                                                              |
    +-------------+---------------+------------------------------------------------------------------------------------+

    -----------

    **Converting from a Python value (FlyteDirectory, str, or pathlib.Path) to a Flyte literal**

    +-----------------------------------+------------------------------------------------------------------------------+
    | Type of Python value              | FlyteDirectory                                                               |
    +===================+===============+==============================================================================+
    | str or            | path matches  | Blob object is returned with uri set to the given path.                      |
    | pathlib.Path or   | http(s)/s3/gs | Nothing is uploaded.                                                         |
    | FlyteDirectory    +---------------+------------------------------------------------------------------------------+
    |                   | path matches  | Contents of file are uploaded to the Flyte blob store (S3, GCS, etc.), in    |
    |                   | /local/path   | a bucket determined by the raw_output_data_prefix setting. If                |
    |                   |               | remote_path is given, then that is used instead of the random path. Blob     |
    |                   |               | object is returned with uri pointing to the blob store location.             |
    |                   |               |                                                                              |
    +-------------------+---------------+------------------------------------------------------------------------------+

    As inputs ::

        def t1(in1: FlyteDirectory):
            ...

        def t1(in1: FlyteDirectory["svg"]):
            ...

    As outputs:

    The contents of this local directory will be uploaded to the Flyte store. ::

        return FlyteDirectory("/path/to/dir/")

        return FlyteDirectory["svg"]("/path/to/dir/", remote_path="s3://special/output/location")

    Similar to the FlyteFile example, if you give an already remote location, it will not be copied to Flyte's
    durable store, the uri will just be stored as is. ::

        return FlyteDirectory("s3://some/other/folder")

    Note if you write a path starting with http/s, if anything ever tries to read it (i.e. use the literal
    as an input, it'll fail because the http proxy doesn't know how to download whole directories.

    The format [] bit is still there because in Flyte, directories are stored as Blob Types also, just like files, and
    the Blob type has the format field. The difference in the type field is represented in the ``dimensionality``
    field in the ``BlobType``.
    """

    def _serialize(self) -> typing.Dict[str, str]:
        lv = FlyteDirToMultipartBlobTransformer().to_literal(
            FlyteContextManager.current_context(), self, type(self), None
        )
        return {"path": lv.scalar.blob.uri}

    @classmethod
    def _deserialize(cls, value) -> "FlyteDirectory":
        return FlyteDirToMultipartBlobTransformer().dict_to_flyte_directory(dict_obj=value, expected_python_type=cls)

    @model_serializer
    def serialize_flyte_dir(self) -> Dict[str, str]:
        lv = FlyteDirToMultipartBlobTransformer().to_literal(
            FlyteContextManager.current_context(), self, type(self), None
        )
        return {"path": lv.scalar.blob.uri}

    @model_validator(mode="after")
    def deserialize_flyte_dir(self, info) -> FlyteDirectory:
        if info.context is None or info.context.get("deserialize") is not True:
            return self

        pv = FlyteDirToMultipartBlobTransformer().to_python_value(
            FlyteContextManager.current_context(),
            Literal(
                scalar=Scalar(
                    blob=Blob(
                        metadata=BlobMetadata(
                            type=_core_types.BlobType(
                                format="", dimensionality=_core_types.BlobType.BlobDimensionality.MULTIPART
                            )
                        ),
                        uri=self.path,
                    )
                )
            ),
            type(self),
        )
        return pv

    def __init__(
        self,
        path: typing.Union[str, os.PathLike],
        downloader: typing.Optional[typing.Callable] = None,
        remote_directory: typing.Optional[typing.Union[os.PathLike, str, typing.Literal[False]]] = None,
    ):
        """
        :param path: The source path that users are expected to call open() on
        :param downloader: Optional function that can be passed that used to delay downloading of the actual fil
            until a user actually calls open().
        :param remote_directory: If the user wants to return something and also specify where it should be uploaded to.
            If set to False, then flytekit will not upload the directory to the remote store.
        """
        # Make this field public, so that the dataclass transformer can set a value for it
        # https://github.com/flyteorg/flytekit/blob/bcc8541bd6227b532f8462563fe8aac902242b21/flytekit/core/type_engine.py#L298
        self.path = path
        self._downloader = downloader or noop
        self._downloaded = False
        self._remote_directory = remote_directory
        self._remote_source: typing.Optional[str] = None

    def __fspath__(self):
        """
        This function should be called by os.listdir as well.
        """
        if not self._downloaded:
            if isinstance(self._downloader, partial):
                underlying_func = self._downloader.func
            else:
                underlying_func = self._downloader

            if asyncio.iscoroutinefunction(underlying_func):
                # If the downloader is a coroutine function, we need to run it in the event loop.
                # This is required when using the Elastic task config with the start method set to 'spawn'.
                # This is possibly due to pickling and unpickling the task function, which may not properly rebind to the synced version.
                loop_manager.synced(self._downloader)()
            else:
                # If the downloader is not a coroutine, we can just call it directly.
                self._downloader()
            self._downloaded = True
        return self.path

    @classmethod
    def extension(cls) -> str:
        return ""

    @classmethod
    def new_remote(cls, stem: typing.Optional[str] = None, alt: typing.Optional[str] = None) -> FlyteDirectory:
        """
        Create a new FlyteDirectory object using the currently configured default remote in the context (i.e.
        the raw_output_prefix configured in the current FileAccessProvider object in the context).
        This is used if you explicitly have a folder somewhere that you want to create files under.
        If you want to write a whole folder, you can let your task return a FlyteDirectory object,
        and let flytekit handle the uploading.

        :param stem: A stem to append to the path as the final prefix "directory".
        :param alt: An alternate first member of the prefix to use instead of the default.
        :return FlyteDirectory: A new FlyteDirectory object that points to a remote location.
        """
        ctx = FlyteContextManager.current_context()
        if stem and Path(stem).suffix:
            raise ValueError("Stem should not have a file extension.")
        remote_path = ctx.file_access.generate_new_custom_path(alt=alt, stem=stem)
        return cls(path=remote_path)

    @classmethod
    def new(cls, dirname: str | os.PathLike) -> FlyteDirectory:
        """
        Create a new FlyteDirectory object in current Flyte working directory.
        """

        if os.path.isabs(dirname):
            raise ValueError("Path should be relative.")

        ctx = FlyteContextManager.current_context()

        path = os.path.join(ctx.user_space_params.working_directory, dirname)

        os.makedirs(path, exist_ok=False)

        return cls(path=path)

    def __class_getitem__(cls, item: typing.Union[typing.Type, str]) -> typing.Type[FlyteDirectory]:
        if item is None:
            return cls

        item_string = FileExt.check_and_convert_to_str(item)

        item_string = item_string.strip().lstrip("~").lstrip(".")
        if item_string == "":
            return cls

        class _SpecificFormatDirectoryClass(FlyteDirectory):
            # Get the type engine to see this as kind of a generic
            __origin__ = FlyteDirectory

            class AttributeHider:
                def __get__(self, instance, owner):
                    raise AttributeError(
                        """We have to return false in hasattr(cls, "__class_getitem__") to make mashumaro deserialize FlyteDirectory correctly."""
                    )

            # Set __class_getitem__ to AttributeHider to make mashumaro deserialize FlyteDirectory correctly
            # https://stackoverflow.com/questions/6057130/python-deleting-a-class-attribute-in-a-subclass/6057409
            # Since mashumaro will use the method __class_getitem__ and __origin__ to construct the dataclass back
            # https://github.com/Fatal1ty/mashumaro/blob/e945ee4319db49da9f7b8ede614e988cc8c8956b/mashumaro/core/meta/helpers.py#L300-L303
            __class_getitem__ = AttributeHider()  # type: ignore

            @classmethod
            def extension(cls) -> str:
                return item_string

        return _SpecificFormatDirectoryClass

    @property
    def downloaded(self) -> bool:
        return self._downloaded

    @property
    def remote_directory(self) -> typing.Optional[typing.Union[os.PathLike, bool, str]]:
        return self._remote_directory

    @property
    def sep(self) -> str:
        if os.name == "nt" and get_protocol(self.path or self.remote_source or self.remote_directory) == "file":
            return "\\"
        return "/"

    @property
    def remote_source(self) -> str:
        """
        If this is an input to a task, and the original path is s3://something, flytekit will download the
        directory for the user. In case the user wants access to the original path, it will be here.
        """
        return typing.cast(str, self._remote_source)

    def new_file(self, name: typing.Optional[str] = None) -> FlyteFile:
        """
        This will create a new file under the current folder.
        If given a name, it will use the name given, otherwise it'll pick a random string.
        Collisions are not checked.
        """
        # TODO we may want to use - https://github.com/fsspec/universal_pathlib
        if not name:
            name = UUID(int=random.getrandbits(128)).hex
        new_path = self.sep.join([str(self.path).rstrip(self.sep), name])  # trim trailing sep if any and join
        return FlyteFile(path=new_path)

    def new_dir(self, name: typing.Optional[str] = None) -> FlyteDirectory:
        """
        This will create a new folder under the current folder.
        If given a name, it will use the name given, otherwise it'll pick a random string.
        Collisions are not checked.
        """
        if not name:
            name = UUID(int=random.getrandbits(128)).hex

        new_path = self.sep.join([str(self.path).rstrip(self.sep), name])  # trim trailing sep if any and join
        return FlyteDirectory(path=new_path)

    @classmethod
    def from_source(cls, source: str | os.PathLike) -> FlyteDirectory:
        """
        Create a new FlyteDirectory object with the remote source set to the input
        """
        ctx = FlyteContextManager.current_context()
        lit = Literal(
            scalar=Scalar(
                blob=Blob(
                    metadata=BlobMetadata(
                        type=BlobType(format="", dimensionality=BlobType.BlobDimensionality.MULTIPART)
                    ),
                    uri=source,
                )
            )
        )
        t = FlyteDirToMultipartBlobTransformer()
        return t.to_python_value(ctx, lit, cls)

    def download(self) -> str:
        return self.__fspath__()

    @classmethod
    def listdir(cls, directory: FlyteDirectory) -> typing.List[typing.Union[FlyteDirectory, FlyteFile]]:
        """
        This function will list all files and folders in the given directory, but without downloading the contents.
        In addition, it will return a list of FlyteFile and FlyteDirectory objects that have ability to lazily download the
        contents of the file/folder. For example:

        ```python
        entity = FlyteDirectory.listdir(directory)
        for e in entity:
            print("s3 object:", e.remote_source)
            # s3 object: s3://test-flytedir/file1.txt
            # s3 object: s3://test-flytedir/file2.txt
            # s3 object: s3://test-flytedir/sub_dir

        open(entity[0], "r")  # This will download the file to the local disk.
        open(entity[0], "r")  # flytekit will read data from the local disk if you open it again.
        ```
        """

        final_path = directory.path
        if directory.remote_source:
            final_path = directory.remote_source
        elif directory.remote_directory:
            final_path = typing.cast(os.PathLike, directory.remote_directory)

        paths: typing.List[typing.Union[FlyteDirectory, FlyteFile]] = []
        file_access = FlyteContextManager.current_context().file_access
        if not file_access.is_remote(final_path):
            for p in os.listdir(final_path):
                joined_path = os.path.join(final_path, p)
                if os.path.isfile(joined_path):
                    paths.append(FlyteFile(joined_path))
                else:
                    paths.append(FlyteDirectory(joined_path))
            return paths

        fs = file_access.get_filesystem_for_path(final_path)
        for key in fs.listdir(final_path):
            remote_path = os.path.join(final_path, key["name"].split(os.sep)[-1])
            if key["type"] == "file":
                local_path = file_access.get_random_local_path()
                os.makedirs(pathlib.Path(local_path).parent, exist_ok=True)
                downloader = partial(file_access.get_data, remote_path, local_path, is_multipart=False)

                flyte_file: FlyteFile = FlyteFile(local_path, downloader=downloader)
                flyte_file._remote_source = remote_path
                paths.append(flyte_file)
            else:
                local_folder = file_access.get_random_local_directory()
                downloader = partial(file_access.get_data, remote_path, local_folder, is_multipart=True)

                flyte_directory: FlyteDirectory = FlyteDirectory(path=local_folder, downloader=downloader)
                flyte_directory._remote_source = remote_path
                paths.append(flyte_directory)

        return paths

    def crawl(
        self, maxdepth: typing.Optional[int] = None, topdown: bool = True, **kwargs
    ) -> Generator[Tuple[typing.Union[str, os.PathLike[Any]], typing.Dict[Any, Any]], None, None]:
        """
        Crawl returns a generator of all files prefixed by any sub-folders under the given "FlyteDirectory".
        if details=True is passed, then it will return a dictionary as specified by fsspec.

        Example:

            >>> list(fd.crawl())
            [("/base", "file1"), ("/base", "dir1/file1"), ("/base", "dir2/file1"), ("/base", "dir1/dir/file1")]

            >>> list(x.crawl(detail=True))
            [('/tmp/test', {'my-dir/ab.py': {'name': '/tmp/test/my-dir/ab.py', 'size': 0, 'type': 'file',
             'created': 1677720780.2318847, 'islink': False, 'mode': 33188, 'uid': 501, 'gid': 0,
              'mtime': 1677720780.2317934, 'ino': 1694329, 'nlink': 1}})]
        """
        final_path = self.path
        if self.remote_source:
            final_path = self.remote_source
        elif self.remote_directory:
            final_path = typing.cast(os.PathLike, self.remote_directory)
        ctx = FlyteContextManager.current_context()
        fs = ctx.file_access.get_filesystem_for_path(final_path)
        base_path_len = len(fsspec.core.strip_protocol(final_path)) + 1  # Add additional `/` at the end
        for base, _, files in fs.walk(final_path, maxdepth, topdown, **kwargs):
            current_base = base[base_path_len:]
            if isinstance(files, dict):
                for f, v in files.items():
                    yield final_path, {os.path.join(current_base, f): v}
            else:
                for f in files:
                    yield final_path, os.path.join(current_base, f)

    def __repr__(self):
        return str(self.path)

    def __str__(self):
        return str(self.path)

    def __truediv__(self, other: str | os.PathLike) -> Path:
        """
        This is a convenience method to allow for easy concatenation of paths.
        """

        return Path(self.path) / other


class FlyteDirToMultipartBlobTransformer(AsyncTypeTransformer[FlyteDirectory]):
    """
    This transformer handles conversion between the Python native FlyteDirectory class defined above, and the Flyte
    IDL literal/type of Multipart Blob. Please see the FlyteDirectory comments for additional information.

    > [!CAUTION] caution:
    > The transformer will not check if the given path is actually a directory. This is because the path could be
       a remote reference.

    """

    def __init__(self):
        super().__init__(name="FlyteDirectory", t=FlyteDirectory)

    @staticmethod
    def get_format(t: typing.Type[FlyteDirectory]) -> str:
        return t.extension()

    @staticmethod
    def _blob_type(format: str) -> _core_types.BlobType:
        return _core_types.BlobType(format=format, dimensionality=_core_types.BlobType.BlobDimensionality.MULTIPART)

    def assert_type(self, t: typing.Type[FlyteDirectory], v: typing.Union[FlyteDirectory, os.PathLike, str]):
        if isinstance(v, FlyteDirectory) or isinstance(v, str) or isinstance(v, os.PathLike):
            """
            >[!NOTE]
            > we do not do a isdir check because the given path could be remote reference
            """
            return
        raise TypeError(
            f"No automatic conversion from {type(v)} declared type {t} to FlyteDirectory found."
            f" Use (FlyteDirectory, str, os.PathLike)"
        )

    def get_literal_type(self, t: typing.Type[FlyteDirectory]) -> LiteralType:
        return _type_models.LiteralType(blob=self._blob_type(format=FlyteDirToMultipartBlobTransformer.get_format(t)))

    async def async_to_literal(
        self,
        ctx: FlyteContext,
        python_val: FlyteDirectory,
        python_type: typing.Type[FlyteDirectory],
        expected: LiteralType,
    ) -> Literal:
        remote_directory = None
        should_upload = True
        batch_size = get_batch_size(python_type)

        meta = BlobMetadata(type=self._blob_type(format=self.get_format(python_type)))

        # There are two kinds of literals we handle, either an actual FlyteDirectory, or a string path to a directory.
        # Handle the FlyteDirectory case
        if isinstance(python_val, FlyteDirectory):
            # If the object has a remote source, then we just convert it back.
            if python_val._remote_source is not None:
                return Literal(scalar=Scalar(blob=Blob(metadata=meta, uri=python_val._remote_source)))

            source_path = str(python_val.path)
            # If the user supplied a pathlike value, then the directory does need to be uploaded. However, don't upload
            # the directory in the following circumstances:
            # - If the user specified the remote_directory to be False.
            # - If the path given is already a remote path, say https://www.google.com, uploading the Flyte
            #   blob store doesn't make sense.
            if not isinstance(python_val.remote_directory, (pathlib.Path, str)) and (
                python_val.remote_directory is False
                or ctx.file_access.is_remote(source_path)
                or ctx.execution_state.is_local_execution()
            ):
                should_upload = False

            # Set the remote destination if one was given instead of triggering a random one below
            remote_directory = python_val.remote_directory or None

        # Handle the string case
        elif isinstance(python_val, (pathlib.Path, str)):
            source_path = str(python_val)

            if ctx.file_access.is_remote(source_path):
                should_upload = False
            else:
                p = Path(source_path)
                if not p.is_dir():
                    raise ValueError(f"Expected a directory. {source_path} is not a directory")
        else:
            raise AssertionError(f"Expected FlyteDirectory or os.PathLike object, received {type(python_val)}")

        # If we're uploading something, that means that the uri should always point to the upload destination.
        if should_upload:
            if remote_directory is None:
                remote_directory = ctx.file_access.get_random_remote_directory()
            if not pathlib.Path(source_path).is_dir():
                raise FlyteAssertion("Expected a directory. {} is not a directory".format(source_path))
            # remote_directory will convert the path from `flyte://` to `s3://` or `gs://`
            remote_directory = await ctx.file_access.async_put_data(
                source_path, remote_directory, is_multipart=True, batch_size=batch_size
            )
            return Literal(scalar=Scalar(blob=Blob(metadata=meta, uri=remote_directory)))

        # If not uploading, then we can only take the original source path as the uri.
        else:
            return Literal(scalar=Scalar(blob=Blob(metadata=meta, uri=source_path)))

    def dict_to_flyte_directory(
        self, dict_obj: typing.Dict[str, str], expected_python_type: typing.Type[FlyteDirectory]
    ) -> FlyteDirectory:
        path = dict_obj.get("path", None)

        if path is None:
            raise ValueError("FlyteDirectory's path should not be None")

        return self.to_python_value(
            FlyteContextManager.current_context(),
            Literal(
                scalar=Scalar(
                    blob=Blob(
                        metadata=BlobMetadata(
                            type=_core_types.BlobType(
                                format="", dimensionality=_core_types.BlobType.BlobDimensionality.MULTIPART
                            )
                        ),
                        uri=path,
                    )
                )
            ),
            expected_python_type,
        )

    def from_binary_idl(
        self, binary_idl_object: Binary, expected_python_type: typing.Type[FlyteDirectory]
    ) -> FlyteDirectory:
        """
        If the input is from flytekit, the Life Cycle will be as follows:

        Life Cycle:
        binary IDL                 -> resolved binary         -> bytes                   -> expected Python object
        (flytekit customized          (propeller processing)     (flytekit binary IDL)      (flytekit customized
        serialization)                                                                       deserialization)

        Example Code:
        @dataclass
        class DC:
            fd: FlyteDirectory

        @workflow
        def wf(dc: DC):
            t_fd(dc.fd)

        Note:
        - The deserialization is the same as put a flyte directory in a dataclass, which will deserialize by the mashumaro's API.

        Related PR:
        - Title: Override Dataclass Serialization/Deserialization Behavior for FlyteTypes via Mashumaro
        - Link: https://github.com/flyteorg/flytekit/pull/2554
        """
        if binary_idl_object.tag == MESSAGEPACK:
            python_val = msgpack.loads(binary_idl_object.value)
            return self.dict_to_flyte_directory(python_val, expected_python_type)
        else:
            raise TypeTransformerFailedError(f"Unsupported binary format: `{binary_idl_object.tag}`")

    def from_generic_idl(self, generic: Struct, expected_python_type: typing.Type[FlyteDirectory]) -> FlyteDirectory:
        """
        If the input is from Flyte Console, the Life Cycle will be as follows:

        Life Cycle:
        json str            -> protobuf struct         -> resolved protobuf struct   -> expected Python object
        (console user input)   (console output)           (propeller)                   (flytekit customized deserialization)

        Example Code:
        @dataclass
        class DC:
            fd: FlyteDirectory

        @workflow
        def wf(dc: DC):
            t_fd(dc.fd)

        Note:
        - The deserialization is the same as put a flyte directory in a dataclass, which will deserialize by the mashumaro's API.

        Related PR:
        - Title: Override Dataclass Serialization/Deserialization Behavior for FlyteTypes via Mashumaro
        - Link: https://github.com/flyteorg/flytekit/pull/2554
        """
        json_str = _json_format.MessageToJson(generic)
        python_val = json.loads(json_str)
        return self.dict_to_flyte_directory(python_val, expected_python_type)

    async def async_to_python_value(
        self, ctx: FlyteContext, lv: Literal, expected_python_type: typing.Type[FlyteDirectory]
    ) -> FlyteDirectory:
        # Handle dataclass attribute access
        if lv.scalar:
            if lv.scalar.binary:
                return self.from_binary_idl(lv.scalar.binary, expected_python_type)
            if lv.scalar.generic:
                return self.from_generic_idl(lv.scalar.generic, expected_python_type)

        try:
            uri = lv.scalar.blob.uri
        except AttributeError:
            raise TypeTransformerFailedError(f"Cannot convert from {lv} to {expected_python_type}")

        if lv.scalar.blob.metadata.type.dimensionality != BlobType.BlobDimensionality.MULTIPART:
            raise TypeTransformerFailedError(f"{lv.scalar.blob.uri} is not a directory.")

        if not ctx.file_access.is_remote(uri) and not os.path.isdir(uri):
            raise FlyteAssertion(f"Expected a directory, but the given uri '{uri}' is not a directory.")

        # This is a local file path, like /usr/local/my_dir, don't mess with it. Certainly, downloading it doesn't
        # make any sense.
        if not ctx.file_access.is_remote(uri):
            return expected_python_type(uri, remote_directory=False)

        # For the remote case, return a FlyteDirectory object that can download
        local_folder = ctx.file_access.get_random_local_directory()

        batch_size = get_batch_size(expected_python_type)

        _downloader = partial(ctx.file_access.get_data, uri, local_folder, is_multipart=True, batch_size=batch_size)

        expected_format = self.get_format(expected_python_type)

        fd = FlyteDirectory.__class_getitem__(expected_format)(local_folder, _downloader)
        fd._remote_source = uri
        return fd

    def guess_python_type(self, literal_type: LiteralType) -> typing.Type[FlyteDirectory[typing.Any]]:
        if (
            literal_type.blob is not None
            and literal_type.blob.dimensionality == _core_types.BlobType.BlobDimensionality.MULTIPART
        ):
            return FlyteDirectory.__class_getitem__(literal_type.blob.format)
        raise ValueError(f"Transformer {self} cannot reverse {literal_type}")


TypeEngine.register(FlyteDirToMultipartBlobTransformer())
