"""Turn Sentinel-1 SLCs into geocoded burst SLCs (GSLCs) using COMPASS.

The stage has four steps:
1. Check that all inputs exist, so COMPASS never tries to download an orbit.
2. Call ``s1_geocode_stack.py`` to generate one runconfig and run file per
   burst and date.
3. Check the generated jobs against the requested bursts.
4. Run the jobs in parallel. Finished jobs are skipped when restarting.

Output layout, read by dolphin later::

    <work_dir>/gslcs/<burst_id>/<date>/<burst_id>_<date>.h5
    <work_dir>/gslcs/<burst_id>/<first date>/static_layers_<burst_id>.h5
"""

import logging
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import yaml
from s1reader.s1_orbit import get_orbit_file_from_list

from pipeline.config import PipelineConfig
from pipeline.environment import build_subprocess_environment

logger = logging.getLogger(__name__)

RUN_FILE_PATTERN = re.compile(r"run_(?P<date>\d{8})_(?P<burst_id>t\d{3}_\d{6}_iw[123])")
STATIC_LAYERS_PRODUCT_TYPE = "CSLC_S1_STATIC"
SLC_NAME_PATTERN = "S1[ABCDE]_*"
ORBIT_NAME_PATTERN = "S1*.EOF"
LOG_TAIL_LINES = 20


class GeocodingError(RuntimeError):
    """A geocoding step failed. The message explains what to check."""


@dataclass(frozen=True)
class CompassJob:
    """One COMPASS run that produces one output file.

    Attributes
    ----------
    name : str
        Readable job name, also used for the log file.
    run_file : Path
        Shell script that runs COMPASS.
    output_file : Path
        HDF5 file the job must produce.
    log_file : Path
        File receiving the job's terminal output.
    """

    name: str
    run_file: Path
    output_file: Path
    log_file: Path

    @property
    def done_marker(self) -> Path:
        """Empty file written only after the job has fully succeeded.

        COMPASS writes directly to its final HDF5 path, so after an interrupted
        run a file can exist without being complete. Only this marker proves
        that the file is finished.
        """
        return self.output_file.with_suffix(".done")


@dataclass(frozen=True)
class GeocodeResult:
    """Output files of the geocoding stage.

    Attributes
    ----------
    gslc_files : list[Path]
        Geocoded SLCs for dolphin, sorted by burst and date.
    static_layer_files : list[Path]
        Static layers, one per burst. Empty if they were disabled.
    """

    gslc_files: list[Path]
    static_layer_files: list[Path]


def find_slc_inputs(slc_dir: Path) -> tuple[list[Path], bool]:
    """Find the SLCs in a directory and detect whether they are unzipped.

    ``s1_geocode_stack.py`` reads either ``.SAFE`` directories or ``.zip``
    files, never both. If both exist, ``.SAFE`` is used.

    Parameters
    ----------
    slc_dir : Path
        Directory containing the SLCs.

    Returns
    -------
    tuple[list[Path], bool]
        The SLCs to process, and True if they are unzipped ``.SAFE`` directories.

    Raises
    ------
    GeocodingError
        If the directory contains no SLCs.
    """
    safe_dirs = sorted(slc_dir.glob(f"{SLC_NAME_PATTERN}.SAFE"))
    zip_files = sorted(slc_dir.glob(f"{SLC_NAME_PATTERN}.zip"))

    if safe_dirs and zip_files:
        logger.warning(
            "%s has both .SAFE and .zip SLCs. Using the %d .SAFE and ignoring "
            "the %d .zip.",
            slc_dir,
            len(safe_dirs),
            len(zip_files),
        )
    if safe_dirs:
        return safe_dirs, True
    if zip_files:
        return zip_files, False
    raise GeocodingError(f"No Sentinel-1 .SAFE or .zip SLCs found in {slc_dir}.")


def check_orbits_exist(slc_inputs: list[Path], orbit_dir: Path) -> None:
    """Check that every SLC has a matching orbit file.

    ``s1_geocode_stack.py`` downloads missing orbits on its own, which this
    project must not do. This check runs first so that it never needs to.

    Parameters
    ----------
    slc_inputs : list[Path]
        SLC ``.SAFE`` directories or ``.zip`` files.
    orbit_dir : Path
        Directory containing ``.EOF`` orbit files.

    Raises
    ------
    GeocodingError
        If at least one SLC has no orbit file. The message names all of them.
    """
    orbit_files = [str(path) for path in orbit_dir.glob(ORBIT_NAME_PATTERN)]
    missing = [
        slc.name
        for slc in slc_inputs
        if not get_orbit_file_from_list(str(slc), orbit_files)
    ]
    if missing:
        names = "\n  ".join(missing)
        raise GeocodingError(
            f"No orbit file in {orbit_dir} for {len(missing)} SLC(s):\n  {names}\n"
            "Add the orbit files or move these SLCs out of the SLC directory."
        )


