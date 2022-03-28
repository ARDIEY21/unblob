import multiprocessing
import stat
import statistics
from operator import attrgetter
from pathlib import Path
from typing import List

import attr
import plotext as plt
from structlog import get_logger

from unblob.handlers import BUILTIN_HANDLERS, Handlers

from .extractor import (
    carve_unknown_chunks,
    carve_valid_chunk,
    fix_extracted_directory,
    get_extract_paths,
    make_extract_dir,
)
from .file_utils import iterate_file, valid_path
from .finder import search_chunks_by_priority
from .iter_utils import pairwise
from .logging import noformat
from .math import shannon_entropy
from .models import ExtractError, Task, TaskResult, UnknownChunk, ValidChunk
from .pool import make_pool
from .profiling import PerfCounter
from .report import Reports, UnknownError
from .signals import terminate_gracefully

logger = get_logger()

DEFAULT_DEPTH = 10
DEFAULT_PROCESS_NUM = multiprocessing.cpu_count()


@attr.define(kw_only=True)
class ExtractionConfig:
    extract_root: Path
    entropy_depth: int
    entropy_plot: bool = False
    max_depth: int = DEFAULT_DEPTH
    process_num: int = DEFAULT_PROCESS_NUM
    keep_extracted_chunks: bool = False
    handlers: Handlers = BUILTIN_HANDLERS


@terminate_gracefully
def process_file(config: ExtractionConfig, path: Path) -> Reports:
    root = path if path.is_dir() else path.parent
    root_task = Task(
        root=root,
        path=path,
        depth=0,
    )

    processor = Processor(config)
    all_reports = Reports()

    def process_result(pool, result):
        for new_task in result.new_tasks:
            pool.submit(new_task)
        all_reports.extend(result.reports)

    pool = make_pool(
        process_num=config.process_num,
        handler=processor.process_task,
        result_callback=process_result,
    )

    with pool:
        pool.submit(root_task)
        pool.process_until_done()
    return all_reports


class Processor:
    def __init__(self, config: ExtractionConfig):
        self._config = config

    def process_task(self, task: Task) -> TaskResult:
        result = TaskResult(task)
        try:
            with PerfCounter(result, "Process Task"):
                self._process_task(result, task)
        except Exception as exc:
            self._process_error(result, exc)
        return result

    def _process_error(self, result: TaskResult, exc: Exception):
        error_report = UnknownError(exception=exc)
        result.add_report(error_report)
        logger.exception("Unknown error happened", exec_info=exc)

    def _process_task(self, result: TaskResult, task: Task):
        log = logger.bind(path=task.path)

        if task.depth >= self._config.max_depth:
            # TODO: Use the reporting feature to warn the user (ONLY ONCE) at the end of execution, that this limit was reached.
            log.debug("Reached maximum depth, stop further processing")
            return

        if not valid_path(task.path):
            log.warn("Path contains invalid characters, it won't be processed")
            return

        statres = task.path.lstat()
        mode, size = statres.st_mode, statres.st_size

        if stat.S_ISDIR(mode):
            log.debug("Found directory")
            for path in task.path.iterdir():
                result.add_new_task(
                    Task(
                        root=task.root,
                        path=path,
                        depth=task.depth,
                    )
                )
            return

        elif stat.S_ISLNK(mode):
            log.debug("Ignoring symlink")
            return

        elif size == 0:
            log.debug("Ignoring empty file")
            return

        with PerfCounter(result, "Process Regular File", name=Path(task.path).name):
            _FileTask(self._config, task, size, result).process()


class _FileTask:
    def __init__(
        self,
        config: ExtractionConfig,
        task: Task,
        size: int,
        result: TaskResult,
    ):
        self.config = config
        self.task = task
        self.size = size
        self.result = result

    def process(self):
        logger.debug("Processing file", path=self.task.path, size=self.size)

        with self.task.path.open("rb") as file:
            with PerfCounter(self.result, "Search Chunks"):
                all_chunks = search_chunks_by_priority(
                    self.task.path, file, self.size, self.config.handlers, self.result
                )
            outer_chunks = remove_inner_chunks(all_chunks)
            unknown_chunks = calculate_unknown_chunks(outer_chunks, self.size)

            if outer_chunks or unknown_chunks:
                self._process_chunks(file, outer_chunks, unknown_chunks)
            else:
                # we don't consider whole files as unknown chunks, but we still want to
                # calculate entropy for whole files which produced no valid chunks
                with PerfCounter(self.result, "Calculate Entropy", whole_file=True):
                    self._calculate_entropies([self.task.path])

    def _process_chunks(
        self, file, outer_chunks: List[ValidChunk], unknown_chunks: List[UnknownChunk]
    ):
        extract_dir = make_extract_dir(
            self.task.root, self.task.path, self.config.extract_root
        )

        carved_unknown_paths = carve_unknown_chunks(extract_dir, file, unknown_chunks)
        with PerfCounter(self.result, "Calculate Entropy", whole_file=False):
            self._calculate_entropies(carved_unknown_paths)

        for chunk in outer_chunks:
            with PerfCounter(self.result, "Carve Valid Chunks"):
                self._extract_chunk(extract_dir, file, chunk)

    def _calculate_entropies(self, paths: List[Path]):
        if self.task.depth < self.config.entropy_depth:
            for path in paths:
                calculate_entropy(path, draw_plot=self.config.entropy_plot)

    def _extract_chunk(
        self,
        extract_dir: Path,
        file,
        chunk: ValidChunk,
    ):
        carved_path = carve_valid_chunk(extract_dir, file, chunk)
        inpath, outdir = get_extract_paths(extract_dir, carved_path)

        try:
            chunk.extract(inpath, outdir)
            if not self.config.keep_extracted_chunks:
                logger.debug("Removing extracted chunk", path=inpath)
                inpath.unlink()

        except ExtractError as e:
            for report in e.reports:
                self.result.add_report(report)

        except Exception as exc:
            logger.exception("Unknown error happened while extracting chunk")
            self.result.add_report(UnknownError(exception=exc))

        # we want to get consistent partial output even in case of unforeseen problems
        fix_extracted_directory(outdir, self.result)

        if outdir.exists():
            self.result.add_new_task(
                Task(
                    root=self.config.extract_root,
                    path=outdir,
                    depth=self.task.depth + 1,
                )
            )


