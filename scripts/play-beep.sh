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

    # --- TEMPORARY DEBUG (remove after retry-beep is diagnosed) ---
    # Captures every ask we see across the boot so we can tell whether the
    # retry path is being taken or the state file isn't surviving.
    mkdir -p /run/cryptsetup-beep 2>/dev/null
    {
        printf '[%s] ask=%s\n' "$(date '+%H:%M:%S.%N')" "$ask"
        printf '  message=%q\n' "$message"
        printf '  passphrase_seen=%s pin_seen=%s\n' \
            "$( [ -e "$PASSPHRASE_SEEN_FILE" ] && echo yes || echo no )" \
            "$( [ -e "$PIN_SEEN_FILE" ] && echo yes || echo no )"
    } >> /run/cryptsetup-beep/debug.log 2>/dev/null
    # --- END TEMPORARY DEBUG ---

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

# Apply a DEVICE_N_MIXER value of the form "CONTROL VALUE[,STATE]" against
# the given card index. Silently no-ops if the string is empty.
apply_device_mixer() {
    # $1 card index, $2 mixer spec
    _card=$1
    _spec=$2
    [ -z "$_spec" ] && return 0
    _control=${_spec%% *}
    _value=${_spec#* }
    [ "$_control" = "$_spec" ] && return 0
    amixer_sset "$_card" "$_control" "$_value"
}

# Apply a DEVICE_N_VOLUME value (0-100) to the card's output chain.
#
# Setting the volume on EVERY common control is wrong — HDA (and most USB
# audio devices) wire Master → PCM → Headphone/Speaker as a SERIAL chain,
# so 70% × 70% × 70% stacks down to ~34% effective, not 70%. v0.4.0 did
# this and the result was "painfully quiet" beeps that didn't match the
# wizard's reported volume.
#
# Instead:
#   1. UNMUTE every common output control (so no muted link kills audio)
#   2. SET VOLUME on only the first-available top-level control, in
#      priority order: Master (card-wide), PCM (common on USB), then
#      output-specific. Leaves the others at wherever alsa-restore / the
#      user had them, so our 70% means 70% and not 70% × their state.
#
# Called BEFORE apply_device_mixer so a user-specified MIXER string can
# still override the broad-brush volume with a specific control + value.
apply_device_volume() {
    # $1 card index, $2 volume 0-100 (empty / <0 = skip)
    _card=$1
    _vol=$2
    [ -z "$_vol" ] && return 0
    # POSIX integer comparison; -1 (unset sentinel) and anything non-numeric fail.
    [ "$_vol" -ge 0 ] 2>/dev/null || return 0
    # Unmute all plausible path controls (silent no-op where missing).
    for _ctrl in Master PCM Headphone Speaker; do
        amixer -c "$_card" -q sset "$_ctrl" unmute >/dev/null 2>&1
    done
    # Volume on the first control that actually exists.
    for _ctrl in Master PCM Headphone Speaker; do
        if amixer -c "$_card" sset "$_ctrl" "${_vol}%" >/dev/null 2>&1; then
            return 0
        fi
    done
}

# Resolve a MATCH string ("codec:X" or "usb:VVVV:PPPP") to a card index.
# Prints the index on stdout and returns 0, or returns 1 if no card matches.
resolve_match_to_card() {
    _match=$1
    _kind=${_match%%:*}
    _spec=${_match#*:}
    case "$_kind" in
        codec)
            for _dir in /proc/asound/card[0-9]*; do
                [ -d "$_dir" ] || continue
                grep -qi "$_spec" "$_dir"/codec* 2>/dev/null || continue
                _c=${_dir#/proc/asound/card}
                printf '%s\n' "$_c"
                return 0
            done
            ;;
        usb)
            # /proc/asound/cardN/usbid contains "vendor:product" (lowercase hex).
            _want=$(printf '%s' "$_spec" | tr 'A-Z' 'a-z')
            for _dir in /proc/asound/card[0-9]*; do
                [ -f "$_dir/usbid" ] || continue
                _have=$(tr 'A-Z' 'a-z' < "$_dir/usbid" | tr -d '[:space:]')
                [ "$_have" = "$_want" ] || continue
                _c=${_dir#/proc/asound/card}
                printf '%s\n' "$_c"
                return 0
            done
            ;;
    esac
    return 1
}

# Attempt to play $2 (wav) through DEVICE_$1_* once. Returns 0 on success.
try_device() {
    _n=$1
    _wav=$2
    eval "_name=\${DEVICE_${_n}_NAME:-}"
    eval "_match=\${DEVICE_${_n}_MATCH:-}"
    eval "_pcm=\${DEVICE_${_n}_PCM:-0}"
    eval "_mixer=\${DEVICE_${_n}_MIXER:-}"
    eval "_vol=\${DEVICE_${_n}_VOLUME:-}"
    [ -z "$_name" ] && return 2  # end of list
    [ -z "$_match" ] && return 1
    _card=$(resolve_match_to_card "$_match") || return 1
    [ -e "/dev/snd/pcmC${_card}D${_pcm}p" ] || return 1
    # Volume first (broad-brush), then MIXER (user-specified fine-grained
    # override). Either or both may be unset.
    apply_device_volume "$_card" "$_vol"
    apply_device_mixer "$_card" "$_mixer"
    play_alsa "$_card" "$_pcm" "$_wav" || return 1
    return 0
}

# Walk the DEVICE_1..N list and play through the first one that works.
# Retries the whole walk PROBE_TIMEOUT times with 1-second gaps to let USB
# or Thunderbolt-attached audio enumerate before we give up silently.
play_multi_device() {
    _wav=$1
    _retries=${PROBE_TIMEOUT:-5}
    _attempt=0
    while [ "$_attempt" -lt "$_retries" ]; do
        _n=1
        while [ "$_n" -lt 100 ]; do
            eval "_probe=\${DEVICE_${_n}_NAME:-}"
            [ -z "$_probe" ] && break
            try_device "$_n" "$_wav"
            _rc=$?
            if [ "$_rc" -eq 0 ]; then
                return 0
            fi
            _n=$((_n + 1))
        done
        _attempt=$((_attempt + 1))
        [ "$_attempt" -lt "$_retries" ] && sleep 1
    done
    return 1
}

case "${METHOD:-pcspkr}" in
    pcspkr)
        modprobe pcspkr 2>/dev/null
        prompt_kind=$(classify_prompt)
        printf '  kind=%s\n' "$prompt_kind" >> /run/cryptsetup-beep/debug.log 2>/dev/null
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

        prompt_kind=$(classify_prompt)
        printf '  kind=%s\n' "$prompt_kind" >> /run/cryptsetup-beep/debug.log 2>/dev/null
        default_wav=${BEEP_WAV:-/usr/share/cryptsetup-beep/beep.wav}
        case "$prompt_kind" in
            retry)     wav=${BEEP_WAV_RETRY:-/usr/share/cryptsetup-beep/beep-retry.wav} ;;
            pin)       wav=${BEEP_WAV_PIN:-/usr/share/cryptsetup-beep/beep-pin.wav} ;;
            pin-retry) wav=${BEEP_WAV_PIN_RETRY:-/usr/share/cryptsetup-beep/beep-pin-retry.wav} ;;
            *)         wav=${BEEP_WAV_READY:-$default_wav} ;;
        esac
        # Fall back to the default WAV if the chosen one is missing.
        [ -r "$wav" ] || wav=$default_wav

        # Multi-device mode is opt-in: DEVICE_1_NAME present → iterate the
        # ordered list, first available device wins. Otherwise fall through
        # to the legacy single-device path below.
        if [ -n "${DEVICE_1_NAME:-}" ]; then
            play_multi_device "$wav"
        else
            # Single-device path (unchanged behaviour pre-0.4.0, with the
            # probe-loop timeout now honouring PROBE_TIMEOUT instead of the
            # hardcoded 2 seconds).
            card=
            # Convert PROBE_TIMEOUT (seconds) to 0.1s-step iterations.
            iters=$(( ${PROBE_TIMEOUT:-5} * 10 ))
            i=0
            while [ "$i" -lt "$iters" ]; do
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

            play_alsa "$card" "${PCM_DEVICE:-0}" "$wav"
        fi
        ;;

    *)
        exit 1
        ;;
esac

while ls "$ASK_DIR"/ask.* >/dev/null 2>&1; do
    sleep 0.2
done
