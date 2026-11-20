"""Tests never read the developer's own config/wbot.yaml (it holds machine-specific measurements).

Several test modules call ``load_config()`` at import time, so the default path has to be redirected
here, at conftest import, not in a fixture. A path that does not exist makes ``load_config()`` return
the built-in defaults.
"""
import tempfile
from pathlib import Path

import wbot.config as config

config.DEFAULT_CONFIG_PATH = Path(tempfile.gettempdir()) / "wbot-tests-no-such-config" / "wbot.yaml"
