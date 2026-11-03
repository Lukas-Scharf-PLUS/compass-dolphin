"""Environment variables for the external programs the pipeline starts."""

import os
import sys
from pathlib import Path


def build_subprocess_environment(
    threads_per_worker: int | None = None,
) -> dict[str, str]:
    """Build the environment variables for running COMPASS or dolphin.

    Run files call plain ``python``, so this environment's interpreter must come
    first on PATH, even if the conda environment was not activated. GDAL and PROJ
    settings are pinned too: settings inherited from another active environment
    make GDAL load incompatible plugins, which crashes COMPASS. PROJ's network
    access is turned off because this project must not download anything.

    Parameters
    ----------
    threads_per_worker : int, optional
        Number of CPU threads per job. If None, the default is kept.

    Returns
    -------
    dict[str, str]
        Environment variables for ``subprocess.run``.
    """
    environment = os.environ.copy()
    python_bin_dir = str(Path(sys.executable).parent)
    environment["PATH"] = f"{python_bin_dir}{os.pathsep}{environment['PATH']}"
    environment["GDAL_DRIVER_PATH"] = f"{sys.prefix}/lib/gdalplugins"
    environment["GDAL_DATA"] = f"{sys.prefix}/share/gdal"
    environment["PROJ_DATA"] = f"{sys.prefix}/share/proj"
    environment["PROJ_NETWORK"] = "OFF"
    if threads_per_worker is not None:
        environment["OMP_NUM_THREADS"] = str(threads_per_worker)
    return environment
