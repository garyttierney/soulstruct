import abc
import io
import json
import logging
import zlib
import typing as tp
from pathlib import Path

from soulstruct.containers.bnd.magic import *
from soulstruct.containers.dcx import DCX
from soulstruct.base.game_file import GameFile, InvalidGameFileTypeError
from soulstruct.utilities.core import read_chars_from_buffer
from soulstruct.utilities.binary_struct import BinaryStruct

__all__ = ["BND", "BND3", "BND4", "BaseBND", "BNDEntry"]
_LOGGER = logging.getLogger(__name__)


class BNDError(Exception):
    pass


class BNDEntry:

    # Header struct is computed in BND, as it is constant across entries in the same BND.

    def __init__(self, data: bytes, entry_id: int = None, path: str = None, magic=0x40):
        self.data = data  # Packed binary data, identical to what the unpacked file would look like.
        self.id = entry_id  # Index used by the game engine to access the packed data (in most cases).
        self.path = path  # Full internal 'path' (in most cases). Encoded in shift-jis with escaped backslashes.
        self.magic = magic  # Defaults to 0x40, which seems to be used in DS1/DS3 at least.

    @classmethod
    def unpack(cls, bnd_buffer, entry_header_struct, path_encoding, count):

        entry_headers = []
        entry_header_dicts = entry_header_struct.unpack_count(bnd_buffer, count=count)

        for d in entry_header_dicts:
            bnd_buffer.seek(d.data_offset)
            path = (
                read_chars_from_buffer(bnd_buffer, offset=d.path_offset, encoding=path_encoding)
                if "path_offset" in d
                else None
            )
            data = bnd_buffer.read(d.compressed_data_size)
            if is_entry_compressed(d.entry_magic):
                data = zlib.decompressobj().decompress(data)
            entry_headers.append(cls(entry_id=d.get("entry_id", None), path=path, data=data, magic=d.entry_magic))

        return entry_headers

    def get_data_for_pack(self):
        """Compresses data first if appropriate. Returns `(data, is_compressed)`."""
        if is_entry_compressed(self.magic):
            return zlib.compress(self.data, level=7), True
        return self.data, False

    @property
    def data_size(self):
        return len(self.data)

    def get_packed_path(self, encoding):
        """Encodes path in Japanese and null-terminates."""
        return self.path.encode(encoding) + b"\x00"

    @property
    def name(self):
        return Path(self.path).name

    @property
    def stem(self):
        return Path(self.path).stem

    @property
    def path_with_forward_slashes(self):
        return self.path.replace("\\", "/")

    @property
    def directory_with_forward_slashes(self):
        return str(Path(self.path).parent).replace("\\", "/")

    def copy(self):
        return BNDEntry(data=self.data, entry_id=self.id, path=self.path, magic=self.magic)

    def __eq__(self, other_bnd_entry):
        return all(getattr(self, field) == getattr(other_bnd_entry, field) for field in ("id", "path", "magic", "data"))


