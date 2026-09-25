"""Borg backup monitor and repository browser, split by concern: shell plumbing, output parsing, check tracking, the plugin, and its file browser."""

from vigil.plugins.borg.plugin import Borg
from vigil.plugins.borg.shell import _frame_line

__all__ = ['Borg', '_frame_line']
