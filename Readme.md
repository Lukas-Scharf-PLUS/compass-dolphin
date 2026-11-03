# COMPASS + dolphin InSAR pipeline for Vienna

An InSAR pipeline for chosen Sentinel-1 bursts over Vienna. COMPASS turns SLCs into
geocoded burst SLCs (GSLCs), and dolphin turns those into displacement time series.
Each stage has its own YAML config file.

**This project downloads nothing.** SLCs, orbit files, the DEM and the burst
database must already exist on your machine. If something is missing, the pipeline
stops with an error message.

## Setup

```bash
mamba env create -f environment.yaml
mamba activate compass-dolphin
```

| Package | Version | Installed from |
| --- | --- | --- |
| COMPASS | 0.5.7 | GitHub tag, via pip |
| dolphin | commit `1bd5ed6` (five commits after 0.42.8) | GitHub, via pip |
| whirlwind-insar | 0.10.0 | PyPI, via pip (imported as `whirlwind`) |
| isce3 (CPU build) | 0.25.8 | conda-forge |
| GDAL / PROJ | 3.12 / 9.7 | conda-forge |

Notes:

- These are the newest versions that work with official COMPASS 0.5.7. It crashes
  on numpy 2 and needs `scipy<1.13`, so the environment stays on numpy 1.26 and
  scipy 1.12. That in turn limits isce3, GDAL, PROJ and rasterio.
- isce3 0.25.8 and 0.25.22 produced pixel-identical GSLCs and static layers in a
  test on one burst. isce3 0.24.4 differed slightly, so do not compare results
  across isce3 versions without checking.
- `pip check` reports that `imagecodecs` wants `numpy>=2.1`. You can ignore this:
  the package passed import and compression round-trip tests on numpy 1.26.
- JAX runs on the CPU and prints a warning if it detects an NVIDIA GPU.

## How to run

Run these commands from the project folder.

**Full pipeline** (COMPASS, then dolphin):

```bash
python run_pipeline.py all \
    --compass-config config/compass.yaml \
    --dolphin-config config/dolphin.yaml
```

**Only COMPASS:**

```bash
python run_pipeline.py compass --compass-config config/compass.yaml
```

**Only dolphin**, on GSLCs that already exist:

```bash
python run_pipeline.py dolphin \
    --dolphin-config config/dolphin.yaml \
    --gslc-dir ~/projects/ADUCAT/data/vienna_pipeline/ASC_073/gslcs
```

A dolphin-only run uses every burst in `--gslc-dir`, including bursts left over from
earlier runs. To choose bursts, add for example
`--burst-ids t073_154935_iw1 t073_154935_iw2`.

The `all` and `dolphin` commands also stitch the static layers before running
dolphin. To turn this off, add `--skip-geometry`. The `compass` command does not
stitch, because the area and resolution are defined in the dolphin config.

If COMPASS is interrupted or a job fails, run the same command again. Only unfinished
jobs are processed.

**Next step:** to turn dolphin's rasters into one GeoParquet point file with EGMS-style
model parameters, use the separate `dolphin_postprocessing` repository next to this
one. It has its own conda environment, because it needs numpy 2.

## How to run the tests

```bash
python -m pytest tests
```

The tests do not need Sentinel-1 data and do not run COMPASS or dolphin's workflow.

## Run with Docker

The image contains the conda environment and the code. Its entry point is
`run_pipeline.py`, so the subcommand and options go straight after the image name.
GitHub Actions (`.github/workflows/docker.yml`) builds it on every push to `main`,
runs the tests in it, and publishes it as
`ghcr.io/lukas-scharf-plus/compass-dolphin` with the tags `latest` and
`sha-<commit>`. A git tag like `v1.0.0` also publishes `1.0.0`. To build it yourself:

```bash
docker build -t compass-dolphin .
```

The config files must use paths **inside the container**, so mount your data and
config folders and point the configs at the mounted paths:

```bash
docker run --rm \
  --user "$(id -u):$(id -g)" \
  -v ~/projects/ADUCAT/data:/work \
  ghcr.io/lukas-scharf-plus/compass-dolphin:latest \
  all --compass-config /work/config/compass.yaml --dolphin-config /work/config/dolphin.yaml
```

`--user` runs the container as you, so the output belongs to you. On Argo the whole
chain (download, this pipeline, post-processing) runs as one workflow from the
separate `aducat_argo` repository.

## Config files

### `config/compass.yaml`

