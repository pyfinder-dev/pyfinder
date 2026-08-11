# Completion only lists the public command vocabulary; it never runs the launcher.
_pyfinder_completion() {
    local current_word previous_word workflow
    current_word="${COMP_WORDS[COMP_CWORD]}"
    previous_word=""
    if [[ "$COMP_CWORD" -gt 0 ]]; then
        previous_word="${COMP_WORDS[COMP_CWORD-1]}"
    fi

    if [[ "$COMP_CWORD" -eq 1 ]]; then
        COMPREPLY=( $(compgen -W "continuous playback on-demand status logs stop help --help" -- "$current_word") )
        return
    fi

    workflow="${COMP_WORDS[1]}"
    case "$workflow" in
        playback)
            COMPREPLY=( $(compgen -W "--event-id --list" -- "$current_word") )
            ;;
        on-demand)
            if [[ "$previous_word" == "--verbosity" ]]; then
                COMPREPLY=( $(compgen -W "DEBUG INFO WARNING ERROR CRITICAL" -- "$current_word") )
            else
                COMPREPLY=( $(compgen -W "--event-id --test --verbosity" -- "$current_word") )
            fi
            ;;
        *)
            COMPREPLY=()
            ;;
    esac
}

complete -F _pyfinder_completion pyfinder
