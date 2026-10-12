"""Per-user configuration/cache locations on the three desktop platforms."""

from pathlib import Path
import sys
import os
from platformdirs import user_config_dir, user_cache_dir, user_data_dir


def config_dir():
    if sys.platform == "linux":
        return (
            Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
            / "ember-launcher"
        )
    return Path(user_config_dir("EmberLauncher", appauthor=False))


def cache_dir():
    if sys.platform == "linux":
        return (
            Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
            / "ember-launcher"
        )
    return Path(user_cache_dir("EmberLauncher", appauthor=False))


def data_dir():
    if sys.platform == "linux":
        return (
            Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
            / "ember-launcher"
        )
    return Path(user_data_dir("EmberLauncher", appauthor=False))


def default_root():
    return data_dir() / (
        "library/Smilegate" if sys.platform == "win32" else "prefix/drive_c/Smilegate"
    )
