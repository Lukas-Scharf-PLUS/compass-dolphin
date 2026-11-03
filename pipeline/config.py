"""Read and validate the YAML config file that controls the pipeline.

Validating everything up front means a typo in a path or burst ID fails within
seconds, instead of after hours of processing.
"""

import re
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    DirectoryPath,
    Field,
    FilePath,
    field_validator,
)

# Example burst ID: t073_154936_iw1 (track, burst number, subswath).
BURST_ID_PATTERN = re.compile(r"t\d{3}_\d{6}_iw[123]")


def _absolute_path(path: str) -> Path:
    """Expand ``~`` and make a path absolute.

    The generated COMPASS runconfigs must stay valid from any working directory.

    Parameters
    ----------
    path : str
        Path as written in the config file.

    Returns
    -------
    Path
        The absolute path.
    """
    return Path(path).expanduser().resolve()


def _track_of(burst_id: str) -> str:
    """Return the track part of a burst ID, for example ``t073``.

    Parameters
    ----------
    burst_id : str
        A valid burst ID.

    Returns
    -------
    str
        The text before the first underscore.
    """
    return burst_id.split("_")[0]


InputDirectory = Annotated[DirectoryPath, BeforeValidator(_absolute_path)]
InputFile = Annotated[FilePath, BeforeValidator(_absolute_path)]
OutputDirectory = Annotated[Path, AfterValidator(lambda path: _absolute_path(str(path)))]


class InputPaths(BaseModel, extra="forbid"):
    """Existing input data. The pipeline never downloads any of it."""

    slc_dir: InputDirectory
    orbit_dir: InputDirectory
    dem_file: InputFile
    burst_db_file: InputFile


class GeocodingOptions(BaseModel, extra="forbid"):
    """Settings for the COMPASS geocoding stage."""

    x_posting_m: float = Field(default=5, gt=0)
    y_posting_m: float = Field(default=10, gt=0)
    # Dolphin needs every burst on every date to build a consistent stack.
    common_bursts_only: bool = True
    # Layover/shadow masks and line-of-sight geometry, one file per burst.
    generate_static_layers: bool = True
    # Total CPU use is roughly n_workers * threads_per_worker.
    n_workers: int = Field(default=2, ge=1)
    threads_per_worker: int = Field(default=4, ge=1)


class PipelineConfig(BaseModel, extra="forbid"):
    """Complete pipeline configuration, matching the YAML file layout."""

    inputs: InputPaths
    work_dir: OutputDirectory
    burst_ids: list[str] = Field(min_length=1)
    geocoding: GeocodingOptions = GeocodingOptions()

    @field_validator("burst_ids")
    @classmethod
    def _check_burst_ids(cls, burst_ids: list[str]) -> list[str]:
        """Normalize burst IDs and check that they form one stack.

        Parameters
        ----------
        burst_ids : list[str]
            Burst IDs as written in the config file.

        Returns
        -------
        list[str]
            Sorted, lower-case burst IDs without duplicates.

        Raises
        ------
        ValueError
            If an ID is malformed, or the IDs belong to more than one track.
        """
        normalized = sorted({burst_id.strip().lower() for burst_id in burst_ids})

        malformed = [b for b in normalized if not BURST_ID_PATTERN.fullmatch(b)]
        if malformed:
            raise ValueError(
                f"Invalid burst IDs {malformed}. Expected a form like t073_154936_iw1."
            )

        # Different tracks view the ground from different geometries and on
        # different dates, so dolphin cannot combine them in one stack.
        tracks = sorted({_track_of(burst_id) for burst_id in normalized})
        if len(tracks) > 1:
            raise ValueError(
                f"Burst IDs span several tracks {tracks}. "
                "Use one config file per track."
            )
        return normalized

    @property
    def gslc_dir(self) -> Path:
        """Directory holding the geocoded SLCs that dolphin reads."""
        return self.work_dir / "gslcs"

    @property
    def log_dir(self) -> Path:
        """Directory holding one log file per processing job."""
        return self.work_dir / "logs"


def load_config(config_file: str | Path) -> PipelineConfig:
    """Load and validate a pipeline config file.

    Parameters
    ----------
    config_file : str or Path
        Path to the YAML config file.

    Returns
    -------
    PipelineConfig
        The validated configuration.

    Raises
    ------
    FileNotFoundError
        If the config file does not exist.
    pydantic.ValidationError
        If a value is missing, invalid, or a path does not exist.
    """
    with open(config_file) as stream:
        return PipelineConfig.model_validate(yaml.safe_load(stream))