def remove_inner_chunks(chunks: List[ValidChunk]) -> List[ValidChunk]:
    """Remove all chunks from the list which are within another bigger chunks."""
    if not chunks:
        return []

    chunks_by_size = sorted(chunks, key=attrgetter("size"), reverse=True)
    outer_chunks = [chunks_by_size[0]]
    for chunk in chunks_by_size[1:]:
        if not any(outer.contains(chunk) for outer in outer_chunks):
            outer_chunks.append(chunk)

    outer_count = len(outer_chunks)
    removed_count = len(chunks) - outer_count
    logger.debug(
        "Removed inner chunks",
        outer_chunk_count=noformat(outer_count),
        removed_inner_chunk_count=noformat(removed_count),
        _verbosity=2,
    )
    return outer_chunks


def calculate_unknown_chunks(
    chunks: List[ValidChunk], file_size: int
) -> List[UnknownChunk]:
    """Calculate the empty gaps between chunks."""
    if not chunks or file_size == 0:
        return []

    sorted_by_offset = sorted(chunks, key=attrgetter("start_offset"))

    unknown_chunks = []

    first = sorted_by_offset[0]
    if first.start_offset != 0:
        unknown_chunk = UnknownChunk(0, first.start_offset)
        unknown_chunks.append(unknown_chunk)

    for chunk, next_chunk in pairwise(sorted_by_offset):
        diff = next_chunk.start_offset - chunk.end_offset
        if diff != 0:
            unknown_chunk = UnknownChunk(
                start_offset=chunk.end_offset,
                end_offset=next_chunk.start_offset,
            )
            unknown_chunks.append(unknown_chunk)

    last = sorted_by_offset[-1]
    if last.end_offset < file_size:
        unknown_chunk = UnknownChunk(
            start_offset=last.end_offset,
            end_offset=file_size,
        )
        unknown_chunks.append(unknown_chunk)

    return unknown_chunks


def calculate_entropy(path: Path, *, draw_plot: bool):
    """Calculate and log shannon entropy divided by 8 for the file in 1mB chunks.

    Shannon entropy returns the amount of information (in bits) of some numeric
    sequence. We calculate the average entropy of byte chunks, which in theory
    can contain 0-8 bits of entropy. We normalize it for visualization to a
    0-100% scale, to make it easier to interpret the graph.
    """
    percentages = []

    # We could use the chunk size instead of another syscall,
    # but we rely on the actual file size written to the disk
    file_size = path.stat().st_size
    logger.debug("Calculating entropy for file", path=path, size=file_size)

    # Smaller chuk size would be very slow to calculate.
    # 1Mb chunk size takes ~ 3sec for a 4,5 GB file.
    buffer_size = calculate_buffer_size(
        file_size, chunk_count=80, min_limit=1024, max_limit=1024 * 1024
    )

    with path.open("rb") as file:
        for chunk in iterate_file(file, 0, file_size, buffer_size=buffer_size):
            entropy = shannon_entropy(chunk)
            entropy_percentage = round(entropy / 8 * 100, 2)
            percentages.append(entropy_percentage)

    logger.debug(
        "Entropy calculated",
        mean=round(statistics.mean(percentages), 2),
        highest=max(percentages),
        lowest=min(percentages),
    )

    if draw_plot:
        draw_entropy_plot(percentages)


def calculate_buffer_size(
    file_size, *, chunk_count: int, min_limit: int, max_limit: int
) -> int:
    """Split the file into even sized chunks, limited by lower and upper values."""
    # We don't care about floating point precision here
    buffer_size = file_size // chunk_count
    buffer_size = max(min_limit, buffer_size)
    buffer_size = min(buffer_size, max_limit)
    return buffer_size


def draw_entropy_plot(percentages: List[float]):
    plt.clear_data()
    plt.colorless()
    plt.title("Entropy distribution")
    plt.xlabel("mB")
    plt.ylabel("entropy %")

    plt.scatter(percentages, marker="dot")
    # 16 height leaves no gaps between the lines
    plt.plot_size(100, 16)
    plt.ylim(0, 100)
    # Draw ticks every 1Mb on the x axis.
    plt.xticks(range(len(percentages) + 1))
    # Always show 0% and 100%
    plt.yticks(range(0, 101, 10))

    # New line so that chart title will be aligned correctly in the next line
    logger.debug("Entropy chart", chart="\n" + plt.build(), _verbosity=3)
