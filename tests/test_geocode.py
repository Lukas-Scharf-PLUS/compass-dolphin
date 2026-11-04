"""Tests for the geocoding logic around COMPASS. COMPASS itself is not run here."""

import logging
import sys

import pytest
import yaml

from pipeline.config import PipelineConfig
from pipeline.environment import build_subprocess_environment
from pipeline.geocode import (
    CompassJob,
    GeocodingError,
    build_gslc_jobs,
    build_static_layer_jobs,
    check_orbits_exist,
    check_requested_bursts_found,
    drop_excluded_dates,
    find_excluded_dates,
    find_generated_bursts,
    find_slc_inputs,
    run_geocoding,
    run_jobs,
    select_common_bursts,
)

SAFE_NAME = "S1A_IW_SLC__1SDV_20220101T164306_20220101T164334_041270_04E7B4_0CF8.SAFE"
MATCHING_ORBIT = (
    "S1A_OPER_AUX_POEORB_OPOD_20220121T121549_V20211231T225942_20220102T005942.EOF"
)
OTHER_DATE_ORBIT = (
    "S1A_OPER_AUX_POEORB_OPOD_20220202T081558_V20220112T225942_20220114T005942.EOF"
)


def _make_job(tmp_path, name, script):
    """Create a job whose run file runs a small shell script instead of COMPASS."""
    run_file = tmp_path / f"{name}.sh"
    output_file = tmp_path / f"{name}.h5"
    run_file.write_text(script.format(output=output_file))
    return CompassJob(name, run_file, output_file, tmp_path / f"{name}.log")


def test_find_slc_inputs_prefers_safe_over_zip(tmp_path):
    (tmp_path / SAFE_NAME).mkdir()
    (tmp_path / "S1A_other.zip").touch()
    slcs, unzipped = find_slc_inputs(tmp_path)
    assert slcs == [tmp_path / SAFE_NAME]
    assert unzipped is True


def test_find_slc_inputs_detects_zip(tmp_path):
    (tmp_path / "S1A_other.zip").touch()
    assert find_slc_inputs(tmp_path) == ([tmp_path / "S1A_other.zip"], False)


def test_find_slc_inputs_fails_on_empty_directory(tmp_path):
    with pytest.raises(GeocodingError, match="No Sentinel-1"):
        find_slc_inputs(tmp_path)


def test_check_orbits_accepts_matching_orbit(tmp_path):
    (tmp_path / MATCHING_ORBIT).touch()
    check_orbits_exist([tmp_path / SAFE_NAME], tmp_path)


def test_check_orbits_names_slc_without_orbit(tmp_path):
    (tmp_path / OTHER_DATE_ORBIT).touch()
    with pytest.raises(GeocodingError, match=SAFE_NAME):
        check_orbits_exist([tmp_path / SAFE_NAME], tmp_path)


def test_subprocess_environment_ignores_other_conda_environments(monkeypatch):
    monkeypatch.setenv("GDAL_DRIVER_PATH", "/other_env/lib/gdalplugins")
    monkeypatch.setenv("PROJ_NETWORK", "ON")
    environment = build_subprocess_environment(threads_per_worker=3)
    assert environment["GDAL_DRIVER_PATH"] == f"{sys.prefix}/lib/gdalplugins"
    assert environment["PROJ_NETWORK"] == "OFF"
    assert environment["OMP_NUM_THREADS"] == "3"