| Key | Meaning |
| --- | --- |
| `inputs.slc_dir` | Directory with `.SAFE` directories or `.zip` files |
| `inputs.orbit_dir` | Directory with an `.EOF` orbit file for every SLC |
| `inputs.dem_file` | DEM file readable by GDAL |
| `inputs.burst_db_file` | OPERA burst database (`.sqlite3`) |
| `work_dir` | Where COMPASS output is written |
| `burst_ids` | Bursts to process, for example `t073_154936_iw1`, from one track only |
| `geocoding.x_posting_m`, `y_posting_m` | Output pixel spacing in meters (default 5 and 10) |
| `geocoding.common_bursts_only` | Drop bursts missing on any date, with a warning (default true) |
| `geocoding.generate_static_layers` | Also create one static layer file per burst (default true) |
| `geocoding.n_workers` | Jobs running at the same time (default 2) |
| `geocoding.threads_per_worker` | CPU threads per job (default 4) |

Typos and unknown keys in this file are reported as errors.

### `config/dolphin.yaml`

A normal dolphin config listing every dolphin option, with dolphin's own explanations.
Keep `cslc_file_list` empty: the pipeline adds the GSLC files. Set `work_directory` to
where dolphin should write its output.

## How it works

**Before processing:** all config files are checked, so a mistake is reported right
away and not after hours of COMPASS processing.

**COMPASS stage** (`pipeline/geocode.py`):

1. Every SLC in `slc_dir` must have an orbit file. This matters because
   `s1_geocode_stack.py` would otherwise download missing orbits on its own.
2. `s1_geocode_stack.py` writes one runconfig and run file per burst and date. Old
   runconfigs and run files are replaced on every run.
3. A requested burst that is in none of the SLCs is an error.
4. COMPASS runs in parallel with one log file per job. A `.done` file is written only
   after a job succeeds. COMPASS can leave a full-size `.h5` behind when it crashes,
   so an existing `.h5` alone does not count as finished.
5. After full success, the temporary `gslcs/scratch` directory is deleted.

**Dolphin stage** (`pipeline/displacement.py`):

1. The GSLC list comes from the COMPASS stage or, in a dolphin-only run, from
   `--gslc-dir`. Static layer files are left out, and a GSLC without a `.done` file
   is an error.
2. The static layers of those bursts are stitched (`pipeline/geometry.py`) using the
   `bounds` or `bounds_wkt`, `bounds_epsg`, `epsg` and `strides` from
   `output_options` in the dolphin config, so the rasters match dolphin's output grid
   pixel by pixel. The DEM path is read from the COMPASS runconfigs. If any burst has
   no finished static layers, stitching is skipped with a warning, and dolphin still
   runs. Earlier stitched files are replaced.
3. A copy of your dolphin config with the GSLC files added is written to
   `<work_directory>/dolphin_config_with_gslcs.yaml`. Your config is not changed.
4. Dolphin runs on that copy, with its progress shown in the terminal.

## Output

```text
<work_dir from compass.yaml>/
├── gslcs/
│   ├── t073_154936_iw1/
│   │   ├── 20250109/
│   │   │   ├── t073_154936_iw1_20250109.h5   # GSLC for dolphin (dataset /data/VV)
│   │   │   ├── t073_154936_iw1_20250109.done
│   │   │   └── static_layers_t073_154936_iw1.h5   # only on the burst's first date
│   │   └── 20250121/
│   │       └── ...
│   ├── runconfigs/
│   └── run_files/
└── logs/

<work_directory from dolphin.yaml>/
├── dolphin_config_with_gslcs.yaml   # the config dolphin actually ran with
├── dolphin.log
├── geometry/                        # stitched onto the same grid as timeseries/
│   ├── los_east.tif
│   ├── los_north.tif
│   ├── local_incidence_angle.tif
│   ├── layover_shadow_mask.tif      # 0=none, 1=shadow, 2=layover, 3=both, 127=no data
│   └── height.tif                   # the DEM on this grid
├── interferograms/
├── unwrapped/
└── timeseries/                      # displacement in meters, and velocity.tif
```

## Project structure

- `run_pipeline.py`: command-line entry point.
- `pipeline/config.py`: reads and validates the COMPASS config.
- `pipeline/geocode.py`: the COMPASS stage.
- `pipeline/displacement.py`: the dolphin stage.
- `pipeline/geometry.py`: stitches the static layers onto dolphin's output grid.
- `pipeline/environment.py`: environment variables for COMPASS and dolphin.
- `config/compass.yaml`, `config/dolphin.yaml`: the config files.
- `tests/`: automated tests.
- `environment.yaml`: conda environment definition.
- `Dockerfile`, `.dockerignore`: container image built from `environment.yaml`.
- `.github/workflows/docker.yml`: builds, tests and publishes the image to ghcr.io.
- `CLAUDE.md`: working instructions for Claude Code.
