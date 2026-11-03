"""Run dolphin on finished GSLCs, using the settings from the dolphin config.

The dolphin config is a normal dolphin YAML file with an empty ``cslc_file_list``.
This module adds the GSLC paths, writes the result to a new file in dolphin's work
directory, and runs dolphin on that file. The user's config is never modified.
"""

import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml
from dolphin.workflows.config import DisplacementWorkflow

from pipeline.environment import build_subprocess_environment

logger = logging.getLogger(__name__)

GSLC_NAME_PATTERN = re.compile(
    r"(?P<burst_id>(?P<track>t\d{3})_\d{6}_iw[123])_(?P<date>\d{8})\.h5"
)
RESOLVED_CONFIG_NAME = "dolphin_config_with_gslcs.yaml"
MINIMUM_NUMBER_OF_DATES = 2


class DisplacementError(RuntimeError):
    """The dolphin stage failed. The message explains what to check."""


def load_dolphin_options(config_file: str | Path) -> dict[str, Any]:
    """Load the dolphin config and check it before any processing starts.

    Dolphin silently ignores misspelled top-level options, so they are checked
    here. Its own validation covers everything else. GSLC files are not required
    yet, because in a full pipeline run they do not exist at this point.

    Parameters
    ----------
    config_file : str or Path
        Path to the dolphin YAML config file.

    Returns
    -------
    dict[str, Any]
        The options, without ``cslc_file_list``, and with an absolute
        ``work_directory``.

    Raises
    ------
    FileNotFoundError
        If the config file does not exist.
    DisplacementError
        If the file is empty, has unknown options or has no ``work_directory``.
    pydantic.ValidationError
        If dolphin rejects a value.
    """
    with open(config_file) as stream:
        options = yaml.safe_load(stream)
    if not isinstance(options, dict):
        raise DisplacementError(f"{config_file} does not contain any options.")

    unknown_options = sorted(set(options) - set(DisplacementWorkflow.model_fields))
    if unknown_options:
        raise DisplacementError(
            f"Unknown options in {config_file}: {unknown_options}."
        )
    if not options.get("work_directory"):
        raise DisplacementError(f"Set work_directory in {config_file}.")

    if options.pop("cslc_file_list", None):
        logger.warning(
            "Ignoring cslc_file_list in %s: the pipeline adds the GSLCs itself.",
            config_file,
        )
    options["work_directory"] = str(
        Path(options["work_directory"]).expanduser().resolve()
    )

    DisplacementWorkflow(**options, require_cslc_files=False)
    return options


def _check_gslc_coverage(dates_by_burst: dict[str, set[str]]) -> None:
    """Check that the bursts form a stack that dolphin can process.

    Parameters
    ----------
    dates_by_burst : dict[str, set[str]]
        Acquisition dates for each burst ID.

    Raises
    ------
    DisplacementError
        If the bursts span several tracks, or a burst has too few dates. A
        warning names every burst that is missing some of the dates.
    """
    tracks = sorted({burst_id.split("_")[0] for burst_id in dates_by_burst})
    if len(tracks) > 1:
        raise DisplacementError(
            f"The GSLCs span several tracks {tracks}. Choose one with --burst-ids."
        )

    too_few_dates = [
        burst_id
        for burst_id, dates in dates_by_burst.items()
        if len(dates) < MINIMUM_NUMBER_OF_DATES
    ]
    if too_few_dates:
        raise DisplacementError(
            f"Dolphin needs at least {MINIMUM_NUMBER_OF_DATES} dates per burst, "
            f"but these have fewer: {sorted(too_few_dates)}."
        )

    all_dates = set().union(*dates_by_burst.values())
    for burst_id, dates in sorted(dates_by_burst.items()):
        if dates != all_dates:
            logger.warning(
                "%s is missing on %s.", burst_id, sorted(all_dates - dates)
            )


