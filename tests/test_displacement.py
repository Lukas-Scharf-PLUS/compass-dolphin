"""Tests for the logic around dolphin. Dolphin's workflow itself is not run here."""

import logging

import pytest
import yaml
from pydantic import ValidationError

from pipeline.displacement import (
    DisplacementError,
    find_finished_gslcs,
    load_dolphin_options,
)
from run_pipeline import create_parser

BURST_ID = "t073_154935_iw1"
OTHER_BURST_ID = "t073_154935_iw2"


def _write_dolphin_config(tmp_path, **options):
    """Write a dolphin config with a work directory and the given options."""
    config_file = tmp_path / "dolphin.yaml"
    config_file.write_text(
        yaml.safe_dump({"work_directory": str(tmp_path / "dolphin"), **options})
    )
    return config_file


def _make_gslc(gslc_dir, burst_id, date, finished=True):
    """Create an empty GSLC file, with a done marker if it is finished."""
    directory = gslc_dir / burst_id / date
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{burst_id}_{date}.h5"
    path.touch()
    if finished:
        path.with_suffix(".done").touch()
    return path


def test_load_options_works_without_gslc_files(tmp_path):
    options = load_dolphin_options(_write_dolphin_config(tmp_path))
    assert options["work_directory"] == str(tmp_path / "dolphin")
    assert "cslc_file_list" not in options


def test_config_file_is_not_modified(tmp_path):
    config_file = _write_dolphin_config(tmp_path, cslc_file_list=[])
    original_text = config_file.read_text()
    load_dolphin_options(config_file)
    assert config_file.read_text() == original_text


def test_gslcs_listed_in_config_are_ignored_with_warning(tmp_path, caplog):
    config_file = _write_dolphin_config(tmp_path, cslc_file_list=["old.h5"])
    with caplog.at_level(logging.WARNING):
        options = load_dolphin_options(config_file)
    assert "cslc_file_list" not in options
    assert "Ignoring cslc_file_list" in caplog.text


def test_misspelled_top_level_option_is_rejected(tmp_path):
    config_file = _write_dolphin_config(tmp_path, unwrap_option={})
    with pytest.raises(DisplacementError, match="unwrap_option"):
        load_dolphin_options(config_file)


def test_invalid_dolphin_value_is_rejected_before_processing(tmp_path):
    config_file = _write_dolphin_config(
        tmp_path, unwrap_options={"unwrap_method": "not_a_method"}
    )
    with pytest.raises(ValidationError):
        load_dolphin_options(config_file)


def test_missing_work_directory_is_rejected(tmp_path):
    config_file = tmp_path / "dolphin.yaml"
    config_file.write_text(yaml.safe_dump({"mask_file": None}))
    with pytest.raises(DisplacementError, match="work_directory"):
        load_dolphin_options(config_file)


def test_find_gslcs_excludes_static_layers(tmp_path):
    first = _make_gslc(tmp_path, BURST_ID, "20220101")
    second = _make_gslc(tmp_path, BURST_ID, "20220113")
    (first.parent / f"static_layers_{BURST_ID}.h5").touch()
    assert find_finished_gslcs(tmp_path) == [first, second]


def test_find_gslcs_filters_by_burst(tmp_path):
    expected = [
        _make_gslc(tmp_path, BURST_ID, "20220101"),
        _make_gslc(tmp_path, BURST_ID, "20220113"),
    ]
    _make_gslc(tmp_path, OTHER_BURST_ID, "20220101")
    assert find_finished_gslcs(tmp_path, [BURST_ID]) == expected


def test_empty_gslc_directory_fails(tmp_path):
    with pytest.raises(DisplacementError, match="Run the compass stage first"):
        find_finished_gslcs(tmp_path)


def test_requested_burst_without_gslcs_fails(tmp_path):
    _make_gslc(tmp_path, BURST_ID, "20220101")
    _make_gslc(tmp_path, BURST_ID, "20220113")
    with pytest.raises(DisplacementError, match=OTHER_BURST_ID):
        find_finished_gslcs(tmp_path, [BURST_ID, OTHER_BURST_ID])


def test_unfinished_gslc_fails(tmp_path):
    _make_gslc(tmp_path, BURST_ID, "20220101")
    _make_gslc(tmp_path, BURST_ID, "20220113", finished=False)
    with pytest.raises(DisplacementError, match="unfinished"):
        find_finished_gslcs(tmp_path)


def test_single_date_fails(tmp_path):
    _make_gslc(tmp_path, BURST_ID, "20220101")
    with pytest.raises(DisplacementError, match="at least 2 dates"):
        find_finished_gslcs(tmp_path)


def test_several_tracks_fail(tmp_path):
    for burst_id in (BURST_ID, "t044_093000_iw1"):
        _make_gslc(tmp_path, burst_id, "20220101")
        _make_gslc(tmp_path, burst_id, "20220113")
    with pytest.raises(DisplacementError, match="several tracks"):
        find_finished_gslcs(tmp_path)


def test_burst_missing_a_date_gives_warning(tmp_path, caplog):
    _make_gslc(tmp_path, BURST_ID, "20220101")
    _make_gslc(tmp_path, BURST_ID, "20220113")
    _make_gslc(tmp_path, OTHER_BURST_ID, "20220101")
    _make_gslc(tmp_path, OTHER_BURST_ID, "20220125")
    with caplog.at_level(logging.WARNING):
        find_finished_gslcs(tmp_path)
    assert "20220125" in caplog.text


def test_all_stage_requires_both_configs():
    with pytest.raises(SystemExit):
        create_parser().parse_args(["all", "--compass-config", "compass.yaml"])


def test_dolphin_stage_requires_gslc_dir():
    with pytest.raises(SystemExit):
        create_parser().parse_args(["dolphin", "--dolphin-config", "dolphin.yaml"])
