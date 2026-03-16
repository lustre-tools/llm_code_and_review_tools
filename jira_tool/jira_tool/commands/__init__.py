"""Command modules for the JIRA CLI tool.

Each module defines a register(main) function that adds commands to the
main click.Group.  This keeps cli.py small while preserving the flat
``jira <command>`` interface.
"""

from . import (
    attachments,
    comments,
    config_cmds,
    filters,
    issues,
    labels,
    links,
    projects,
    search,
    users,
    watchers,
    worklogs,
)

_MODULES = [
    attachments,
    comments,
    config_cmds,
    filters,
    issues,
    labels,
    links,
    projects,
    search,
    users,
    watchers,
    worklogs,
]


def register_all(main):
    """Register every command module on *main*."""
    for mod in _MODULES:
        mod.register(main)