def find_finished_gslcs(
    gslc_dir: Path, burst_ids: list[str] | None = None
) -> list[Path]:
    """Find the GSLC files for a dolphin-only run.

    Parameters
    ----------
    gslc_dir : Path
        The ``gslcs`` directory written by the COMPASS stage.
    burst_ids : list[str], optional
        Only use these bursts. By default, all bursts in the directory are used.

    Returns
    -------
    list[Path]
        The GSLC files, sorted by burst and date. Static layer files are excluded.

    Raises
    ------
    DisplacementError
        If GSLCs or requested bursts are missing, a GSLC is unfinished, or the
        files do not form a valid stack.
    """
    gslc_files = []
    dates_by_burst: dict[str, set[str]] = {}
    for path in sorted(gslc_dir.glob("*/*/*.h5")):
        match = GSLC_NAME_PATTERN.fullmatch(path.name)
        if match and (burst_ids is None or match["burst_id"] in burst_ids):
            gslc_files.append(path)
            dates_by_burst.setdefault(match["burst_id"], set()).add(match["date"])

    if not gslc_files:
        raise DisplacementError(
            f"No GSLC files found in {gslc_dir}"
            + (f" for bursts {burst_ids}." if burst_ids else ".")
            + " Run the compass stage first."
        )
    missing_bursts = sorted(set(burst_ids or []) - set(dates_by_burst))
    if missing_bursts:
        raise DisplacementError(f"No GSLC files in {gslc_dir} for {missing_bursts}.")

    # An interrupted COMPASS job can leave a full-size but broken .h5 behind.
    unfinished = [p for p in gslc_files if not p.with_suffix(".done").exists()]
    if unfinished:
        names = "\n  ".join(path.name for path in unfinished)
        raise DisplacementError(
            f"{len(unfinished)} GSLC(s) are unfinished:\n  {names}\n"
            "Run the compass stage again to complete them."
        )

    _check_gslc_coverage(dates_by_burst)
    logger.info(
        "Found %d finished GSLCs for bursts %s.",
        len(gslc_files),
        sorted(dates_by_burst),
    )
    return gslc_files


def write_config_with_gslcs(options: dict[str, Any], gslc_files: list[Path]) -> Path:
    """Write a dolphin config that contains the GSLC file paths.

    Parameters
    ----------
    options : dict[str, Any]
        Options returned by ``load_dolphin_options``.
    gslc_files : list[Path]
        The GSLC files to process.

    Returns
    -------
    Path
        The new config file inside dolphin's work directory.

    Raises
    ------
    pydantic.ValidationError
        If dolphin rejects the options together with the files.
    """
    workflow = DisplacementWorkflow(**options, cslc_file_list=gslc_files)
    work_directory = Path(options["work_directory"])
    work_directory.mkdir(parents=True, exist_ok=True)
    config_file = work_directory / RESOLVED_CONFIG_NAME
    workflow.to_yaml(config_file)
    return config_file


def run_dolphin(options: dict[str, Any], gslc_files: list[Path]) -> Path:
    """Add the GSLC files to the dolphin options and run dolphin.

    Parameters
    ----------
    options : dict[str, Any]
        Options returned by ``load_dolphin_options``.
    gslc_files : list[Path]
        The GSLC files to process.

    Returns
    -------
    Path
        Dolphin's work directory, which contains all of its output.

    Raises
    ------
    DisplacementError
        If dolphin is not installed or exits with an error.
    pydantic.ValidationError
        If dolphin rejects the options together with the files.
    """
    dolphin_program = Path(sys.executable).parent / "dolphin"
    if not dolphin_program.exists():
        raise DisplacementError(
            f"{dolphin_program} not found. "
            "Run this with the compass-dolphin environment."
        )

    config_file = write_config_with_gslcs(options, gslc_files)
    logger.info("Running dolphin on %d GSLCs with %s", len(gslc_files), config_file)

    # Dolphin's progress goes straight to the terminal, and to dolphin.log.
    result = subprocess.run(
        [str(dolphin_program), "run", str(config_file)],
        env=build_subprocess_environment(),
        check=False,
    )
    if result.returncode != 0:
        raise DisplacementError(
            f"dolphin failed with exit code {result.returncode}. "
            f"See the output above or the log in {options['work_directory']}."
        )
    return Path(options["work_directory"])
