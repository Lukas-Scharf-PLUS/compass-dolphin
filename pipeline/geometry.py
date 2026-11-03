"""Stitch the per-burst static layers onto the grid of dolphin's output.

The area and resolution come from ``output_options`` in the dolphin config, so
each stitched raster lines up pixel by pixel with dolphin's results. This module
is modeled on ``sweets._geometry.stitch_geometry``.

The output goes to ``<dolphin work_directory>/geometry``.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import rasterio
import yaml
from dolphin import io, stitching
from dolphin._types import Bbox
from dolphin.workflows.config import DisplacementWorkflow, OutputOptions
from shapely import from_wkt

from pipeline.displacement import GSLC_NAME_PATTERN

logger = logging.getLogger(__name__)

GEOMETRY_DIRECTORY_NAME = "geometry"
HEIGHT_FILE_NAME = "height.tif"


@dataclass(frozen=True)
class StaticLayer:
    """One dataset to stitch from the COMPASS static layer files.

    Attributes
    ----------
    name : str
        Dataset name below ``/data`` in the HDF5 file, and output file stem.
    nodata : int
        Value marking pixels without data.
    description : str
        Text stored in the band description of the output raster.
    """

    name: str
    nodata: int
    description: str


STATIC_LAYERS = (
    StaticLayer(
        "los_east", 0, "East component of LOS unit vector from target to sensor"
    ),
    StaticLayer(
        "los_north", 0, "North component of LOS unit vector from target to sensor"
    ),
    StaticLayer("local_incidence_angle", 0, "Local incidence angle in degrees"),
    StaticLayer(
        "layover_shadow_mask",
        127,
        "Layover shadow mask. 0=no layover, no shadow; 1=shadow; 2=layover; "
        "3=shadow and layover.",
    ),
)


def find_static_layer_files(gslc_files: list[Path]) -> tuple[list[Path], list[str]]:
    """Find the finished static layer file of each burst used by dolphin.

    Parameters
    ----------
    gslc_files : list[Path]
        GSLC files in the layout ``<gslc_dir>/<burst_id>/<date>/<name>.h5``.

    Returns
    -------
    tuple[list[Path], list[str]]
        The static layer files, and the burst IDs without a finished file.
    """
    burst_directories = {
        GSLC_NAME_PATTERN.fullmatch(path.name)["burst_id"]: path.parents[1]
        for path in gslc_files
    }

    static_files = []
    missing_bursts = []
    for burst_id, directory in sorted(burst_directories.items()):
        candidates = sorted(directory.glob(f"*/static_layers_{burst_id}.h5"))
        finished = [p for p in candidates if p.with_suffix(".done").exists()]
        if finished:
            static_files.append(finished[0])
        else:
            missing_bursts.append(burst_id)
    return static_files, missing_bursts


def find_dem_file(gslc_dir: Path) -> Path | None:
    """Read the DEM path from the runconfigs written by the COMPASS stage.

    A dolphin-only run has no COMPASS config, so this is where the DEM is known.

    Parameters
    ----------
    gslc_dir : Path
        The ``gslcs`` directory.

    Returns
    -------
    Path or None
        The DEM file, or None if no runconfig names an existing file.
    """
    for runconfig_file in sorted((gslc_dir / "runconfigs").glob("*.yaml")):
        runconfig = yaml.safe_load(runconfig_file.read_text())
        groups = runconfig["runconfig"]["groups"]
        dem_file = Path(groups["dynamic_ancillary_file_group"]["dem_file"])
        if dem_file.is_file():
            return dem_file
    return None


def get_output_bounds(
    output_options: OutputOptions, input_files: list[str]
) -> tuple[Bbox | None, int | None]:
    """Get the bounds to stitch to, handled in the same way as dolphin.

    Parameters
    ----------
    output_options : OutputOptions
        The validated ``output_options`` of the dolphin config.
    input_files : list[str]
        GDAL-readable input rasters.

    Returns
    -------
    tuple[Bbox or None, int or None]
        The bounds and their EPSG code. Both are None to use the full extent.
    """
    if output_options.bounds:
        return Bbox(*output_options.bounds), output_options.bounds_epsg
    if output_options.bounds_wkt:
        wkt_bounds = from_wkt(output_options.bounds_wkt).bounds
        return Bbox(*wkt_bounds), output_options.bounds_epsg
    # merge_images ignores the strides for one file without bounds. Passing the
    # file's own bounds makes it apply them, so the resolution still matches dolphin.
    if len(input_files) == 1:
        return io.get_raster_bounds(input_files[0]), io.get_raster_crs(
            input_files[0]
        ).to_epsg()
    return None, None


def stitch_static_layers(
    static_files: list[Path],
    dem_file: Path | None,
    output_options: OutputOptions,
    output_directory: Path,
) -> list[Path]:
    """Stitch all static layers, and the DEM if available, onto dolphin's grid.

    Existing output is replaced, because it may come from different bounds or
    strides.

    Parameters
    ----------
    static_files : list[Path]
        One static layer file per burst.
    dem_file : Path or None
        DEM to warp to the same grid as ``height.tif``. None skips this.
    output_options : OutputOptions
        The validated ``output_options`` of the dolphin config.
    output_directory : Path
        Directory for the stitched rasters.

    Returns
    -------
    list[Path]
        The stitched rasters.
    """
    output_directory.mkdir(parents=True, exist_ok=True)
    strides = {"x": output_options.strides.x, "y": output_options.strides.y}

    output_files = []
    for layer in STATIC_LAYERS:
        input_files = [
            io.format_nc_filename(path, ds_name=f"/data/{layer.name}")
            for path in static_files
        ]
        out_bounds, out_bounds_epsg = get_output_bounds(output_options, input_files)
        output_file = output_directory / f"{layer.name}.tif"
        logger.info("Creating %s", output_file)
        stitching.merge_images(
            input_files,
            outfile=output_file,
            driver="GTiff",
            out_bounds=out_bounds,
            out_bounds_epsg=out_bounds_epsg,
            dest_epsg=output_options.epsg,
            target_aligned_pixels=True,
            in_nodata=layer.nodata,
            out_nodata=layer.nodata,
            strides=strides,
            resample_alg="nearest",
            overwrite=True,
        )
        with rasterio.open(output_file, "r+") as raster:
            raster.set_band_description(1, layer.description)
        output_files.append(output_file)

    if dem_file is None:
        logger.warning("No DEM found in the runconfigs. Skipping %s.", HEIGHT_FILE_NAME)
        return output_files

    height_file = output_directory / HEIGHT_FILE_NAME
    logger.info("Creating %s", height_file)
    height_file.unlink(missing_ok=True)
    stitching.warp_to_match(
        input_file=dem_file,
        match_file=output_files[0],
        output_file=height_file,
        resample_alg="cubic",
    )
    output_files.append(height_file)
    return output_files


def stitch_geometry_if_available(
    dolphin_options: dict[str, Any], gslc_files: list[Path]
) -> list[Path]:
    """Stitch the geometry if every burst used by dolphin has static layers.

    If any are missing, stitching is skipped with a warning, because a partial
    result would not cover the same area as dolphin's output.

    Parameters
    ----------
    dolphin_options : dict[str, Any]
        Options returned by ``load_dolphin_options``.
    gslc_files : list[Path]
        The GSLC files that dolphin will process.

    Returns
    -------
    list[Path]
        The stitched rasters. Empty if stitching was skipped.
    """
    static_files, missing_bursts = find_static_layer_files(gslc_files)
    if missing_bursts:
        logger.warning(
            "Skipping geometry stitching: no finished static layers for %s.",
            missing_bursts,
        )
        return []

    workflow = DisplacementWorkflow(**dolphin_options, require_cslc_files=False)
    gslc_dir = gslc_files[0].parents[2]
    output_directory = Path(dolphin_options["work_directory"]) / GEOMETRY_DIRECTORY_NAME
    logger.info("Stitching static layers of %d bursts.", len(static_files))
    return stitch_static_layers(
        static_files,
        find_dem_file(gslc_dir),
        workflow.output_options,
        output_directory,
    )
