#!/bin/sh
# cryptsetup-beep runtime player.
# Sourced config drives behaviour. Triggered by cryptsetup-beep.path when
# systemd-cryptsetup creates an ask-password file in /run/systemd/ask-password.
# We pick a beep tone that matches the prompt: the initial passphrase prompt,
# a passphrase retry, an initial token PIN prompt, or a token PIN retry.
# After playing, the script waits for the ask-password directory to drain so
# the .path unit doesn't re-trigger us in a tight loop while the prompt is
# still on screen.

set -u

CONFIG=/etc/cryptsetup-beep/config
ASK_DIR=/run/systemd/ask-password
STATE_DIR=/run/cryptsetup-beep
PIN_SEEN_FILE="$STATE_DIR/pin-seen"
PASSPHRASE_SEEN_FILE="$STATE_DIR/passphrase-seen"

# shellcheck disable=SC1090
[ -r "$CONFIG" ] && . "$CONFIG"

# ---------------------------------------------------------------------------
# Prompt classification — pick a tone based on the message systemd-cryptsetup
# writes into the most recent /run/systemd/ask-password/ask.* file.
# ---------------------------------------------------------------------------

newest_ask_file() {
    ask=
    for f in "$ASK_DIR"/ask.*; do
        [ -e "$f" ] || continue
        if [ -z "$ask" ] || [ "$f" -nt "$ask" ]; then
            ask=$f
        fi
    done
    printf '%s\n' "$ask"
}

# Returns one of: ready | retry | pin | pin-retry
classify_prompt() {
    ask=$(newest_ask_file)
    if [ -z "$ask" ]; then
        printf 'ready\n'
        return
    fi
    message=$(grep -E '^Message=' "$ask" 2>/dev/null | head -1)
    message=${message#Message=}

    case "$message" in
        # Explicit "try again" wording — kept as a defensive fallback for
        # any systemd configuration that emits it. systemd-cryptsetup in
        # the standard sd-encrypt path doesn't, hence the per-boot state
        # marker below.
        *"Incorrect passphrase, try again"*|*"try again"*)
            printf 'retry\n'
            ;;
        *"security token PIN"*|*"LUKS2 token PIN"*|*"TPM2 PIN"*|*"FIDO2 PIN"*)
            mkdir -p "$STATE_DIR" 2>/dev/null
            if [ -e "$PIN_SEEN_FILE" ]; then
                printf 'pin-retry\n'
            else
                : > "$PIN_SEEN_FILE"
                printf 'pin\n'
            fi
            ;;
        # Passphrase / recovery key prompts. systemd-cryptsetup re-emits the
        # same Message= on every retry, so we use a per-boot state marker
        # in tmpfs to know whether this is the first ask of the boot.
        *passphrase*|*"recovery key"*)
            mkdir -p "$STATE_DIR" 2>/dev/null
            if [ -e "$PASSPHRASE_SEEN_FILE" ]; then
                printf 'retry\n'
            else
                : > "$PASSPHRASE_SEEN_FILE"
                printf 'ready\n'
            fi
            ;;
        *)
            printf 'ready\n'
            ;;
    esac
}

# ---------------------------------------------------------------------------
# Tone playback
# ---------------------------------------------------------------------------

play_alsa() {
    # $1 = card index, $2 = pcm device, $3 = wav path
    aplay -q -D "plughw:$1,$2" "$3"
}

amixer_sset() {
    # $1 card, $2 control, $3 "value" or "value,state"
    case "$3" in
        *,*) amixer -c "$1" -q sset "$2" "${3%,*}" "${3#*,}" 2>/dev/null ;;
        *)   amixer -c "$1" -q sset "$2" "$3" 2>/dev/null ;;
    esac
}

case "${METHOD:-pcspkr}" in
    pcspkr)
        modprobe pcspkr 2>/dev/null
        prompt_kind=$(classify_prompt)
        # Frequency: ready/retry use the base tone, pin/pin-retry an octave up.
        case "$prompt_kind" in
            pin|pin-retry) freq=${PCSPKR_FREQ_PIN:-880} ;;
            *)             freq=${PCSPKR_FREQ:-440} ;;
        esac
        len=${PCSPKR_LEN:-500}
        # Repeat: retry variants double the beep.
        case "$prompt_kind" in
            retry|pin-retry) beeps=2 ;;
            *)               beeps=1 ;;
        esac
        i=0
        while [ "$i" -lt "$beeps" ]; do
            beep -f "$freq" -l "$len" 2>/dev/null
            i=$((i + 1))
            [ "$i" -lt "$beeps" ] && sleep 0.1
        done
        ;;

    alsa)
        for m in ${CODEC_MODULES:-}; do
            modprobe "$m" 2>/dev/null
        done

        card=
        i=0
        while [ "$i" -lt 20 ]; do
            for dir in /proc/asound/card[0-9]*; do
                [ -d "$dir" ] || continue
                if [ -n "${CODEC_MATCH:-}" ]; then
                    grep -qi "$CODEC_MATCH" "$dir"/codec* 2>/dev/null || continue
                fi
                c=${dir#/proc/asound/card}
                if [ -e "/dev/snd/pcmC${c}D${PCM_DEVICE:-0}p" ]; then
                    card=$c
                    break 2
                fi
            done
            sleep 0.1
            i=$((i + 1))
        done

        if [ -z "$card" ]; then
            exit 1
        fi

        # Cold-boot codec defaults are often muted; set them explicitly.
        # Mixer values are formatted as "<percent>" or "<percent>,<state>"
        # — we split on the comma using POSIX parameter expansion rather than
        # tr(1), which isn't reliably present in the initramfs.
        [ -n "${MIXER_MASTER:-}" ]    && amixer_sset "$card" Master    "$MIXER_MASTER"
        [ -n "${MIXER_SPEAKER:-}" ]   && amixer_sset "$card" Speaker   "$MIXER_SPEAKER"
        [ -n "${MIXER_HEADPHONE:-}" ] && amixer_sset "$card" Headphone "$MIXER_HEADPHONE"
        [ -n "${MIXER_PCM:-}" ]       && amixer_sset "$card" PCM       "$MIXER_PCM"

        prompt_kind=$(classify_prompt)
        default_wav=${BEEP_WAV:-/usr/share/cryptsetup-beep/beep.wav}
        case "$prompt_kind" in
            retry)     wav=${BEEP_WAV_RETRY:-/usr/share/cryptsetup-beep/beep-retry.wav} ;;
            pin)       wav=${BEEP_WAV_PIN:-/usr/share/cryptsetup-beep/beep-pin.wav} ;;
            pin-retry) wav=${BEEP_WAV_PIN_RETRY:-/usr/share/cryptsetup-beep/beep-pin-retry.wav} ;;
            *)         wav=${BEEP_WAV_READY:-$default_wav} ;;
        esac
        # Fall back to the default WAV if the chosen one is missing.
        [ -r "$wav" ] || wav=$default_wav

        play_alsa "$card" "${PCM_DEVICE:-0}" "$wav"
        ;;

    *)
        exit 1
        ;;
esac

while ls "$ASK_DIR"/ask.* >/dev/null 2>&1; do
    sleep 0.2
done
