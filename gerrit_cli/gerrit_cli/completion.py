"""Bash completion for the gerrit CLI, generated from its parsers.

    python -m gerrit_cli.completion > scripts/gerrit-completion.bash

tests/test_completion.py fails when the committed script offers a
command or option the parsers do not have, or misses one they do.
"""

import argparse


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _options(parser: argparse.ArgumentParser) -> list[str]:
    return sorted(
        opt for action in parser._actions for opt in action.option_strings
    )


def completion_table(
    parser: argparse.ArgumentParser,
) -> tuple[list[str], list[str], dict[str, list[str]], dict[str, list[str]]]:
    """Global options, command names, options per command, and the
    subcommands of the commands that have them.

    A subcommand's options are keyed "command:sub" (staged:list).
    """
    commands = _subparsers(parser)
    options: dict[str, list[str]] = {}
    nested: dict[str, list[str]] = {}
    for name, sub in commands.items():
        subs = _subparsers(sub)
        if subs:
            nested[name] = sorted(subs)
            for sub_name, sub_parser in subs.items():
                options[f"{name}:{sub_name}"] = _options(sub_parser)
        else:
            options[name] = _options(sub)
    return _options(parser), sorted(commands), options, nested


def bash_completion(parser: argparse.ArgumentParser) -> str:
    global_opts, commands, options, nested = completion_table(parser)

    # One arm per parser, so an alias shares its command's arm.
    arms: dict[tuple[str, ...], list[str]] = {}
    by_parser: dict[int, list[str]] = {}
    for name, sub in _subparsers(parser).items():
        by_parser.setdefault(id(sub), []).append(name)
    for names in by_parser.values():
        if names[0] in options:
            arms[tuple(sorted(names))] = options[names[0]]
    for key in options:
        if ":" in key:
            arms[(key,)] = options[key]
    arm_lines = [
        f'        {"|".join(keys)}) opts="{" ".join(opts)}" ;;'
        for keys, opts in sorted(arms.items())
    ]
    nested_lines = [
        f'        {name}) subs="{" ".join(subs)}" ;;'
        for name, subs in sorted(nested.items())
    ]

    return "\n".join([
        "# Bash tab completion for gerrit-cli (run as gerrit, gerrit-cli or gc).",
        "#",
        "# Generated from the argparse parsers -- do not edit by hand:",
        "#     python -m gerrit_cli.completion > scripts/gerrit-completion.bash",
        "#",
        "# To enable:  source /path/to/gerrit-completion.bash",
        "",
        "_gerrit_completions() {",
        "    local cur prev words cword",
        "    _init_completion || return",
        "",
        f'    local global_opts="{" ".join(global_opts)}"',
        f'    local commands="{" ".join(commands)}"',
        "",
        "    # The command is the first word that is not a global option.",
        '    local cmd="" i=1',
        "    while (( i < cword )); do",
        '        case "${words[i]}" in',
        "            --user|-U) (( i += 2 )); continue ;;",
        "            -*) (( i++ )); continue ;;",
        "        esac",
        '        cmd="${words[i]}"',
        "        break",
        "    done",
        '    if [[ -z "$cmd" ]]; then',
        '        COMPREPLY=($(compgen -W "$global_opts $commands" -- "$cur"))',
        "        return",
        "    fi",
        "",
        '    local key="$cmd" subs=""',
        '    case "$cmd" in',
        *nested_lines,
        "    esac",
        '    if [[ -n "$subs" ]]; then',
        "        if (( cword == i + 1 )); then",
        '            COMPREPLY=($(compgen -W "$subs" -- "$cur"))',
        "            return",
        "        fi",
        '        key="$cmd:${words[i+1]}"',
        "    fi",
        "",
        '    local opts=""',
        '    case "$key" in',
        *arm_lines,
        "    esac",
        '    if [[ "$cur" == -* ]]; then',
        '        COMPREPLY=($(compgen -W "$opts" -- "$cur"))',
        "    else",
        '        COMPREPLY=($(compgen -f -- "$cur"))',
        "        compopt -o filenames",
        "    fi",
        "}",
        "",
        "complete -F _gerrit_completions gerrit gc gerrit-cli",
        "",
    ])


if __name__ == "__main__":
    from .cli import build_parser

    print(bash_completion(build_parser()), end="")