class BaseBND(GameFile, abc.ABC):

    # `EXT` depends on BND type.
    MANIFEST_FIELDS: tuple[str] = ()
    VERSION: str = None

    BNDEntry = BNDEntry  # for convenience

    signature: str
    magic: int
    big_endian: bool

    def __init__(self, bnd_source=None, dcx_magic=()):
        """Load a BND binder file.

        Source can be a .*bnd[.dcx] file, an unpacked BND directory (or the 'bnd_manifest.json' file inside it), raw
        `bytes`, or an open data stream (or None to create an empty BND).
        """
        self.signature = ""
        self.magic = -1  # Can't guess this; you'll need to specify it based on your BND type.
        self.big_endian = False
        self._most_recent_hash_table = b""
        self._most_recent_entry_count = 0
        self._most_recent_paths = []
        self.header_struct = None  # type: tp.Optional[BinaryStruct]

        self._entries = []  # type: list[BNDEntry]

        super().__init__(bnd_source, dcx_magic=dcx_magic)

    def _handle_other_source_types(self, file_source, **kwargs) -> tp.Optional[io.BufferedIOBase]:
        """A BND can also be loaded from a `bnd_manifest.json` file or a directory containing such a file."""

        if isinstance(file_source, (str, Path)):
            file_source = Path(file_source)
            if file_source.is_dir() and (file_source / "bnd_manifest.json").exists():
                file_source = file_source / "bnd_manifest.json"  # go to below
            if file_source.is_file() and file_source.name == "bnd_manifest.json":
                directory = file_source.parent
                if directory.suffix == ".unpacked":  # only this suffix is automatically removed
                    self.path = directory.with_name(directory.name[:-9])
                else:
                    self.path = directory  # writing this path will conflict with this unpacked folder source
                self.load_unpacked_dir(directory)
                return

        raise InvalidGameFileTypeError(f"`bnd_source` is not a `bnd_manifest.json` file or directory containing one.")

    def load_unpacked_dir(self, directory):
        """Load BND from a Soulstruct-unpacked BND directory with a `bnd_manifest.json` file."""
        directory = Path(directory)
        if not directory.is_dir():
            raise ValueError(f"Could not find unpacked BND directory {repr(directory)}.")
        with (directory / "bnd_manifest.json").open("r", encoding="shift-jis") as f:
            manifest = json.load(f)
        self.load_manifest_header(manifest)  # abstract
        self.add_entries_from_manifest(manifest["entries"], directory, manifest["use_id_prefix"])
        self.create_header_structs()  # abstract

    def load_manifest_header(self, manifest: dict):
        if "version" not in manifest:
            raise BNDError("JSON manifest file does not contain 'version' key.")
        self._check_version(manifest["version"])
        for field in self.MANIFEST_FIELDS:
            setattr(self, field, manifest[field])
        self.dcx_magic = tuple(manifest["dcx_magic"])

    def add_entries_from_manifest(self, entries: dict, directory, use_id_prefix: bool):
        directory = Path(directory)
        unsorted_entries = {}  # maps ID to (path, data, magic) tuple
        for root, entry_dicts in entries.items():
            for entry in entry_dicts:
                find_entry_basename = f"__{entry['id']}__{entry['name']}" if use_id_prefix else entry['name']
                with (directory / find_entry_basename).open("rb") as entry_file:
                    entry_data = entry_file.read()
                unsorted_entries[entry['id']] = (f"{root}\\{entry['name']}", entry_data, entry['magic'])
        for entry_id, (path, data, magic) in sorted(unsorted_entries.items()):
            self.add_entry(BNDEntry(entry_id=entry_id, path=path, data=data, magic=magic))

    @abc.abstractmethod
    def create_header_structs(self):
        """Called by `load_unpacked_dir` to create instance structs from manifest fields."""

    def write_unpacked_dir(self, directory=None):
        if not has_path(self.magic):
            raise TypeError("Writing unpacked BND directories is only supported for BND versions with path strings.")
        if directory is None:
            if self.path:
                directory = self.path.with_suffix(self.path.suffix + ".unpacked")
            else:
                raise ValueError("Cannot set automatic unpacked BND directory name.")
        else:
            directory = Path(directory, self.path.name + ".unpacked")
        directory.mkdir(parents=True, exist_ok=True)

        entry_tree_dict = {}
        use_index_prefix = self.has_repeated_entry_names

        for i, entry in enumerate(self._entries):
            entry_directory = str(Path(entry.path).parent)  # no trailing backslash
            entry_dict = {"magic": entry.magic, "id": entry.id if has_id(self.magic) else i, "name": entry.name}
            entry_tree_dict.setdefault(entry_directory, []).append(entry_dict)
            entry_file_name = f"__{entry.id}__{entry.name}" if use_index_prefix else entry.name
            with (directory / entry_file_name).open("wb") as f:
                packed_entry, _ = entry.get_data_for_pack()
                f.write(packed_entry)

        json_dict = self.get_json_header()
        json_dict["entries"] = entry_tree_dict

        # NOTE: BND manifest is always encoded in shift-JIS.
        with (directory / "bnd_manifest.json").open("w", encoding="shift-jis") as f:
            json.dump(json_dict, f, indent=4)

    def add_entry(self, entry: BNDEntry):
        if entry in self._entries:
            raise BNDError(f"Given `BNDEntry` instance with ID {entry.id} is already in this BND.")
        if entry.id in self.entries_by_id:
            _LOGGER.warning(f"Entry ID {entry.id} appears more than once in this BND. Fix this as soon as you can.")
        self._entries.append(entry)

    def remove_entry(self, id_or_path_or_basename):
        if isinstance(id_or_path_or_basename, int):
            entry = self.entries_by_id[id_or_path_or_basename]
        elif isinstance(id_or_path_or_basename, str):
            try:
                entry = self.entries_by_path[id_or_path_or_basename]
            except KeyError:
                entry = self.entries_by_basename[id_or_path_or_basename]
        else:
            raise TypeError("Entry to be removed should be a BND ID (int) or path/basename (str).")
        self._entries.remove(entry)

    def clear_entries(self):
        """Remove all entries from the BND."""
        self._entries.clear()

    def _check_version(self, version: str):
        if version != self.VERSION:
            raise BNDError(f"Version of file ({version}) does not match `BND` class version ({self.VERSION}).")

    def get_json_header(self) -> dict:
        raise NotImplementedError

    @property
    def entries(self) -> list[BNDEntry]:
        """Returns an ordered list of BND entries, unpacked with the `entry_class` given to the constructor."""
        return self._entries

    @property
    def entries_by_id(self) -> dict[int, BNDEntry]:
        """Dictionary mapping entry IDs to entries.

        If there are multiple entries with the same ID in the BND, this will raise a `ValueError`. This should never
        happen; if it does, fix it by accessing the culprit entries with `.entries` and changing one or more IDs.
        """
        entries = {}
        for entry in self._entries:
            if entry.id in entries:
                raise BNDError(f"There are multiple entries with ID {entry.id}.")
            entries[entry.id] = entry
        return entries

    @property
    def entries_by_path(self) -> dict[str, BNDEntry]:
        """Dictionary mapping entry paths to (classed) entries.

        The same path and/or basename may appear in multiple paths in a BND (e.g. vanilla 'item.msgbnd' in Dark Souls
        Remastered). If it does, this property will raise an exception.
        """
        entries = {}
        for entry in self._entries:
            if entry.path in entries:
                raise ValueError(f"Path '{entry.path}' appears in multiple `BNDEntry` paths.")
            entries[entry.path] = entry
        return entries

    @property
    def entries_by_basename(self) -> dict[str, BNDEntry]:
        """Dictionary mapping entry basenames to (classed) entries.

        The same path and/or basename may appear in multiple paths in a BND (e.g. vanilla 'item.msgbnd' in Dark Souls
        Remastered). If it does, this property will raise an exception.
        """
        entries = {}
        for entry in self._entries:
            if entry.name in entries:
                raise ValueError(f"Basename '{entry.name}' appears in multiple BND entry paths.")
            entries[entry.name] = entry
        return entries

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    @property
    def has_repeated_entry_names(self):
        entry_names = [e.name for e in self.entries]
        return len(set(entry_names)) < len(entry_names)

    def __getitem__(self, id_or_path_or_basename) -> BNDEntry:
        """Shortcut for access by ID (int) or path (str) or basename (str).

        If the path of one entry is the basename of another entry, the former will be given precedence here, but this
        should never happen.
        """
        if isinstance(id_or_path_or_basename, int):
            return self.entries_by_id[id_or_path_or_basename]
        elif isinstance(id_or_path_or_basename, str):
            try:
                return self.entries_by_path[id_or_path_or_basename]
            except KeyError:
                return self.entries_by_basename[id_or_path_or_basename]
        raise TypeError("`BND` key should be an entry ID (int) or path/basename (str).")

    def __iter__(self) -> tp.Iterator[BNDEntry]:
        return iter(self._entries)

    def __len__(self):
        return len(self._entries)

    @classmethod
    def detect(cls, bnd_source):
        """Returns True if `bnd_source` appears to be this subclass of `BaseBND`. Does not support DCX sources."""
        if isinstance(bnd_source, (str, Path)):
            bnd_path = Path(bnd_source)
            if bnd_path.is_file() and bnd_path.name == "bnd_manifest.json":
                bnd_path = bnd_path.parent
            if bnd_path.is_dir():
                try:
                    with (bnd_path / "bnd_manifest.json").open("rb") as f:
                        return json.load(f)["version"] == cls.__name__  # "BND3" or "BND4"
                except FileNotFoundError:
                    return False
            elif bnd_path.is_file():
                with bnd_path.open("rb") as buffer:
                    try:
                        version = read_chars_from_buffer(buffer, length=4, encoding="utf-8")
                    except ValueError:
                        return False
                    return version == cls.__name__
            return False
        elif isinstance(bnd_source, bytes):
            bnd_source = io.BytesIO(bnd_source)
        if isinstance(bnd_source, io.BufferedIOBase):
            old_offset = bnd_source.tell()
            bnd_source.seek(0)
            try:
                version = read_chars_from_buffer(bnd_source, length=4, encoding="utf-8")
            except ValueError:
                bnd_source.seek(old_offset)
                return False
            bnd_source.seek(old_offset)
            return version == cls.__name__


