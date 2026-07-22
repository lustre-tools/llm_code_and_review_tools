"""Command modules for the Confluence CLI tool.

Each module defines a register(main) function that adds commands to the
main click.Group, keeping cli.py small.
"""

from . import meta, pages, search, spaces

_MODULES = [search, pages, spaces, meta]


def register_all(main):
    """Register every command module on *main*."""
    for mod in _MODULES:
        mod.register(main)
