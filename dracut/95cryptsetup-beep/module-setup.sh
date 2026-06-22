#!/bin/bash
# Dracut install hook for cryptsetup-beep.
# Mirrors initcpio/install-hook but uses dracut's API: bakes the same
# WAVs, play script, config, systemd units, kernel modules, and userspace
# binaries into the initramfs so the beep plays the moment
# systemd-cryptsetup posts an ask-password prompt.

check() {
    # 0 = ship it, 255 = optional/skip, 1 = not installable
    [[ -f /etc/cryptsetup-beep/config ]] || return 255
    return 0
}

depends() {
    # systemd-cryptsetup is what creates /run/systemd/ask-password/ask.*
    # that our .path unit watches; crypt pulls cryptsetup binaries in.
    echo "systemd crypt"
    return 0
}

install() {
    local cfg=/etc/cryptsetup-beep/config

    if [[ ! -r "$cfg" ]]; then
        dwarning "cryptsetup-beep: no /etc/cryptsetup-beep/config — run 'cryptsetup-beep --init'"
        return 0
    fi

    # shellcheck disable=SC1090
    . "$cfg"

    inst_simple "$cfg" /etc/cryptsetup-beep/config
    inst_simple /usr/share/cryptsetup-beep/beep.wav
    inst_simple /usr/share/cryptsetup-beep/beep-retry.wav
    inst_simple /usr/share/cryptsetup-beep/beep-pin.wav
    inst_simple /usr/share/cryptsetup-beep/beep-pin-retry.wav
    inst_simple /usr/lib/cryptsetup-beep/play-beep.sh
    inst_simple /usr/lib/systemd/system/cryptsetup-beep.path
    inst_simple /usr/lib/systemd/system/cryptsetup-beep.service

    # play-beep.sh needs these — dracut's base module doesn't include them
    # (mkinitcpio's sd-encrypt pulls busybox which has all of them).
    inst_binary /usr/bin/grep
    inst_binary /usr/bin/head
    inst_binary /usr/bin/sed
    inst_binary /usr/bin/date
    inst_binary /usr/bin/find

    # Enable the .path unit inside the initramfs only (never on the host).
    mkdir -p "${initdir}/etc/systemd/system/sysinit.target.wants"
    ln -sf /usr/lib/systemd/system/cryptsetup-beep.path \
        "${initdir}/etc/systemd/system/sysinit.target.wants/cryptsetup-beep.path"

    case "${METHOD:-}" in
        pcspkr)
            instmods pcspkr
            inst_binary /usr/bin/beep
            ;;
        alsa)
            local m
            for m in ${CODEC_MODULES:-}; do
                instmods "$m"
            done
            inst_binary /usr/bin/aplay
            inst_binary /usr/bin/amixer
            # ALSA runtime data (codec quirks, conf snippets). dracut has no
            # add_full_dir equivalent — walk it ourselves.
            local f
            while IFS= read -r f; do
                inst_simple "$f"
            done < <(find /usr/share/alsa -type f 2>/dev/null)
            ;;
        *)
            dwarning "cryptsetup-beep: METHOD '${METHOD:-}' is not 'alsa' or 'pcspkr'; nothing baked"
            return 0
            ;;
    esac
}