def test_missing_orbit_stops_before_compass_can_download(tmp_path, monkeypatch):
    slc_dir = tmp_path / "slc"
    orbit_dir = tmp_path / "orbits"
    slc_dir.mkdir()
    orbit_dir.mkdir()
    (slc_dir / SAFE_NAME).mkdir()
    dem_file = tmp_path / "dem.tif"
    burst_db_file = tmp_path / "bursts.sqlite3"
    dem_file.touch()
    burst_db_file.touch()
    config = PipelineConfig(
        inputs={
            "slc_dir": slc_dir,
            "orbit_dir": orbit_dir,
            "dem_file": dem_file,
            "burst_db_file": burst_db_file,
        },
        work_dir=tmp_path / "work",
        burst_ids=["t073_154936_iw1"],
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("s1_geocode_stack.py must not run without orbits")

    monkeypatch.setattr("pipeline.geocode.subprocess.run", fail_if_called)
    with pytest.raises(GeocodingError, match="No orbit file"):
        run_geocoding(config)


S1C_SAFE_NAME = "S1C_IW_SLC__1SDV_20250608T164148_20250608T164215_002694_0058F8_5400.SAFE"


def test_all_platforms_exclude_nothing(tmp_path):
    slcs = [tmp_path / SAFE_NAME, tmp_path / S1C_SAFE_NAME]
    assert find_excluded_dates(slcs, ("S1A", "S1B", "S1C", "S1D")) == set()


def test_dates_of_not_selected_platforms_are_excluded(tmp_path, caplog):
    slcs = [tmp_path / SAFE_NAME, tmp_path / S1C_SAFE_NAME]
    with caplog.at_level(logging.INFO):
        assert find_excluded_dates(slcs, ("S1A",)) == {"20250608"}
    assert "1 S1C" in caplog.text


def test_no_slc_of_selected_platform_fails(tmp_path):
    with pytest.raises(GeocodingError, match="None of the 1 SLCs"):
        find_excluded_dates([tmp_path / SAFE_NAME], ("S1C",))


def test_same_date_from_two_platforms_fails(tmp_path):
    other_platform = "S1C" + SAFE_NAME[3:]  # same date as SAFE_NAME
    with pytest.raises(GeocodingError, match="20220101"):
        find_excluded_dates([tmp_path / SAFE_NAME, tmp_path / other_platform], ("S1A",))


def test_drop_excluded_dates_removes_their_jobs(tmp_path, caplog):
    burst_id = "t073_154936_iw1"
    for directory in ("run_files", "runconfigs"):
        (tmp_path / directory).mkdir()
    for date in ("20220101", "20220107"):
        (tmp_path / "run_files" / f"run_{date}_{burst_id}.sh").touch()
        (tmp_path / "runconfigs" / f"geo_runconfig_{date}_{burst_id}.yaml").touch()
    # A GSLC of the excluded date left over from an earlier run
    (tmp_path / burst_id / "20220107").mkdir(parents=True)
    (tmp_path / burst_id / "20220107" / f"{burst_id}_20220107.h5").touch()

    with caplog.at_level(logging.WARNING):
        remaining = drop_excluded_dates(
            tmp_path,
            {burst_id: {"20220101", "20220107"}, "t073_154935_iw1": {"20220107"}},
            {"20220107"},
        )

    assert remaining == {burst_id: {"20220101"}}
    assert [p.name for p in (tmp_path / "run_files").iterdir()] == [
        f"run_20220101_{burst_id}.sh"
    ]
    assert [p.name for p in (tmp_path / "runconfigs").iterdir()] == [
        f"geo_runconfig_20220101_{burst_id}.yaml"
    ]
    assert "A dolphin-only run would use them" in caplog.text


def test_find_generated_bursts_ignores_other_files(tmp_path):
    run_dir = tmp_path / "run_files"
    run_dir.mkdir()
    (run_dir / "run_20220101_t073_154936_iw1.sh").touch()
    (run_dir / "run_20220113_t073_154936_iw1.sh").touch()
    (run_dir / "run_unrelated.sh").touch()
    assert find_generated_bursts(tmp_path) == {
        "t073_154936_iw1": {"20220101", "20220113"}
    }


def test_requested_burst_not_in_slcs_fails():
    with pytest.raises(GeocodingError, match="t073_000000_iw1"):
        check_requested_bursts_found(
            ["t073_000000_iw1", "t073_154936_iw1"], {"t073_154936_iw1": {"20220101"}}
        )


def test_select_common_bursts_drops_incomplete_burst_with_warning(caplog):
    dates_by_burst = {
        "t073_154935_iw1": {"20220101", "20220113"},
        "t073_154936_iw1": {"20220101"},
    }
    with caplog.at_level(logging.WARNING):
        assert select_common_bursts(dates_by_burst) == ["t073_154935_iw1"]
    assert "t073_154936_iw1" in caplog.text
    assert "20220113" in caplog.text


def test_select_common_bursts_fails_if_none_are_complete():
    dates_by_burst = {
        "t073_154935_iw1": {"20220101"},
        "t073_154936_iw1": {"20220113"},
    }
    with pytest.raises(GeocodingError, match="covers every date"):
        select_common_bursts(dates_by_burst)


def test_gslc_jobs_match_compass_output_layout(tmp_path):
    jobs = build_gslc_jobs(tmp_path, tmp_path, {"t073_154936_iw1": {"20220113"}})
    assert jobs[0].output_file == (
        tmp_path / "t073_154936_iw1" / "20220113" / "t073_154936_iw1_20220113.h5"
    )
    assert jobs[0].run_file == tmp_path / "run_files" / "run_20220113_t073_154936_iw1.sh"


def test_static_job_uses_first_date_and_changes_only_product_type(tmp_path):
    burst_id = "t073_154936_iw1"
    (tmp_path / "runconfigs").mkdir()
    (tmp_path / "run_files").mkdir()
    source_runconfig = tmp_path / "runconfigs" / f"geo_runconfig_20220101_{burst_id}.yaml"
    source_runconfig.write_text(
        yaml.safe_dump(
            {
                "runconfig": {
                    "groups": {
                        "primary_executable": {"product_type": "CSLC_S1"},
                        "processing": {"polarization": "co-pol"},
                    }
                }
            }
        )
    )
    source_run_file = tmp_path / "run_files" / f"run_20220101_{burst_id}.sh"
    source_run_file.write_text(f"python s1_cslc.py {source_runconfig}\n")

    jobs = build_static_layer_jobs(tmp_path, tmp_path, {burst_id: {"20220113", "20220101"}})

    assert len(jobs) == 1
    assert jobs[0].output_file.parent == tmp_path / burst_id / "20220101"
    static_runconfig = tmp_path / "runconfigs" / f"static_runconfig_{burst_id}.yaml"
    groups = yaml.safe_load(static_runconfig.read_text())["runconfig"]["groups"]
    assert groups["primary_executable"]["product_type"] == "CSLC_S1_STATIC"
    assert groups["processing"]["polarization"] == "co-pol"
    assert str(static_runconfig) in jobs[0].run_file.read_text()


def test_successful_job_is_marked_done_and_skipped_on_restart(tmp_path):
    job = _make_job(tmp_path, "good", "echo run >> {output}\n")
    run_jobs([job], n_workers=1, threads_per_worker=1)
    run_jobs([job], n_workers=1, threads_per_worker=1)
    assert job.done_marker.exists()
    assert job.output_file.read_text() == "run\n"


def test_output_without_done_marker_is_reprocessed(tmp_path):
    job = _make_job(tmp_path, "interrupted", "echo complete > {output}\n")
    job.output_file.write_text("partial")
    run_jobs([job], n_workers=1, threads_per_worker=1)
    assert job.output_file.read_text() == "complete\n"


def test_failed_job_is_reported_without_stopping_others(tmp_path):
    bad_job = _make_job(tmp_path, "bad", "touch {output}\nexit 3\n")
    good_job = _make_job(tmp_path, "good", "touch {output}\n")
    with pytest.raises(GeocodingError, match="1 of 2 jobs failed") as error:
        run_jobs([bad_job, good_job], n_workers=1, threads_per_worker=1)
    assert str(bad_job.log_file) in str(error.value)
    assert not bad_job.done_marker.exists()
    assert good_job.done_marker.exists()


def test_job_without_output_file_counts_as_failed(tmp_path):
    job = _make_job(tmp_path, "silent", "true\n")
    with pytest.raises(GeocodingError, match="was not written"):
        run_jobs([job], n_workers=1, threads_per_worker=1)