def _read_log_tail(log_file: Path) -> str:
    """Return the last lines of a log file for use in an error message.

    Parameters
    ----------
    log_file : Path
        The log file to read.

    Returns
    -------
    str
        Its last ``LOG_TAIL_LINES`` lines.
    """
    lines = log_file.read_text(errors="replace").splitlines()
    return "\n".join(lines[-LOG_TAIL_LINES:])


def generate_run_files(config: PipelineConfig, unzipped: bool) -> None:
    """Generate runconfigs and run files with ``s1_geocode_stack.py``.

    Existing runconfigs and run files are deleted first. They are cheap to
    recreate, and leftovers would run bursts that are no longer requested or use
    outdated settings. Finished GSLCs are not touched.

    ``--common-bursts-only`` is deliberately not passed. The tool compares all
    bursts in each SLC instead of the requested ones, and if none are common its
    empty filter keeps everything. ``select_common_bursts`` handles this instead.

    Parameters
    ----------
    config : PipelineConfig
        The pipeline configuration.
    unzipped : bool
        True if the SLCs are ``.SAFE`` directories.

    Raises
    ------
    GeocodingError
        If the tool is not installed or exits with an error.
    """
    stack_script = Path(sys.executable).parent / "s1_geocode_stack.py"
    if not stack_script.exists():
        raise GeocodingError(
            f"{stack_script} not found. Run this with the compass-dolphin environment."
        )

    gslc_dir = config.gslc_dir
    for generated_dir in (gslc_dir / "runconfigs", gslc_dir / "run_files"):
        shutil.rmtree(generated_dir, ignore_errors=True)
    config.log_dir.mkdir(parents=True, exist_ok=True)

    command = [
        str(stack_script),
        "--slc-dir", str(config.inputs.slc_dir),
        "--dem-file", str(config.inputs.dem_file),
        "--orbit-dir", str(config.inputs.orbit_dir),
        "--working-dir", str(gslc_dir),
        "--x-spac", str(config.geocoding.x_posting_m),
        "--y-spac", str(config.geocoding.y_posting_m),
        "--burst-db-file", str(config.inputs.burst_db_file),
        "--burst-id", *config.burst_ids,
    ]  # fmt: skip
    if unzipped:
        command.append("--unzipped")

    log_file = config.log_dir / "s1_geocode_stack.log"
    logger.info("Generating run files. The log is at %s", log_file)
    with open(log_file, "w") as log:
        result = subprocess.run(
            command,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=build_subprocess_environment(),
            check=False,
        )
    if result.returncode != 0:
        raise GeocodingError(
            f"s1_geocode_stack.py failed with exit code {result.returncode}. "
            f"End of {log_file}:\n{_read_log_tail(log_file)}"
        )


def find_generated_bursts(gslc_dir: Path) -> dict[str, set[str]]:
    """List which bursts and dates have a generated run file.

    Parameters
    ----------
    gslc_dir : Path
        The GSLC directory containing ``run_files``.

    Returns
    -------
    dict[str, set[str]]
        Acquisition dates (YYYYMMDD) for each burst ID.
    """
    dates_by_burst: dict[str, set[str]] = {}
    for run_file in (gslc_dir / "run_files").glob("run_*.sh"):
        match = RUN_FILE_PATTERN.fullmatch(run_file.stem)
        if match:
            dates_by_burst.setdefault(match["burst_id"], set()).add(match["date"])
    return dates_by_burst


def check_requested_bursts_found(
    requested_burst_ids: list[str], dates_by_burst: dict[str, set[str]]
) -> None:
    """Check that the SLCs contain every requested burst.

    ``s1_geocode_stack.py`` silently ignores burst IDs it does not find.

    Parameters
    ----------
    requested_burst_ids : list[str]
        Burst IDs from the config file.
    dates_by_burst : dict[str, set[str]]
        Dates found for each burst.

    Raises
    ------
    GeocodingError
        If any requested burst is in none of the SLCs.
    """
    missing = sorted(set(requested_burst_ids) - set(dates_by_burst))
    if missing:
        raise GeocodingError(
            f"These burst IDs are in none of the SLCs: {missing}. "
            f"Bursts found: {sorted(dates_by_burst)}."
        )


