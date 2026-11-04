"""Tests for reading and validating the pipeline config file."""

import pytest
import yaml
from pydantic import ValidationError

from pipeline.config import load_config


@pytest.fixture
def config_values(tmp_path):
    """Return valid config values whose input paths exist in a temp directory."""
    dem_file = tmp_path / "dem.tif"
    burst_db_file = tmp_path / "bursts.sqlite3"
    dem_file.touch()
    burst_db_file.touch()
    return {
        "inputs": {
            "slc_dir": str(tmp_path),
            "orbit_dir": str(tmp_path),
            "dem_file": str(dem_file),
            "burst_db_file": str(burst_db_file),
        },
        "work_dir": str(tmp_path / "work"),
        "burst_ids": ["t073_154936_iw1"],
    }


def _write_and_load(tmp_path, config_values):
    """Write config values to a YAML file and load it through the public API."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.safe_dump(config_values))
    return load_config(config_file)


def test_valid_config_uses_defaults_and_derives_gslc_dir(tmp_path, config_values):
    config = _write_and_load(tmp_path, config_values)
    assert config.geocoding.x_posting_m == 5
    assert config.geocoding.y_posting_m == 10
    assert config.gslc_dir == tmp_path / "work" / "gslcs"


def test_burst_ids_are_normalized_and_deduplicated(tmp_path, config_values):
    config_values["burst_ids"] = ["T073_154936_IW2", "t073_154936_iw1", "t073_154936_iw2"]
    config = _write_and_load(tmp_path, config_values)
    assert config.burst_ids == ["t073_154936_iw1", "t073_154936_iw2"]


@pytest.mark.parametrize("bad_id", ["t73_154936_iw1", "t073_154936_iw4", "t073_154936"])
def test_malformed_burst_id_is_rejected(tmp_path, config_values, bad_id):
    config_values["burst_ids"] = [bad_id]
    with pytest.raises(ValidationError, match="Invalid burst IDs"):
        _write_and_load(tmp_path, config_values)


def test_several_tracks_are_rejected(tmp_path, config_values):
    config_values["burst_ids"] = ["t073_154936_iw1", "t044_093000_iw1"]
    with pytest.raises(ValidationError, match="several tracks"):
        _write_and_load(tmp_path, config_values)


def test_empty_burst_list_is_rejected(tmp_path, config_values):
    config_values["burst_ids"] = []
    with pytest.raises(ValidationError):
        _write_and_load(tmp_path, config_values)


def test_missing_input_path_is_rejected(tmp_path, config_values):
    config_values["inputs"]["dem_file"] = str(tmp_path / "does_not_exist.tif")
    with pytest.raises(ValidationError, match="dem_file"):
        _write_and_load(tmp_path, config_values)


def test_platforms_default_to_all(tmp_path, config_values):
    config = _write_and_load(tmp_path, config_values)
    assert config.platforms == ("S1A", "S1B", "S1C", "S1D")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("all", ("S1A", "S1B", "S1C", "S1D")),
        ("S1A", ("S1A",)),
        ("c, a", ("S1A", "S1C")),
        (["sentinel-1c", "S1D", "S1C"], ("S1C", "S1D")),
    ],
)
def test_platforms_are_normalized(tmp_path, config_values, value, expected):
    config_values["platforms"] = value
    assert _write_and_load(tmp_path, config_values).platforms == expected


def test_unknown_platform_is_rejected(tmp_path, config_values):
    config_values["platforms"] = ["S1A", "S2A"]
    with pytest.raises(ValidationError, match="Unknown satellite 'S2A'"):
        _write_and_load(tmp_path, config_values)


def test_misspelled_option_is_rejected(tmp_path, config_values):
    config_values["geocoding"] = {"n_worker": 4}
    with pytest.raises(ValidationError, match="n_worker"):
        _write_and_load(tmp_path, config_values)
