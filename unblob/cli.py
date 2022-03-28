#!/usr/bin/env python3
import sys
from pathlib import Path
from typing import Optional, Tuple

import click
from structlog import get_logger

from unblob.plugins import UnblobPluginManager
from unblob.profiling import to_speedscope
from unblob.report import Reports, Severity

from .cli_options import verbosity_option
from .dependencies import get_dependencies, pretty_format_dependencies
from .handlers import BUILTIN_HANDLERS, Handlers
from .logging import configure_logger, noformat
from .processing import (
    DEFAULT_DEPTH,
    DEFAULT_PROCESS_NUM,
    ExtractionConfig,
    process_file,
)

logger = get_logger()


def show_external_dependencies(
    ctx: click.Context, _param: click.Option, value: bool
) -> None:
    if not value or ctx.resilient_parsing:
        return

    plugin_manager = ctx.params["plugin_manager"]
    plugins_path = ctx.params.get(
        "plugins_path"
    )  # may not exist, depends on parameter order...
    plugin_manager.import_plugins(plugins_path)
    extra_handlers = plugin_manager.load_handlers_from_plugins()
    handlers = ctx.params["handlers"].with_prepended(extra_handlers)

    dependencies = get_dependencies(handlers)
    text = pretty_format_dependencies(dependencies)
    exit_code = 0 if all(dep.is_installed for dep in dependencies) else 1

    click.echo(text)
    ctx.exit(code=exit_code)


def get_help_text():
    dependencies = get_dependencies(BUILTIN_HANDLERS)
    lines = [
        "A tool for getting information out of any kind of binary blob.",
        "",
        "You also need these extractor commands to be able to extract the supported file types:",
        ", ".join(dep.command for dep in dependencies),
        "",
        "NOTE: Some older extractors might not be compatible.",
    ]
    return "\n".join(lines)


class UnblobContext(click.Context):
    def __init__(
        self,
        *args,
        handlers: Optional[Handlers] = None,
        plugin_manager: Optional[UnblobPluginManager] = None,
        **kwargs
    ):
        super().__init__(*args, **kwargs)
        handlers = handlers or BUILTIN_HANDLERS
        plugin_manager = plugin_manager or UnblobPluginManager()

        self.params["handlers"] = handlers
        self.params["plugin_manager"] = plugin_manager


@click.command(help=get_help_text())
@click.argument(
    "files",
    nargs=-1,
    type=click.Path(path_type=Path, exists=True, resolve_path=True),
    required=True,
)
@click.option(
    "-e",
    "--extract-dir",
    "extract_root",
    type=click.Path(path_type=Path, dir_okay=True, file_okay=False, resolve_path=True),
    default=Path.cwd(),
    help="Extract the files to this directory. Will be created if doesn't exist.",
)
@click.option(
    "-d",
    "--depth",
    default=DEFAULT_DEPTH,
    type=click.IntRange(1),
    show_default=True,
    help="Recursion depth. How deep should we extract containers.",
)
@click.option(
    "-n",
    "--entropy-depth",
    type=click.IntRange(0),
    default=1,
    show_default=True,
    help=(
        "Entropy calculation depth. How deep should we calculate entropy for unknown files? "
        "1 means input files only, 0 turns it off."
    ),
)
@click.option(
    "-P",
    "--plugins-path",
    type=click.Path(path_type=Path, exists=True, resolve_path=True),
    default=None,
    help="Load plugins from the provided path.",
    show_default=True,
)
@click.option(
    "--profile",
    "profile_path",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Write Speedscope profile files of extraction at this path.",
)
@click.option(
    "-p",
    "--process-num",
    "process_num",
    type=click.IntRange(1),
    default=DEFAULT_PROCESS_NUM,
    help="Number of worker processes to process files parallelly.",
    show_default=True,
)
@click.option(
    "-k",
    "--keep-extracted-chunks",
    "keep_extracted_chunks",
    is_flag=True,
    show_default=True,
    help="Keep extracted chunks",
)
@verbosity_option
@click.option(
    "--show-external-dependencies",
    help="Shows commands needs to be available for unblob to work properly",
    is_flag=True,
    callback=show_external_dependencies,
    expose_value=False,
)
def cli(
    files: Tuple[Path],
    extract_root: Path,
    depth: int,
    entropy_depth: int,
    process_num: int,
    keep_extracted_chunks: bool,
    verbose: int,
    plugins_path: Optional[Path],
    profile_path: Optional[Path],
    handlers: Handlers,
    plugin_manager: UnblobPluginManager,
) -> Reports:
    configure_logger(verbose, extract_root)

    plugin_manager.import_plugins(plugins_path)
    extra_handlers = plugin_manager.load_handlers_from_plugins()
    handlers = handlers.with_prepended(extra_handlers)

    config = ExtractionConfig(
        extract_root=extract_root,
        max_depth=depth,
        entropy_depth=entropy_depth,
        entropy_plot=bool(verbose >= 3),
        process_num=process_num,
        handlers=handlers,
        keep_extracted_chunks=keep_extracted_chunks,
    )

    logger.info("Start processing files", count=noformat(len(files)))
    all_reports = Reports()
    for path in files:
        report = process_file(config, path)
        all_reports.extend(report)

    if profile_path:
        with profile_path.open("w") as fd:
            to_speedscope(all_reports, fd)

    return all_reports


cli.context_class = UnblobContext


def get_exit_code_from_reports(reports: Reports) -> int:
    severity_to_exit_code = [
        (Severity.ERROR, 1),
        (Severity.WARNING, 0),
    ]
    severities = {error.severity for error in reports.errors}

    for severity, exit_code in severity_to_exit_code:
        if severity in severities:
            return exit_code

    return 0


def main():
    try:
        # Click argument parsing
        ctx = cli.make_context("unblob", sys.argv[1:])
    except click.ClickException as e:
        e.show()
        sys.exit(e.exit_code)
    except click.exceptions.Exit as e:
        sys.exit(e.exit_code)
    except Exception:
        logger.exception("Unhandled exception during unblob")
        sys.exit(1)

    try:
        with ctx:
            reports = cli.invoke(ctx)
    except Exception:
        logger.exception("Unhandled exception during unblob")
        sys.exit(1)

    sys.exit(get_exit_code_from_reports(reports))


if __name__ == "__main__":
    main()
