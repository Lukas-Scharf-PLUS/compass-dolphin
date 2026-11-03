"""Tests for finding static layers and choosing the stitching grid."""

import logging

import pytest
import yaml
from dolphin._types import Bbox
from dolphin.workflows.config import OutputOptions

from pipeline import geometry
from pipeline.geometry import (
    find_dem_file,
    find_static_layer_files,
    get_output_bounds,
    stitch_geometry_if_available,
)

BURST_ID = "t073_154935_iw1"
OTHER_BURST_ID = "t073_154935_iw2"
DATE = "20220101"
INPUT_FILE = "NETCDF:input.h5:/data/los_east"
UTM_33N = 32633


def _gslc_path(gslc_dir, burst_id):
    """Return where a burst's GSLC is expected, creating its directory."""
    directory = gslc_dir / burst_id / DATE
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{burst_id}_{DATE}.h5"


def _make_static_layers(gslc_dir, burst_id, finished=True):
    """Create an empty static layer file, with a done marker if it is finished."""
    path = _gslc_path(gslc_dir, burst_id).parent / f"static_layers_{burst_id}.h5"
    path.touch()
    if finished:
        path.with_suffix(".done").touch()
    return path


def _write_runconfig(gslc_dir, dem_file):
    """Write a minimal runconfig that names a DEM file."""
    directory = gslc_dir / "runconfigs"
    directory.mkdir()
    groups = {"dynamic_ancillary_file_group": {"dem_file": str(dem_file)}}
    (directory / "geo_runconfig.yaml").write_text(
        yaml.safe_dump({"runconfig": {"groups": groups}})
    )


def test_finds_static_layers_of_the_requested_bursts_only(tmp_path):
    expected = _make_static_layers(tmp_path, BURST_ID)
    _make_static_layers(tmp_path, OTHER_BURST_ID)
    found, missing = find_static_layer_files([_gslc_path(tmp_path, BURST_ID)])
    assert found == [expected]
    assert missing == []


def test_burst_without_static_layers_is_reported(tmp_path):
    found, missing = find_static_layer_files([_gslc_path(tmp_path, BURST_ID)])
    assert found == []
    assert missing == [BURST_ID]


def test_unfinished_static_layers_count_as_missing(tmp_path):
    _make_static_layers(tmp_path, BURST_ID, finished=False)
    _, missing = find_static_layer_files([_gslc_path(tmp_path, BURST_ID)])
    assert missing == [BURST_ID]


def test_dem_is_read_from_runconfig(tmp_path):
    dem_file = tmp_path / "dem.tif"
    dem_file.touch()
    _write_runconfig(tmp_path, dem_file)
    assert find_dem_file(tmp_path) == dem_file


def test_dem_that_no_longer_exists_is_not_returned(tmp_path):
    _write_runconfig(tmp_path, tmp_path / "moved.tif")
    assert find_dem_file(tmp_path) is None


def test_missing_runconfigs_give_no_dem(tmp_path):
    assert find_dem_file(tmp_path) is None


def test_bounds_use_the_epsg_from_the_dolphin_config():
    options = OutputOptions(bounds=[1, 2, 3, 4], bounds_epsg=UTM_33N)
    assert get_output_bounds(options, [INPUT_FILE]) == (Bbox(1, 2, 3, 4), UTM_33N)


def test_bounds_can_come_from_wkt():
    wkt = "POLYGON((1 2, 3 2, 3 4, 1 4, 1 2))"
    options = OutputOptions(bounds_wkt=wkt, bounds_epsg=UTM_33N)
    assert get_output_bounds(options, [INPUT_FILE]) == (Bbox(1, 2, 3, 4), UTM_33N)


def test_several_files_without_bounds_use_the_full_extent():
    assert get_output_bounds(OutputOptions(), [INPUT_FILE, INPUT_FILE]) == (None, None)


def test_single_file_without_bounds_uses_its_own_bounds(monkeypatch):
    """Without bounds, merge_images would ignore the strides for a single file."""

    class FakeCrs:
        def to_epsg(self):
            return UTM_33N

    file_bounds = Bbox(10, 20, 30, 40)
    monkeypatch.setattr(geometry.io, "get_raster_bounds", lambda _: file_bounds)
    monkeypatch.setattr(geometry.io, "get_raster_crs", lambda _: FakeCrs())
    assert get_output_bounds(OutputOptions(), [INPUT_FILE]) == (file_bounds, UTM_33N)


def test_stitching_is_skipped_with_warning_if_a_burst_is_missing(
    tmp_path, monkeypatch, caplog
):
    _make_static_layers(tmp_path, BURST_ID)

    def fail_if_called(*args, **kwargs):
        pytest.fail("A partial geometry must not be stitched.")

    monkeypatch.setattr(geometry, "stitch_static_layers", fail_if_called)
    gslc_files = [_gslc_path(tmp_path, BURST_ID), _gslc_path(tmp_path, OTHER_BURST_ID)]
    options = {"work_directory": str(tmp_path / "dolphin")}
    with caplog.at_level(logging.WARNING):
        assert stitch_geometry_if_available(options, gslc_files) == []
    assert OTHER_BURST_ID in caplog.text
