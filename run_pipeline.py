#!/usr/bin/env python
"""Command-line entry point: run COMPASS, dolphin, or both.

Usage:
  python run_pipeline.py all --compass-config config/compass.yaml \
      --dolphin-config config/dolphin.yaml
  python run_pipeline.py compass --compass-config config/compass.yaml
  python run_pipeline.py dolphin --dolphin-config config/dolphin.yaml \
      --gslc-dir <work_dir>/gslcs
"""

import argparse
import logging
import os
import sys
from pathlib import Path

from pipeline.environment import build_subprocess_environment

# Geometry stitching runs GDAL and PROJ inside this process, and GDAL reads these
# settings when it is imported, so they must be set before the imports below.
os.environ.update(build_subprocess_environment())

import yaml  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from pipeline.config import load_config  # noqa: E402
from pipeline.displacement import (  # noqa: E402
    DisplacementError,
    find_finished_gslcs,
    load_dolphin_options,
    run_dolphin,
)
from pipeline.geocode import (  # noqa: E402
    GeocodingError,
    GeocodeResult,
    run_geocoding,
)
from pipeline.geometry import stitch_geometry_if_available  # noqa: E402

logger = logging.getLogger("pipeline")

SKIP_GEOMETRY_HELP = "Do not stitch the static layers onto dolphin's output grid."


def create_parser() -> argparse.ArgumentParser:
    """Create the command-line parser with one subcommand per way of running.

    Returns
    -------
    argparse.ArgumentParser
        The configured parser.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subcommands = parser.add_subparsers(dest="stage", required=True)

    run_all = subcommands.add_parser("all", help="Run COMPASS, then dolphin.")
    run_all.add_argument("--compass-config", type=Path, required=True)
    run_all.add_argument("--dolphin-config", type=Path, required=True)
    run_all.add_argument(
        "--skip-geometry", action="store_true", help=SKIP_GEOMETRY_HELP
    )

    run_compass = subcommands.add_parser("compass", help="Run only COMPASS.")
    run_compass.add_argument("--compass-config", type=Path, required=True)

    run_dolphin_only = subcommands.add_parser(
        "dolphin", help="Run only dolphin on GSLCs that already exist."
    )
    run_dolphin_only.add_argument("--dolphin-config", type=Path, required=True)
    run_dolphin_only.add_argument(
        "--gslc-dir",
        type=Path,
        required=True,
        help="The gslcs directory written by the compass stage.",
    )
    run_dolphin_only.add_argument(
        "--burst-ids",
        nargs="+",
        help="Only use these bursts. By default, all bursts in --gslc-dir are used.",
    )
    run_dolphin_only.add_argument(
        "--skip-geometry", action="store_true", help=SKIP_GEOMETRY_HELP
    )
    return parser


def _log_compass_result(result: GeocodeResult, gslc_dir: Path) -> None:
    """Log a summary of the COMPASS stage.

    Parameters
    ----------
    result : GeocodeResult
        The output files of the stage.
    gslc_dir : Path
        The directory containing them.
    """
    logger.info(
        "COMPASS finished: %d GSLCs and %d static layer files in %s",
        len(result.gslc_files),
        len(result.static_layer_files),
        gslc_dir,
    )


def run_selected_stages(args: argparse.Namespace) -> None:
    """Run the stages selected on the command line.

    All config files are checked first, so that a mistake in the dolphin config
    is reported before COMPASS spends hours processing.

    Parameters
    ----------
    args : argparse.Namespace
        The parsed command-line arguments.

    Raises
    ------
    FileNotFoundError, yaml.YAMLError, pydantic.ValidationError
        If a config file is missing or invalid.
    GeocodingError, DisplacementError
        If a stage fails.
    """
    if args.stage == "compass":
        compass_config = load_config(args.compass_config)
        _log_compass_result(run_geocoding(compass_config), compass_config.gslc_dir)
        return

    dolphin_options = load_dolphin_options(args.dolphin_config)

    if args.stage == "dolphin":
        gslc_dir = args.gslc_dir.expanduser().resolve()
        gslc_files = find_finished_gslcs(gslc_dir, args.burst_ids)
    else:
        compass_config = load_config(args.compass_config)
        result = run_geocoding(compass_config)
        _log_compass_result(result, compass_config.gslc_dir)
        gslc_files = result.gslc_files

    if not args.skip_geometry:
        stitch_geometry_if_available(dolphin_options, gslc_files)

    output_directory = run_dolphin(dolphin_options, gslc_files)
    logger.info("Dolphin finished. Output is in %s", output_directory)


def main() -> int:
    """Parse the command line and run the pipeline.

    Returns
    -------
    int
        Process exit code: 0 on success, 1 if the pipeline failed.
    """
    args = create_parser().parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s"
    )

    # Expected problems get a clear message instead of a long stack trace.
    try:
        run_selected_stages(args)
    except (
        FileNotFoundError,
        yaml.YAMLError,
        ValidationError,
        GeocodingError,
        DisplacementError,
    ) as error:
        logger.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