def select_common_bursts(dates_by_burst: dict[str, set[str]]) -> list[str]:
    """Select the bursts that exist on every date of the stack.

    Parameters
    ----------
    dates_by_burst : dict[str, set[str]]
        Dates found for each requested burst.

    Returns
    -------
    list[str]
        Sorted IDs of the bursts with complete coverage. A warning names every
        dropped burst and the dates it is missing.

    Raises
    ------
    GeocodingError
        If no burst covers every date.
    """
    all_dates = set().union(*dates_by_burst.values())
    complete = sorted(b for b, dates in dates_by_burst.items() if dates == all_dates)

    for burst_id in sorted(set(dates_by_burst) - set(complete)):
        missing_dates = sorted(all_dates - dates_by_burst[burst_id])
        logger.warning("Dropping %s: missing on %s.", burst_id, missing_dates)

    if not complete:
        raise GeocodingError(
            "No requested burst covers every date. Remove the incomplete dates "
            "or set geocoding.common_bursts_only to false."
        )
    return complete


def _remove_run_files(gslc_dir: Path, burst_id: str) -> None:
    """Delete a dropped burst's generated files, so they match the selection.

    Parameters
    ----------
    gslc_dir : Path
        The GSLC directory.
    burst_id : str
        The burst to remove.
    """
    for run_file in (gslc_dir / "run_files").glob(f"run_*_{burst_id}.sh"):
        run_file.unlink()
    for runconfig in (gslc_dir / "runconfigs").glob(f"geo_runconfig_*_{burst_id}.yaml"):
        runconfig.unlink()


def build_gslc_jobs(
    gslc_dir: Path, log_dir: Path, dates_by_burst: dict[str, set[str]]
) -> list[CompassJob]:
    """Create one geocoding job per burst and date.

    Parameters
    ----------
    gslc_dir : Path
        The GSLC directory.
    log_dir : Path
        The directory for log files.
    dates_by_burst : dict[str, set[str]]
        Dates to process for each burst.

    Returns
    -------
    list[CompassJob]
        The jobs, sorted by burst and date.
    """
    return [
        CompassJob(
            name=f"{burst_id}_{date}",
            run_file=gslc_dir / "run_files" / f"run_{date}_{burst_id}.sh",
            output_file=gslc_dir / burst_id / date / f"{burst_id}_{date}.h5",
            log_file=log_dir / f"gslc_{burst_id}_{date}.log",
        )
        for burst_id, dates in sorted(dates_by_burst.items())
        for date in sorted(dates)
    ]


def build_static_layer_jobs(
    gslc_dir: Path, log_dir: Path, dates_by_burst: dict[str, set[str]]
) -> list[CompassJob]:
    """Create one static layer job per burst, based on its first date.

    The burst geometry barely changes between dates, so one date per burst is
    enough. Each job gets a copy of that date's runconfig and run file with only
    the product type changed.

    Parameters
    ----------
    gslc_dir : Path
        The GSLC directory.
    log_dir : Path
        The directory for log files.
    dates_by_burst : dict[str, set[str]]
        Dates to process for each burst.

    Returns
    -------
    list[CompassJob]
        One job per burst, sorted by burst.
    """
    jobs = []
    for burst_id, dates in sorted(dates_by_burst.items()):
        first_date = min(dates)
        source_runconfig = (
            gslc_dir / "runconfigs" / f"geo_runconfig_{first_date}_{burst_id}.yaml"
        )
        static_runconfig = gslc_dir / "runconfigs" / f"static_runconfig_{burst_id}.yaml"
        runconfig = yaml.safe_load(source_runconfig.read_text())
        runconfig["runconfig"]["groups"]["primary_executable"]["product_type"] = (
            STATIC_LAYERS_PRODUCT_TYPE
        )
        static_runconfig.write_text(yaml.safe_dump(runconfig))

        source_run_file = gslc_dir / "run_files" / f"run_{first_date}_{burst_id}.sh"
        static_run_file = gslc_dir / "run_files" / f"static_{burst_id}.sh"
        static_run_file.write_text(
            source_run_file.read_text().replace(
                str(source_runconfig), str(static_runconfig)
            )
        )

        jobs.append(
            CompassJob(
                name=f"static_layers_{burst_id}",
                run_file=static_run_file,
                output_file=(
                    gslc_dir / burst_id / first_date / f"static_layers_{burst_id}.h5"
                ),
                log_file=log_dir / f"static_layers_{burst_id}.log",
            )
        )
    return jobs


