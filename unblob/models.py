import abc
import io
from pathlib import Path
from typing import List, Optional, Tuple, Type

import attr
import yara
from structlog import get_logger

from .file_utils import Endian, InvalidInputFormat, StructParser
from .report import Report, Reports

logger = get_logger()

# The state transitions are:
#
# file ──► YaraMatchResult ──► ValidChunk
#


@attr.define
class Task:
    root: Path
    path: Path
    depth: int


@attr.define
class YaraMatchResult:
    """Results of a YARA match grouped by file types (handlers).

    When running a YARA search for specific bytes, we get a list of Blobs
    and the Handler to the corresponding YARA rule.
    """

    handler: "Handler"
    match: yara.Match


@attr.define
class Chunk:
    """
    Chunk of a Blob, have start and end offset, but still can be invalid.

    For an array ``b``, a chunk ``c`` represents the slice:
    ::

        b[c.start_offset:c.end_offset]
    """

    start_offset: int
    """The index of the first byte of the chunk"""

    end_offset: int
    """The index of the first byte after the end of the chunk"""

    def __attrs_post_init__(self):
        if self.start_offset < 0 or self.end_offset < 0:
            raise InvalidInputFormat(f"Chunk has negative offset: {self}")
        if self.start_offset >= self.end_offset:
            raise InvalidInputFormat(
                f"Chunk has higher start_offset than end_offset: {self}"
            )

    @property
    def size(self) -> int:
        return self.end_offset - self.start_offset

    @property
    def range_hex(self) -> str:
        return f"0x{self.start_offset:x}-0x{self.end_offset:x}"

    def contains(self, other: "Chunk") -> bool:
        return (
            self.start_offset < other.start_offset
            and self.end_offset >= other.end_offset
        )

    def contains_offset(self, offset: int) -> bool:
        return self.start_offset <= offset < self.end_offset

    def __repr__(self) -> str:
        return self.range_hex


@attr.define(repr=False)
class ValidChunk(Chunk):
    """Known to be valid chunk of a Blob, can be extracted with an external program."""

    handler: "Handler" = attr.ib(init=False, eq=False)
    is_encrypted: bool = attr.ib(default=False)

    def extract(self, inpath: Path, outdir: Path):
        if self.is_encrypted:
            logger.warning(
                "Encrypted file is not extracted",
                path=inpath,
                chunk=self,
            )
            raise ExtractError()

        self.handler.extract(inpath, outdir)


@attr.define(repr=False)
class UnknownChunk(Chunk):
    """Gaps between valid chunks or otherwise unknown chunks.

    Important for manual analysis, and analytical certanity: for example
    entropy, other chunks inside it, metadata, etc.

    These are not extracted, just logged for information purposes and further analysis,
    like most common bytest (like \x00 and \xFF), ASCII strings, high entropy, etc.
    """


class TaskResult:
    def __init__(self, task=None):
        self._task = task
        self._reports = Reports()
        self._new_tasks = []

    def add_report(self, report: Report):
        self._reports.append(report)

    def add_new_task(self, task: Task):
        self._new_tasks.append(task)

    @property
    def task(self):
        return self._task

    @property
    def new_tasks(self):
        return self._new_tasks

    @property
    def reports(self) -> Reports:
        return self._reports


class ExtractError(Exception):
    """There was an error during extraction"""

    def __init__(self, *reports: Report):
        super().__init__()
        self.reports: Tuple[Report, ...] = reports


class Extractor(abc.ABC):
    def get_dependencies(self) -> List[str]:
        """Returns the external command dependencies."""
        return []

    @abc.abstractmethod
    def extract(self, inpath: Path, outdir: Path):
        """Extract the carved out chunk.

        Raises ExtractError on failure.
        """


class Handler(abc.ABC):
    """A file type handler is responsible for searching, validating and "unblobbing" files from Blobs."""

    NAME: str
    YARA_RULE: str
    # We need this, because not every match reflects the actual start
    # (e.g. tar magic is in the middle of the header)
    YARA_MATCH_OFFSET: int = 0

    EXTRACTOR: Optional[Extractor]

    @classmethod
    def get_dependencies(cls):
        """Returns external command dependencies needed for this handler to work."""
        if cls.EXTRACTOR:
            return cls.EXTRACTOR.get_dependencies()
        return []

    @abc.abstractmethod
    def calculate_chunk(
        self, file: io.BufferedIOBase, start_offset: int
    ) -> Optional[ValidChunk]:
        """Calculate the Chunk offsets from the Blob and the file type headers."""

    def extract(self, inpath: Path, outdir: Path):
        if self.EXTRACTOR is None:
            logger.debug("Skipping file: no extractor.", path=inpath)
            raise ExtractError()

        # We only extract every blob once, it's a mistake to extract the same blob again
        outdir.mkdir(parents=True, exist_ok=False)

        self.EXTRACTOR.extract(inpath, outdir)


class StructHandler(Handler):
    C_DEFINITIONS: str
    # A struct from the C_DEFINITIONS used to parse the file's header
    HEADER_STRUCT: str

    def __init__(self):
        self._struct_parser = StructParser(self.C_DEFINITIONS)

    @property
    def cparser_le(self):
        return self._struct_parser.cparser_le

    @property
    def cparser_be(self):
        return self._struct_parser.cparser_be

    def parse_header(self, file: io.BufferedIOBase, endian=Endian.LITTLE):
        header = self._struct_parser.parse(self.HEADER_STRUCT, file, endian)
        logger.debug("Header parsed", header=header, _verbosity=3)
        return header


class Handlers:
    def __init__(self, by_priority: List[Tuple[Type[Handler], ...]]):
        self._by_priority = by_priority
        self._flat = [h for handlers in by_priority for h in handlers]

    def with_prepended(self, by_priority):
        if not by_priority:
            # No additions
            return self
        return Handlers([tuple(by_priority)] + self._by_priority)

    @property
    def by_priority(self):
        return self._by_priority

    @property
    def flat(self):
        return self._flat
