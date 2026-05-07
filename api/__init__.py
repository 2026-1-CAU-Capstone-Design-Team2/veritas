"""VERITAS package."""

from .api import app
from .main import cli_main, main

__all__ = ["app", "cli_main", "main"]