class BND3(BaseBND):
    """BND used before Dark Souls 2 (2014)."""

    HEADER_STRUCT_START = (
        ("version", "4s", b"BND3"),
        ("signature", "8s"),  # Real signature may be shorter, but packing will pad it out.
        ("magic", "b"),
        ("big_endian", "?"),
    )
    HEADER_STRUCT_ENDIAN = (
        ("unknown", "?"),  # usually False
        ("zero", "B", 0),
        ("entry_count", "i"),
        ("file_size", "i"),
        "8x",
    )

    BND_ENTRY_HEADER = (
        ("entry_magic", "B"),
        "3x",
        ("compressed_data_size", "i"),
        ("data_offset", "i"),
    )
    ENTRY_ID = ("entry_id", "i")
    NAME_OFFSET = ("path_offset", "i")
    UNCOMPRESSED_DATA_SIZE = ("uncompressed_data_size", "i")

    VERSION = "BND3"
    MANIFEST_FIELDS = ("signature", "magic", "big_endian", "unknown")

    unknown: bool

    def __init__(self, bnd_source=None, dcx_magic=()):
        self.unknown = False
        super().__init__(bnd_source=bnd_source, dcx_magic=dcx_magic)

    def unpack(self, buffer, **kwargs):
        self.header_struct = BinaryStruct(*self.HEADER_STRUCT_START, byte_order="<")
        header = self.header_struct.unpack(buffer)
        self._check_version(header["version"].decode())
        self.signature = header["signature"].rstrip(b"\0").decode()
        self.magic = header["magic"]
        self.big_endian = header["big_endian"] or is_big_endian(self.magic)
        byte_order = ">" if self.big_endian else "<"
        header.update(self.header_struct.unpack(buffer, *self.HEADER_STRUCT_ENDIAN, byte_order=byte_order))
        self.unknown = header["unknown"]

        self.entry_header_struct = BinaryStruct(*self.BND_ENTRY_HEADER, byte_order=byte_order)
        if has_id(self.magic):
            self.entry_header_struct.add_fields(self.ENTRY_ID, byte_order=byte_order)
        if has_path(self.magic):
            self.entry_header_struct.add_fields(self.NAME_OFFSET, byte_order=byte_order)
        if has_uncompressed_size(self.magic):
            self.entry_header_struct.add_fields(self.UNCOMPRESSED_DATA_SIZE, byte_order=byte_order)

        # NOTE: BND paths are *not* encoded in `shift_jis_2004`, unlike most other strings! They are `shift-jis`.
        #  The main annoyance here is that escaped backslashes are encoded as the yen symbol in `shift_jis_2004`.
        for entry in BNDEntry.unpack(
            buffer, self.entry_header_struct, path_encoding="shift-jis", count=header["entry_count"]
        ):
            self.add_entry(entry)

    def create_header_structs(self):
        self.header_struct = BinaryStruct(*self.HEADER_STRUCT_START, byte_order="<")
        byte_order = ">" if self.big_endian else "<"
        self.header_struct.add_fields(*self.HEADER_STRUCT_ENDIAN, byte_order=byte_order)
        self.entry_header_struct = BinaryStruct(*self.BND_ENTRY_HEADER, byte_order=byte_order)
        if has_id(self.magic):
            self.entry_header_struct.add_fields(self.ENTRY_ID, byte_order=byte_order)
        if has_path(self.magic):
            self.entry_header_struct.add_fields(self.NAME_OFFSET, byte_order=byte_order)
        if has_uncompressed_size(self.magic):
            self.entry_header_struct.add_fields(self.UNCOMPRESSED_DATA_SIZE, byte_order=byte_order)

    def pack(self):
        entry_header_dicts = []
        packed_entry_headers = b""
        packed_entry_paths = b""
        relative_entry_path_offsets = []
        packed_entry_data = b""
        relative_entry_data_offsets = []

        for entry in sorted(self._entries, key=lambda e: e.id):
            entry_header_dict = {
                "entry_magic": entry.magic,
                "compressed_data_size": entry.data_size,
                "data_offset": len(packed_entry_data),
            }
            if has_id(self.magic):
                entry_header_dict["entry_id"] = entry.id
            if has_path(self.magic):
                entry_header_dict["path_offset"] = len(packed_entry_paths)
                relative_entry_path_offsets.append(len(packed_entry_paths))  # Relative to start of packed entry paths.
                packed_entry_paths += entry.get_packed_path("shift-jis")
            if has_uncompressed_size(self.magic):
                entry_header_dict["uncompressed_data_size"] = entry.data_size

            relative_entry_data_offsets.append(len(packed_entry_data))
            entry_data, is_compressed = entry.get_data_for_pack()
            if is_compressed:
                entry_header_dict["compressed_data_size"] = len(entry_data)
            packed_entry_data += entry_data
            entry_header_dicts.append(entry_header_dict)

        # Compute table offsets.
        entry_header_table_offset = self.header_struct.size
        entry_path_table_offset = entry_header_table_offset + self.entry_header_struct.size * len(self._entries)
        entry_packed_data_offset = entry_path_table_offset + len(packed_entry_paths)
        bnd_file_size = entry_packed_data_offset + len(packed_entry_data)

        # Pack BND header.
        packed_header = self.header_struct.pack(
            signature=self.signature,
            magic=self.magic,
            big_endian=self.big_endian,
            unknown=self.unknown,
            entry_count=len(self._entries),
            file_size=bnd_file_size,
        )

        # Convert relative offsets to absolute and pack entry headers.
        for entry_header_dict in entry_header_dicts:
            entry_header_dict["data_offset"] += entry_packed_data_offset
            if has_path(self.magic):
                entry_header_dict["path_offset"] += entry_path_table_offset
            packed_entry_headers += self.entry_header_struct.pack(entry_header_dict)

        return packed_header + packed_entry_headers + packed_entry_paths + packed_entry_data

    def get_json_header(self):
        return {
            "version": "BND3",
            "signature": self.signature,
            "magic": self.magic,
            "big_endian": self.big_endian,
            "unknown": self.unknown,
            "use_id_prefix": self.has_repeated_entry_names,
            "dcx_magic": self.dcx_magic,
        }


