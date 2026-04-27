#!/bin/sh
# cryptsetup-beep runtime player.
# Sourced config drives behaviour. Triggered by cryptsetup-beep.path when
# systemd-cryptsetup creates an ask-password file in /run/systemd/ask-password.
# After playing, the script waits for the ask-password directory to drain so
# the .path unit doesn't re-trigger us in a tight loop while the prompt is
# still on screen.

set -u

CONFIG=/etc/cryptsetup-beep/config
ASK_DIR=/run/systemd/ask-password

# shellcheck disable=SC1090
[ -r "$CONFIG" ] && . "$CONFIG"

case "${METHOD:-pcspkr}" in
    pcspkr)
        modprobe pcspkr 2>/dev/null
        beep -f "${PCSPKR_FREQ:-440}" -l "${PCSPKR_LEN:-500}" 2>/dev/null
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
        amixer_sset() {
            # $1 card, $2 control, $3 "value" or "value,state"
            case "$3" in
                *,*) amixer -c "$1" -q sset "$2" "${3%,*}" "${3#*,}" 2>/dev/null ;;
                *)   amixer -c "$1" -q sset "$2" "$3" 2>/dev/null ;;
            esac
        }
        [ -n "${MIXER_MASTER:-}" ]    && amixer_sset "$card" Master    "$MIXER_MASTER"
        [ -n "${MIXER_SPEAKER:-}" ]   && amixer_sset "$card" Speaker   "$MIXER_SPEAKER"
        [ -n "${MIXER_HEADPHONE:-}" ] && amixer_sset "$card" Headphone "$MIXER_HEADPHONE"
        [ -n "${MIXER_PCM:-}" ]       && amixer_sset "$card" PCM       "$MIXER_PCM"

        aplay -q -D "plughw:$card,${PCM_DEVICE:-0}" "${BEEP_WAV:-/usr/share/cryptsetup-beep/beep.wav}"
        ;;
    *)
        exit 1
        ;;
esac

while ls "$ASK_DIR"/ask.* >/dev/null 2>&1; do
    sleep 0.2
done
