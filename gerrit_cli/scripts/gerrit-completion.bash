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
    local commands="abandon abort ack add-reviewer batch checkout co comments continue-reintegration describe diff done examples explain extract find-user finish-patch graph hashtag i info interactive label maloo message next-patch push rebase related remove-reviewer reply restore review review-series reviewers s sashiko-review search series-comments series-info series-status set-topic skip-reintegration sr stage staged status upload vote watch work-on-patch"

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
        abandon) opts="--dry-run --help --message --pretty -h -m -n -p" ;;
        abort) opts="--help --keep-changes -h -k" ;;
        ack) opts="--help --pretty -h -p" ;;
        add-reviewer) opts="--cc --dry-run --help --pretty -h -n -p" ;;
        batch) opts="--dry-run --help --pretty -h -n -p" ;;
        checkout|co) opts="--branch --help --patchset --pretty -b -h -p -r" ;;
        comments|extract) opts="--all --context-lines --fields --help --include-ci --include-system --no-context --pretty --summary -a -c -h -p -s" ;;
        continue-reintegration) opts="--help -h" ;;
        describe) opts="--command --help --pretty -h -p" ;;
        diff) opts="--help --pretty -h -p" ;;
        done) opts="--help --pretty -h -p" ;;
        examples) opts="--help -h" ;;
        explain) opts="--help -h" ;;
        find-user) opts="--help --limit --pretty -h -n -p" ;;
        finish-patch) opts="--help --stay -h" ;;
        graph) opts="--branch --comments --cross-project --help --include-hashtag --include-topic --name --no-open --output --pretty --skip-ci-details --skip-hashtag --skip-topic --ticket -h -o -p" ;;
        hashtag) opts="--add --help --pretty --remove -a -h -p -r" ;;
        i|interactive) opts="--help -h" ;;
        info) opts="--help --pretty --show-bots -h -p" ;;
        label|vote) opts="--help --message --pretty -h -m -p" ;;
        maloo) opts="--help --patchset --pretty -h -p -r" ;;
        message) opts="--help --pretty -h -p" ;;
        next-patch) opts="--help --with-comments -h" ;;
        push) opts="--dry-run --help -h -n" ;;
        rebase) opts="--help --pretty -h -p" ;;
        related) opts="--help --pretty -h -p" ;;
        remove-reviewer) opts="--dry-run --help --pretty -h -n -p" ;;
        reply) opts="--ack --done --dry-run --help --pretty --resolve --url -a -d -h -n -p -r -u" ;;
        restore) opts="--help --message --pretty -h -m -p" ;;
        review) opts="--base --changes-only --dry-run --full-content --full-context --help --message --post-comments --prefix --pretty --summary --tag --unified --vote -b -c -f -h -m -n -p -s -u" ;;
        review-series) opts="--checkout --help --include-abandoned --no-checkout --no-prompt --numbers-only --pretty --urls-only -a -c -h -n -p -u" ;;
        reviewers) opts="--help --pretty -h -p" ;;
        s|search) opts="--all --help --limit --max --pretty --start -S -a -h -n -p" ;;
        sashiko-review|sr) opts="--dry-run --help --repo --sashiko-url --timeout --vote -h" ;;
        series-comments) opts="--all --context-lines --fields --help --include-ci --include-system --no-context --pretty --summary -a -c -h -p -s" ;;
        series-info) opts="--help --pretty --show-bots -h -p" ;;
        series-status) opts="--help --pretty -h -p" ;;
        set-topic) opts="--help --pretty -h -p" ;;
        skip-reintegration) opts="--help -h" ;;
        stage) opts="--ack --done --help --resolve --url -a -d -h -r" ;;
        staged:clear) opts="--help -h" ;;
        staged:list) opts="--help --pretty -h -p" ;;
        staged:refresh) opts="--help -h" ;;
        staged:remove) opts="--help -h" ;;
        staged:show) opts="--help --pretty -h -p" ;;
        status) opts="--help -h" ;;
        upload) opts="--branch --dry-run --expect-patchset --help --no-amend --project --repo --series --topic -C -b -h -n -t" ;;
        watch) opts="--help -h" ;;
        work-on-patch) opts="--help -h" ;;
    esac
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "$opts" -- "$cur"))
    else
        COMPREPLY=($(compgen -f -- "$cur"))
        compopt -o filenames
    fi
}

complete -F _gerrit_completions gerrit gc gerrit-cli