class BND4(BaseBND):
    """BND used since Dark Souls 2 (2014)."""

    HEADER_STRUCT_START = (
        ("version", "4s", b"BND4"),
        ("flag1", "?"),
        ("flag2", "?"),
        "2x",
        ("big_endian", "i"),  # 0x00010000 (False) or 0x00000100 (True)
    )
    HEADER_STRUCT_ENDIAN = (
        ("entry_count", "i"),
        ("header_size", "q", 64),
        ("signature", "8s"),  # Real signature may be shorter, but packing will pad it out.
        ("entry_header_size", "q"),
        ("data_offset", "q"),
        ("utf16_paths", "?"),
        ("magic", "b"),
        ("hash_table_type", "B"),  # 0, 1, 4, or 128
        "5x",
        ("hash_table_offset", "q"),  # only non-zero if hash_table_type == 4
    )

    BND_ENTRY_HEADER = (("entry_magic", "B"), "3x", ("minus_one", "i", -1), ("compressed_data_size", "q"))
    UNCOMPRESSED_DATA_SIZE = ("uncompressed_data_size", "q")
    DATA_OFFSET = ("data_offset", "I")
    ENTRY_ID = ("entry_id", "i")
    NAME_OFFSET = ("path_offset", "i")

    HASH_TABLE_HEADER = BinaryStruct(
        "8x", ("path_hashes_offset", "q"), ("hash_group_count", "I"), ("unknown", "i", 0x00080810)
    )
    PATH_HASH_STRUCT = BinaryStruct(("hashed_value", "I"), ("entry_index", "i"),)
    HASH_GROUP_STRUCT = BinaryStruct(("length", "i"), ("index", "i"),)

    VERSION = "BND4"
    MANIFEST_FIELDS = ("signature", "magic", "big_endian", "utf16_paths", "hash_table_type", "flag1", "flag2")

    flag1: bool
    flag2: bool
    utf16_paths: bool
    hash_table_type: int
    hash_table_offset: int

    def __init__(self, bnd_source=None, dcx_magic=()):
        self.flag1 = False  # unknown
        self.flag2 = False  # unknown
        self.utf16_paths = False  # If False, paths are written in Shift-JIS.
        self.hash_table_type = 0
        self.hash_table_offset = 0
        super().__init__(bnd_source=bnd_source, dcx_magic=dcx_magic)

    def unpack(self, bnd_buffer, **kwargs):
        self.header_struct = BinaryStruct(*self.HEADER_STRUCT_START, byte_order="<")
        header = self.header_struct.unpack(bnd_buffer)
        self._check_version(header["version"].decode())
        self.flag1 = header["flag1"]
        self.flag2 = header["flag2"]
        self.big_endian = header["big_endian"] == 0x00000100  # Magic not used to infer endianness here.
        byte_order = ">" if self.big_endian else "<"
        header.update(self.header_struct.unpack(bnd_buffer, *self.HEADER_STRUCT_ENDIAN, byte_order=byte_order))
        self.signature = header["signature"].rstrip(b"\0").decode()
        self.magic = header["magic"]
        self.utf16_paths = header["utf16_paths"]
        self.hash_table_type = header["hash_table_type"]
        self.hash_table_offset = header["hash_table_offset"]
        path_encoding = ("utf-16be" if self.big_endian else "utf-16le") if self.utf16_paths else "shift-jis"

        if header["entry_header_size"] != header_size(self.magic):
            raise ValueError(
                f"Expected BND entry header size {header_size(self.magic)} based on magic\n"
                f"{hex(self.magic)}, but BND header says {header['entry_header_size']}."
            )
        if self.hash_table_type != 4 and self.hash_table_offset != 0:
            _LOGGER.warning(
                f"Found non-zero hash table offset {self.hash_table_offset}, but header says this BND has no hash "
                f"table."
            )
        self.entry_header_struct = BinaryStruct(*self.BND_ENTRY_HEADER, byte_order=byte_order)
        if has_uncompressed_size(self.magic):
            self.entry_header_struct.add_fields(self.UNCOMPRESSED_DATA_SIZE, byte_order=byte_order)
        self.entry_header_struct.add_fields(self.DATA_OFFSET, byte_order=byte_order)
        if has_id(self.magic):
            self.entry_header_struct.add_fields(self.ENTRY_ID, byte_order=byte_order)
        if has_path(self.magic):
            self.entry_header_struct.add_fields(self.NAME_OFFSET, byte_order=byte_order)
        if self.magic == 0x20:
            # Extra pad.
            self.entry_header_struct.add_fields("8x")
        if header["entry_header_size"] != self.entry_header_struct.size:
            _LOGGER.warning(
                f"Entry header size given in BND header ({header['entry_header_size']}) does not match actual entry "
                f"header size ({self.entry_header_struct.size})."
            )
        for entry in BNDEntry.unpack(
            bnd_buffer, self.entry_header_struct, path_encoding=path_encoding, count=header["entry_count"]
        ):
            self.add_entry(entry)

        # Read hash table.
        if self.hash_table_type == 4:
            bnd_buffer.seek(self.hash_table_offset)
            self._most_recent_hash_table = bnd_buffer.read(header["data_offset"] - self.hash_table_offset)
        self._most_recent_entry_count = len(self._entries)
        self._most_recent_paths = [entry.path for entry in self._entries]

    def create_header_structs(self):
        self._most_recent_hash_table = b""  # Hash table will need to be built on first pack.
        self._most_recent_entry_count = len(self._entries)
        self._most_recent_paths = [entry.path for entry in self._entries]

        self.header_struct = BinaryStruct(*self.HEADER_STRUCT_START, byte_order="<")
        byte_order = ">" if self.big_endian else "<"
        self.header_struct.add_fields(*self.HEADER_STRUCT_ENDIAN, byte_order=byte_order)
        self.entry_header_struct = BinaryStruct(*self.BND_ENTRY_HEADER, byte_order=byte_order)
        if has_uncompressed_size(self.magic):
            self.entry_header_struct.add_fields(self.UNCOMPRESSED_DATA_SIZE, byte_order=byte_order)
        self.entry_header_struct.add_fields(self.DATA_OFFSET, byte_order=byte_order)
        if has_id(self.magic):
            self.entry_header_struct.add_fields(self.ENTRY_ID, byte_order=byte_order)
        if has_path(self.magic):
            self.entry_header_struct.add_fields(self.NAME_OFFSET, byte_order=byte_order)
        if self.magic == 0x20:
            # Extra pad.
            self.entry_header_struct.add_fields("8x")

    def pack(self):
        entry_header_dicts = []
        packed_entry_headers = b""
        packed_entry_paths = b""
        relative_entry_path_offsets = []
        packed_entry_data = b""
        relative_entry_data_offsets = []
        rebuild_hash_table = not self._most_recent_hash_table
        path_encoding = ("utf-16be" if self.big_endian else "utf-16le") if self.utf16_paths else "shift-jis"

        if len(self._entries) != self._most_recent_entry_count:
            rebuild_hash_table = True
        for i, entry in enumerate(self._entries):
            if not rebuild_hash_table and entry.path != self._most_recent_paths[i]:
                rebuild_hash_table = True

        self._most_recent_entry_count = len(self._entries)
        self._most_recent_paths = [entry.path for entry in self._entries]

        for entry in self._entries:

            packed_entry_data += b"\0" * 10  # Each entry is separated by ten pad bytes. (Probably not necessary.)

            entry_header_dict = {
                "entry_magic": entry.magic,
                "compressed_data_size": entry.data_size,
                "data_offset": len(packed_entry_data),
            }
            if has_id(self.magic):
                entry_header_dict["entry_id"] = entry.id
            if has_path(self.magic):
                entry_header_dict["path_offset"] = len(packed_entry_paths)
                relative_entry_path_offsets.append(len(packed_entry_paths))  # Relative to start of packed entry paths.
                packed_entry_paths += entry.get_packed_path(path_encoding)
            if has_uncompressed_size(self.magic):
                entry_header_dict["uncompressed_data_size"] = entry.data_size

            relative_entry_data_offsets.append(len(packed_entry_data))
            entry_data, is_compressed = entry.get_data_for_pack()
            if is_compressed:
                entry_header_dict["compressed_data_size"] = len(entry_data)
            packed_entry_data += entry_data
            entry_header_dicts.append(entry_header_dict)

        entry_header_table_offset = self.header_struct.size
        entry_path_table_offset = entry_header_table_offset + self.entry_header_struct.size * len(self._entries)
        if self.hash_table_type == 4:
            hash_table_offset = entry_path_table_offset + len(packed_entry_paths)
            if rebuild_hash_table:
                packed_hash_table = self.build_hash_table()
            else:
                packed_hash_table = self._most_recent_hash_table
            entry_packed_data_offset = hash_table_offset + len(packed_hash_table)
        else:
            hash_table_offset = 0
            packed_hash_table = b""
            entry_packed_data_offset = entry_path_table_offset + len(packed_entry_paths)
        # BND file size not needed.

        packed_header = self.header_struct.pack(
            flag1=self.flag1,
            flag2=self.flag2,
            big_endian=self.big_endian,
            entry_count=len(self._entries),
            signature=self.signature,
            entry_header_size=self.entry_header_struct.size,
            data_offset=entry_packed_data_offset,
            utf16_paths=self.utf16_paths,
            magic=self.magic,
            hash_table_type=self.hash_table_type,
            hash_table_offset=hash_table_offset,
        )

        # Convert relative offsets to absolute and pack entry headers.
        for entry_header_dict in entry_header_dicts:
            entry_header_dict["data_offset"] += entry_packed_data_offset
            if has_path(self.magic):
                entry_header_dict["path_offset"] += entry_path_table_offset
            packed_entry_headers += self.entry_header_struct.pack(entry_header_dict)

        return packed_header + packed_entry_headers + packed_entry_paths + packed_hash_table + packed_entry_data

    def get_json_header(self):
        return {
            "version": "BND4",
            "signature": self.signature,
            "magic": self.magic,
            "big_endian": self.big_endian,
            "utf16_paths": self.utf16_paths,
            "hash_table_type": self.hash_table_type,
            "flag1": self.flag1,
            "flag2": self.flag2,
            "use_id_prefix": self.has_repeated_entry_names,
            "dcx_magic": self.dcx_magic,
        }

    @staticmethod
    def is_prime(p):
        if p < 2:
            return False
        if p == 2:
            return True
        if (p % 2) == 0:
            return False
        for i in range(3, p // 2, 2):
            if (p % i) == 0:
                return False
            if i ** 2 > p:
                return True
        return True

    def build_hash_table(self):
        """ Some BND4 resources include tables of hashed entry paths, which aren't needed to read file contents, but
        need to be re-hashed to properly pack the file in case any paths have changed (or the number of entries). """

        # Group count set to first prime number greater than or equal to the number of entries divided by 7.
        for p in range(len(self._entries) // 7, 100000):
            if self.is_prime(p):
                group_count = p
                break
        else:
            raise ValueError("Hash group count could not be determined.")

        hashes = []
        hash_lists = [[] for _ in range(group_count)]  # type: list[list[tuple[int, int]], ...]

        for entry_index, entry in enumerate(self._entries):
            hashes.append(self.path_hash(entry.path))
            list_index = hashes[-1] % group_count
            hash_lists[list_index].append((hashes[-1], entry_index))

        for hash_list in hash_lists:
            hash_list.sort()  # Sort by hash value.

        hash_groups = []
        path_hashes = []

        total_hash_count = 0
        for hash_list in hash_lists:
            first_hash_index = total_hash_count
            for path_hash in hash_list:
                path_hashes.append({"hashed_value": path_hash[0], "entry_index": path_hash[1]})
                total_hash_count += 1
            hash_groups.append({"index": first_hash_index, "length": total_hash_count - first_hash_index})

        packed_hash_groups = self.HASH_GROUP_STRUCT.pack_multiple(hash_groups)
        packed_hash_table_header = self.HASH_TABLE_HEADER.pack(
            path_hashes_offset=self.HASH_TABLE_HEADER.size + len(packed_hash_groups), hash_group_count=group_count,
        )
        packed_path_hashes = self.PATH_HASH_STRUCT.pack_multiple(path_hashes)

        return packed_hash_table_header + packed_hash_groups + packed_path_hashes

    @staticmethod
    def path_hash(path_string):
        """ Simple string-hashing algorithm used by FROM. Strings use forward-slash path separators and always start
        with a forward slash. """
        hashable = path_string.replace("\\", "/")
        if not hashable.startswith("/"):
            hashable = "/" + hashable
        h = 0
        for i, s in enumerate(hashable):
            h += i * 37 + ord(s)
        return h


def BND(bnd_source=None, dcx_magic=()) -> BaseBND:
    """Auto-detects BND version (`BND3` or `BND4`) to use when opening the source, if appropriate.

    Args:
        bnd_source: path to BND file or BND file content. The BND version will be automatically detected from the data.
            If None, this function will return an empty `BND4` container. (Default: None)
        dcx_magic: optional DCX magic (pair of integers).
    """
    detect_source = bnd_source
    if isinstance(bnd_source, (str, Path)):
        if not Path(bnd_source).is_dir():
            bnd_path = Path(bnd_source).absolute()
            if not bnd_path.is_file():
                raise FileNotFoundError(f"Could not find BND file: {bnd_path}")
            if bnd_path.suffix == ".dcx":
                # Must unpack DCX archive before detecting BND type.
                if dcx_magic:
                    raise ValueError("Cannot specify `dcx_magic` when using a DCX source.")
                detect_source = DCX(bnd_path).data
            else:
                detect_source = bnd_path
    if BND3.detect(detect_source):
        return BND3(bnd_source, dcx_magic=dcx_magic)
    elif BND4.detect(detect_source):
        return BND4(bnd_source, dcx_magic=dcx_magic)
    raise TypeError("Data bytes could not be interpreted as `BND3` or `BND4` instance.")
