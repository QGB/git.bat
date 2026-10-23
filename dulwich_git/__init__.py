"""Modular implementation of the Dulwich push tool."""

from pathlib import Path

from . import push as _push
from .push import arguments, main, self_test
from .shared import setup_logging

# Keep self-test subprocesses and default commit messages anchored to the standalone script.
_push.__file__ = str(Path(__file__).resolve().parents[1] / "dulwich_push.py")

__all__ = ["arguments", "main", "self_test", "setup_logging"]
