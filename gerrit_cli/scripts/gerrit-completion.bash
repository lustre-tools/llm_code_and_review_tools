#!/usr/bin/env bash
# Bash tab completion for gerrit CLI
#
# To enable:
#   source /path/to/gerrit-completion.bash
#
# Or add to ~/.bashrc:
#   source /path/to/gerrit-completion.bash

_gerrit_completions() {
    local cur prev words cword
    _init_completion || return

    local commands="extract reply batch review review-series series-comments \
        interactive series-status work-on-patch next-patch finish-patch \
        abort-patch rebase-status end-session continue-reintegration \
        skip-reintegration stage push staged-list staged-review staged-remove \
        staged-clear staged-clear-all staged-refresh \
        reviewers add-reviewer remove-reviewer find-user \
        abandon checkout maloo message"

    # Options for each command
    local extract_opts="--all -a --no-context --context-lines -c --json -j"
    local reply_opts="--done -d --ack -a --resolve -r --json -j"
    local batch_opts="--json -j"
    local review_opts="--json -j --changes-only -c --full-content --post-comments"
    local review_series_opts="--json -j --urls-only -u --numbers-only -n --include-abandoned -a --no-prompt --no-checkout"
    local series_comments_opts="--json -j --all -a --no-context --context-lines -c"
    local series_status_opts="--json -j"
    local work_on_patch_opts=""
    local next_patch_opts="--with-comments -c"
    local finish_patch_opts="--stay -s"
    local stage_opts="--done -d --ack -a --resolve -r --url -u"
    local push_opts="--dry-run -n"
    local staged_list_opts="--json -j"
    local staged_review_opts="--json -j"

    # Get the command (second word, after gerrit)
    local cmd=""
    if (( cword >= 2 )); then
        cmd="${words[1]}"
    fi

    # If no command yet, complete commands
    if (( cword == 1 )); then
        COMPREPLY=($(compgen -W "$commands" -- "$cur"))
        return
    fi

    # Complete options based on command
    case "$cmd" in
        extract)
            COMPREPLY=($(compgen -W "$extract_opts" -- "$cur"))
            ;;
        reply)
            COMPREPLY=($(compgen -W "$reply_opts" -- "$cur"))
            ;;
        batch)
            # After URL, complete files for the JSON file argument
            if (( cword == 3 )) || [[ "$prev" == *.json ]]; then
                COMPREPLY=($(compgen -f -X '!*.json' -- "$cur"))
                compopt -o filenames
            else
                COMPREPLY=($(compgen -W "$batch_opts" -- "$cur"))
            fi
            ;;
        review)
            if [[ "$prev" == "--post-comments" ]]; then
                COMPREPLY=($(compgen -f -X '!*.json' -- "$cur"))
                compopt -o filenames
            else
                COMPREPLY=($(compgen -W "$review_opts" -- "$cur"))
            fi
            ;;
        review-series)
            COMPREPLY=($(compgen -W "$review_series_opts" -- "$cur"))
            ;;
        series-comments)
            COMPREPLY=($(compgen -W "$series_comments_opts" -- "$cur"))
            ;;
        series-status)
            COMPREPLY=($(compgen -W "$series_status_opts" -- "$cur"))
            ;;
        work-on-patch)
            COMPREPLY=($(compgen -W "$work_on_patch_opts" -- "$cur"))
            ;;
        next-patch)
            COMPREPLY=($(compgen -W "$next_patch_opts" -- "$cur"))
            ;;
        finish-patch)
            COMPREPLY=($(compgen -W "$finish_patch_opts" -- "$cur"))
            ;;
        stage)
            COMPREPLY=($(compgen -W "$stage_opts" -- "$cur"))
            ;;
        push)
            COMPREPLY=($(compgen -W "$push_opts" -- "$cur"))
            ;;
        staged-list)
            COMPREPLY=($(compgen -W "$staged_list_opts" -- "$cur"))
            ;;
        staged-review)
            COMPREPLY=($(compgen -W "$staged_review_opts" -- "$cur"))
            ;;
        staged-remove|staged-clear|staged-refresh)
            # These take change numbers as arguments, no specific completion
            ;;
        abort-patch|rebase-status|end-session|continue-reintegration|skip-reintegration|staged-clear-all)
            # No arguments
            ;;
        *)
            # Unknown command, try generic file completion
            COMPREPLY=($(compgen -f -- "$cur"))
            compopt -o filenames
            ;;
    esac
}

complete -F _gerrit_completions gerrit
complete -F _gerrit_completions gerrit-cli
complete -F _gerrit_completions gc