def _run_job(job: CompassJob, threads_per_worker: int) -> None:
    """Run one COMPASS job and mark it as done if it succeeds.

    Parameters
    ----------
    job : CompassJob
        The job to run.
    threads_per_worker : int
        Number of CPU threads for this job.

    Raises
    ------
    GeocodingError
        If COMPASS fails or does not write the expected output file.
    """
    # A marker from an earlier run must not survive if this run gets interrupted.
    job.done_marker.unlink(missing_ok=True)
    with open(job.log_file, "w") as log:
        result = subprocess.run(
            ["bash", str(job.run_file)],
            stdout=log,
            stderr=subprocess.STDOUT,
            env=build_subprocess_environment(threads_per_worker),
            check=False,
        )
    if result.returncode != 0:
        raise GeocodingError(f"exit code {result.returncode}")
    if not job.output_file.exists():
        raise GeocodingError(f"exit code 0, but {job.output_file} was not written")
    job.done_marker.touch()


def run_jobs(jobs: list[CompassJob], n_workers: int, threads_per_worker: int) -> None:
    """Run jobs in parallel, skipping those that already finished.

    A failed job does not stop the others, so one bad burst does not waste a long
    run. All failures are reported together at the end.

    Parameters
    ----------
    jobs : list[CompassJob]
        The jobs to run.
    n_workers : int
        Number of jobs to run at the same time.
    threads_per_worker : int
        Number of CPU threads per job.

    Raises
    ------
    GeocodingError
        If any job failed. The message names each job and its log file.
    """
    todo = [job for job in jobs if not job.done_marker.exists()]
    logger.info("%d of %d jobs already finished.", len(jobs) - len(todo), len(jobs))

    failures = []
    start_time = time.perf_counter()
    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = {
            executor.submit(_run_job, job, threads_per_worker): job for job in todo
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            job = futures[future]
            minutes = (time.perf_counter() - start_time) / 60
            try:
                future.result()
            except (GeocodingError, OSError) as error:
                failures.append(f"{job.name}: {error} (log: {job.log_file})")
                logger.error("[%d/%d] FAILED %s", completed, len(todo), job.name)
            else:
                logger.info(
                    "[%d/%d] finished %s (%.1f min elapsed)",
                    completed,
                    len(todo),
                    job.name,
                    minutes,
                )

    if failures:
        details = "\n  ".join(failures)
        raise GeocodingError(
            f"{len(failures)} of {len(todo)} jobs failed:\n  {details}\n"
            "Run the pipeline again to retry only the unfinished jobs."
        )


def run_geocoding(config: PipelineConfig) -> GeocodeResult:
    """Run the complete geocoding stage.

    Parameters
    ----------
    config : PipelineConfig
        The pipeline configuration.

    Returns
    -------
    GeocodeResult
        The finished output files.

    Raises
    ------
    GeocodingError
        If inputs are missing, requested bursts are not found, or a job fails.
    """
    slc_inputs, unzipped = find_slc_inputs(config.inputs.slc_dir)
    logger.info("Found %d SLCs in %s", len(slc_inputs), config.inputs.slc_dir)
    check_orbits_exist(slc_inputs, config.inputs.orbit_dir)

    generate_run_files(config, unzipped)
    dates_by_burst = find_generated_bursts(config.gslc_dir)
    check_requested_bursts_found(config.burst_ids, dates_by_burst)

    if config.geocoding.common_bursts_only:
        selected_bursts = select_common_bursts(dates_by_burst)
        for burst_id in set(dates_by_burst) - set(selected_bursts):
            _remove_run_files(config.gslc_dir, burst_id)
        dates_by_burst = {b: dates_by_burst[b] for b in selected_bursts}

    gslc_jobs = build_gslc_jobs(config.gslc_dir, config.log_dir, dates_by_burst)
    static_jobs = []
    if config.geocoding.generate_static_layers:
        static_jobs = build_static_layer_jobs(
            config.gslc_dir, config.log_dir, dates_by_burst
        )

    logger.info(
        "Processing %d bursts: %d GSLC and %d static layer jobs.",
        len(dates_by_burst),
        len(gslc_jobs),
        len(static_jobs),
    )
    run_jobs(
        gslc_jobs + static_jobs,
        config.geocoding.n_workers,
        config.geocoding.threads_per_worker,
    )

    # Reached only on success. After a failure the files help with debugging.
    shutil.rmtree(config.gslc_dir / "scratch", ignore_errors=True)

    return GeocodeResult(
        gslc_files=[job.output_file for job in gslc_jobs],
        static_layer_files=[job.output_file for job in static_jobs],
    )
