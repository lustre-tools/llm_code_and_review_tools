# Bash tab completion for gerrit-cli (run as gerrit, gerrit-cli or gc).
#
# Generated from the argparse parsers -- do not edit by hand:
#     python -m gerrit_cli.completion > scripts/gerrit-completion.bash
#
# To enable:  source /path/to/gerrit-completion.bash

_gerrit_completions() {
    local cur prev words cword
    _init_completion || return

    local global_opts="--envelope --help --user --version -U -h"
    local commands="abandon abort ack add-reviewer batch check checkout co comments continue-reintegration delete-comment delete-message describe diff done examples explain extract find-user finish-patch graph hashtag info label maloo message next-patch publish-drafts push rebase related remove-reviewer reply restore review review-series reviewer-candidates reviewers s search series-comments series-info series-status set-topic skip-reintegration stage staged status upload vote watch work-on-patch"

    # The command is the first word that is not a global option.
    local cmd="" i=1
    while (( i < cword )); do
        case "${words[i]}" in
            --user|-U) (( i += 2 )); continue ;;
            -*) (( i++ )); continue ;;
        esac
        cmd="${words[i]}"
        break
    done
    if [[ -z "$cmd" ]]; then
        COMPREPLY=($(compgen -W "$global_opts $commands" -- "$cur"))
        return
    fi

    local key="$cmd" subs=""
    case "$cmd" in
        staged) subs="clear list refresh remove show" ;;
    esac
    if [[ -n "$subs" ]]; then
        if (( cword == i + 1 )); then
            COMPREPLY=($(compgen -W "$subs" -- "$cur"))
            return
        fi
        key="$cmd:${words[i+1]}"
    fi

    local opts=""
    case "$key" in
        abandon) opts="--dry-run --envelope --help --message --pretty --user -U -h -m -n -p" ;;
        abort) opts="--envelope --help --keep-changes --user -U -h -k" ;;
        ack) opts="--envelope --help --pretty --user -U -h -p" ;;
        add-reviewer) opts="--cc --dry-run --envelope --help --pretty --user -U -h -n -p" ;;
        batch) opts="--dry-run --envelope --help --pretty --user -U -h -n -p" ;;
        check) opts="--envelope --help --offline --user -U -h" ;;
        checkout|co) opts="--branch --envelope --help --patchset --pretty --user -U -b -h -p -r" ;;
        comments|extract) opts="--all --context-lines --envelope --fields --help --include-ci --include-lint --include-system --messages --no-context --pretty --since --summary --timeline --user -U -a -c -h -p -s" ;;
        continue-reintegration) opts="--envelope --help --user -U -h" ;;
        delete-comment) opts="--dry-run --envelope --help --pretty --reason --user -U -h -n -p" ;;
        delete-message) opts="--dry-run --envelope --help --pretty --reason --user -U -h -n -p" ;;
        describe) opts="--command --envelope --help --pretty --user -U -h -p" ;;
        diff) opts="--envelope --help --pretty --unified --user -U -h -p -u" ;;
        done) opts="--envelope --help --pretty --user -U -h -p" ;;
        examples) opts="--envelope --help --user -U -h" ;;
        explain) opts="--envelope --help --user -U -h" ;;
        find-user) opts="--envelope --help --limit --pretty --user -U -h -n -p" ;;
        finish-patch) opts="--envelope --help --stay --user -U -h" ;;
        graph) opts="--branch --comments --conflicts --cross-project --envelope --help --include-hashtag --include-topic --name --no-open --output --pretty --skip-ci-details --skip-hashtag --skip-topic --ticket --user -U -h -o -p" ;;
        hashtag) opts="--add --envelope --help --pretty --remove --user -U -a -h -p -r" ;;
        info) opts="--envelope --help --pretty --show-bots --user -U -h -p" ;;
        label|vote) opts="--envelope --help --message --no-thread --pretty --user -U -h -m -p" ;;
        maloo) opts="--envelope --help --patchset --pretty --user -U -h -p -r" ;;
        message) opts="--envelope --help --no-thread --pretty --unresolved --user -U -h -p" ;;
        next-patch) opts="--envelope --help --user --with-comments -U -h" ;;
        publish-drafts) opts="--dry-run --envelope --expect-patchset --help --pretty --user -U -h -n -p" ;;
        push) opts="--dry-run --envelope --help --user -U -h -n" ;;
        rebase) opts="--envelope --help --pretty --user -U -h -p" ;;
        related) opts="--envelope --help --pretty --user -U -h -p" ;;
        remove-reviewer) opts="--dry-run --envelope --help --pretty --user -U -h -n -p" ;;
        reply) opts="--ack --done --dry-run --envelope --help --pretty --resolve --url --user -U -a -d -h -n -p -r -u" ;;
        restore) opts="--envelope --help --message --pretty --user -U -h -m -p" ;;
        review) opts="--base --changes-only --dry-run --envelope --full-content --full-context --help --message --no-thread --post-comments --prefix --pretty --summary --tag --unified --user --vote -U -b -c -f -h -m -n -p -s -u" ;;
        review-series) opts="--checkout --envelope --help --include-abandoned --no-checkout --no-prompt --numbers-only --pretty --urls-only --user -U -a -c -h -n -p -u" ;;
        reviewer-candidates) opts="--asked --brief --compact --envelope --exclude --help --limit --load --months --path --pretty --ref --repo --table --user --widen-to -C -U -h -n -p" ;;
        reviewers) opts="--envelope --help --pretty --user -U -h -p" ;;
        s|search) opts="--all --envelope --help --limit --max --pretty --start --user -S -U -a -h -n -p" ;;
        series-comments) opts="--all --context-lines --envelope --fields --help --include-ci --include-lint --include-system --no-context --pretty --summary --user -U -a -c -h -p -s" ;;
        series-info) opts="--envelope --help --pretty --show-bots --user -U -h -p" ;;
        series-status) opts="--envelope --help --pretty --user -U -h -p" ;;
        set-topic) opts="--envelope --help --pretty --user -U -h -p" ;;
        skip-reintegration) opts="--envelope --help --user -U -h" ;;
        stage) opts="--ack --done --envelope --help --resolve --url --user -U -a -d -h -r" ;;
        staged:clear) opts="--envelope --help --user -U -h" ;;
        staged:list) opts="--envelope --help --pretty --user -U -h -p" ;;
        staged:refresh) opts="--envelope --help --user -U -h" ;;
        staged:remove) opts="--envelope --help --user -U -h" ;;
        staged:show) opts="--envelope --help --pretty --user -U -h -p" ;;
        status) opts="--envelope --help --user -U -h" ;;
        upload) opts="--base --branch --dry-run --envelope --expect-patchset --help --no-amend --no-author-edit --no-ci-reviewers --private --project --public --repo --series --topic --user -C -U -b -h -n -t" ;;
        watch) opts="--envelope --help --user -U -h" ;;
        work-on-patch) opts="--envelope --help --user -U -h" ;;
    esac
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "$opts" -- "$cur"))
    else
        COMPREPLY=($(compgen -f -- "$cur"))
        compopt -o filenames
    fi
}

complete -F _gerrit_completions gerrit gc gerrit-cli
