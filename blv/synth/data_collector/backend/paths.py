"""Pure path derivation helpers for the BLV Synth Data Collector.

All filesystem paths used by the extension fan out from a small set of
project-level strings: ``root_folder``, ``class_name``, ``environment``
and (optionally) ``location``.  This module centralizes the rules for
deriving directories from those inputs so they can be unit-tested without
Isaac Sim on the PYTHONPATH.

The layout is::

    {root}/{class}/{env}/                         class-env base
    {root}/{class}/{env}/{location}/              location base (optional)
    {root}/{class}/{env}/{location}/trajectories/ trajectory JSONs
    {root}/{class}/{env}/{location}/{run}/        capture run folder
    {root}/{class}/{env}/{location}/{run}/{traj}/ per-trajectory captures
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Tuple


# --------------------------------------------------------------------- #
#  Basic helpers                                                         #
# --------------------------------------------------------------------- #


def normalize(path: str) -> str:
    """Expand ``~`` and normalize a filesystem path."""
    if not path:
        return ""
    return os.path.normpath(os.path.expanduser(path))


def sanitize_folder_name(name: str) -> str:
    """Make *name* safe to use as a single folder component.

    Replaces any character that isn't alphanumeric, underscore, dash or
    period with an underscore.  Returns an empty string when given one.
    """
    if not name:
        return ""
    return "".join(c if (c.isalnum() or c in "_-.") else "_" for c in name)


# --------------------------------------------------------------------- #
#  Directory composition                                                 #
# --------------------------------------------------------------------- #


def class_env_dir(root: str, class_name: str, environment: str) -> str:
    """Return ``{root}/{class}/{environment}``.

    *root* is normalized (``~`` expansion).  *class_name* and
    *environment* are joined as-is — callers are expected to have
    validated them.  An empty *class_name* or *environment* is joined
    literally (producing ``{root}``) — it is the caller's responsibility
    to ensure both are populated before using the result for I/O.
    """
    return os.path.join(normalize(root), class_name, environment)


def location_dir(
    root: str, class_name: str, environment: str, location: str
) -> str:
    """Return the per-location base directory.

    Falls back to :func:`class_env_dir` when *location* is empty.
    """
    base = class_env_dir(root, class_name, environment)
    return os.path.join(base, location) if location else base


def trajectories_dir(
    root: str, class_name: str, environment: str, location: str = ""
) -> str:
    """Return ``{location_dir}/trajectories``."""
    return os.path.join(
        location_dir(root, class_name, environment, location), "trajectories"
    )


def run_dir(base_dir: str, run_name: str, traj_stem: str = "") -> str:
    """Build a capture-run directory, optionally with a trajectory subfolder.

    * With ``traj_stem == ""``: returns ``{base_dir}/{run_name}``.
    * With a non-empty stem: returns ``{base_dir}/{run_name}/{traj_stem}``.
    """
    out = os.path.join(base_dir, run_name)
    if traj_stem:
        out = os.path.join(out, traj_stem)
    return out


# --------------------------------------------------------------------- #
#  ``default_N`` allocation                                              #
# --------------------------------------------------------------------- #


def next_default_run_name(base_dir: str) -> str:
    """Pick the next ``default_N`` folder name under *base_dir*.

    Scans *base_dir* for entries matching ``default_<int>`` and returns
    ``default_{max+1}``.  If nothing matches (or the directory is
    missing), returns ``default_1``.  Non-numeric suffixes are skipped.
    """
    next_n = 1
    if base_dir and os.path.isdir(base_dir):
        existing: List[int] = []
        try:
            entries = os.listdir(base_dir)
        except OSError:
            entries = []
        for entry in entries:
            if entry.startswith("default_"):
                suffix = entry[len("default_"):]
                try:
                    existing.append(int(suffix))
                except ValueError:
                    continue
        if existing:
            next_n = max(existing) + 1
    return f"default_{next_n}"


# --------------------------------------------------------------------- #
#  Collect-all planning                                                  #
# --------------------------------------------------------------------- #


@dataclass(frozen=True)
class EnvPlan:
    """One environment's entry in a collect-all plan.

    Attributes
    ----------
    env_name:
        The environment folder name inside ``environments_folder``.
    usd_path:
        Absolute path to the environment's ``{env}.usd`` scene.
    locations:
        Ordered mapping of ``location_name → [trajectory_file, ...]``.
        Trajectory filenames are sorted for deterministic capture order.
    """

    env_name: str
    usd_path: str
    locations: Dict[str, List[str]] = field(default_factory=dict)

    @property
    def total_trajectories(self) -> int:
        return sum(len(v) for v in self.locations.values())


def plan_collect_all(
    root: str,
    class_name: str,
    envs_folder: str,
) -> List[EnvPlan]:
    """Discover the full collect-all work plan.

    Walks *envs_folder* looking for ``{env}/{env}.usd`` scenes.  For
    each, looks at ``{root}/{class_name}/{env}/<loc>/`` for locations
    that have both a ``location.json`` and a non-empty
    ``trajectories/`` folder; those locations' ``.json`` trajectory
    files make up the work list.

    Environments with no qualifying locations are dropped — they would
    produce nothing during capture.

    The result is deterministic: environments, locations and trajectory
    filenames are all sorted lexicographically.  This matters because
    it keeps captures from silently reordering between runs, which
    would make it impossible to diff outputs against a previous run.
    """
    plans: List[EnvPlan] = []
    root_n = normalize(root)
    envs_n = normalize(envs_folder)
    if not class_name or not envs_n or not os.path.isdir(envs_n):
        return plans

    for env_name in sorted(os.listdir(envs_n)):
        usd_path = os.path.join(envs_n, env_name, f"{env_name}.usd")
        if not os.path.isfile(usd_path):
            continue

        data_dir = os.path.join(root_n, class_name, env_name)
        if not os.path.isdir(data_dir):
            continue

        loc_map: Dict[str, List[str]] = {}
        for loc_name in sorted(os.listdir(data_dir)):
            loc_d = os.path.join(data_dir, loc_name)
            if not os.path.isdir(loc_d):
                continue
            loc_json = os.path.join(loc_d, "location.json")
            traj_d = os.path.join(loc_d, "trajectories")
            if not (os.path.isfile(loc_json) and os.path.isdir(traj_d)):
                continue
            traj_files = sorted(
                f for f in os.listdir(traj_d) if f.endswith(".json")
            )
            if traj_files:
                loc_map[loc_name] = traj_files

        if loc_map:
            plans.append(EnvPlan(env_name=env_name, usd_path=usd_path, locations=loc_map))

    return plans


def plan_totals(plans: List[EnvPlan]) -> Tuple[int, int, int]:
    """Return ``(n_envs, n_locations, n_trajectories)`` for progress displays."""
    n_envs = len(plans)
    n_locs = sum(len(p.locations) for p in plans)
    n_trajs = sum(p.total_trajectories for p in plans)
    return n_envs, n_locs, n_trajs
