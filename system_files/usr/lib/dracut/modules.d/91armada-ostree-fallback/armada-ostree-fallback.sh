#!/bin/bash
# Point ostree-prepare-root at a deployment that exists when /KERNEL's baked
# ostree= karg names one that does not: ostree deletes the old boot.N tree on
# each deployment write, so a /KERNEL that missed its regen names a dead path.
# Same bootcsum means the same kernel, so it is a deployment this boot may
# enter. Only the karg is corrected; the deployment tree is ostree's.

set -u

MARKER=/run/armada/ostree-fallback-remapped

log() { echo "armada-ostree-fallback: $*" > /dev/kmsg 2>/dev/null || echo "armada-ostree-fallback: $*"; }

# ExecStartPost: /proc/cmdline is conventionally what the bootloader passed, and
# the stale value is the evidence that this device was rescued.
if [ "${1:-}" = "--restore" ]; then
    [ -e "${MARKER}" ] || exit 0
    umount /proc/cmdline 2>/dev/null || log "could not restore the real /proc/cmdline"
    exit 0
fi

read -r cmdline < /proc/cmdline

set -f
karg=""
for t in ${cmdline}; do
    case "${t}" in ostree=*) karg="${t#ostree=}" ;; esac
done
set +f

[ -n "${karg}" ] || exit 0
[ -e "/sysroot${karg}" ] && exit 0
[ -d /sysroot/ostree ] || exit 0

case "${karg}" in /ostree/boot.*) ;; *) exit 0 ;; esac
rel="${karg#/ostree/}"
bootdir="${rel%%/*}"; rel="${rel#*/}"
osname="${rel%%/*}"; rel="${rel#*/}"
csum="${rel%%/*}"
idx="${rel#*/}"
case "${idx}" in */*) idx="" ;; esac
if [ -z "${bootdir}" ] || [ -z "${osname}" ] || [ -z "${csum}" ] || \
   [ -z "${idx}" ] || [ "${csum}" = "${idx}" ]; then
    log "unparseable ostree= karg: ${karg}"
    exit 0
fi

log "baked ostree path ${karg} is missing; looking for bootcsum ${csum}"

# Same index under the surviving bootversion first, then any index for this
# bootcsum: the kernel and initramfs are identical either way.
found=""
for b in boot.0 boot.1; do
    if [ -e "/sysroot/ostree/${b}/${osname}/${csum}/${idx}" ]; then
        found="/ostree/${b}/${osname}/${csum}/${idx}"
        break
    fi
done
if [ -z "${found}" ]; then
    for e in /sysroot/ostree/boot.0/"${osname}/${csum}"/* \
             /sysroot/ostree/boot.1/"${osname}/${csum}"/*; do
        [ -e "${e}" ] || continue
        found="${e#/sysroot}"
        break
    done
fi
if [ -z "${found}" ]; then
    log "no surviving bootlink for bootcsum ${csum}; cannot remap"
    exit 0
fi

set -f
fixed=""
for t in ${cmdline}; do
    case "${t}" in
        ostree=*) fixed="${fixed} ostree=${found}" ;;
        *) fixed="${fixed} ${t}" ;;
    esac
done
set +f
fixed="${fixed# }"

# Only for ostree-prepare-root's read; ExecStartPost puts the real one back.
mkdir -p /run/armada 2>/dev/null || true
if ! printf '%s\n' "${fixed}" > /run/armada/cmdline 2>/dev/null; then
    log "could not stage a corrected cmdline"
    exit 0
fi
# Marker before the mount: --restore keys off it, so a replacement must never
# outlive a marker that was never written.
if ! printf '%s\n' "${found}" > "${MARKER}" 2>/dev/null; then
    log "could not write the restore marker; leaving the baked karg alone"
    rm -f /run/armada/cmdline 2>/dev/null || true
    exit 0
fi
if mount --bind /run/armada/cmdline /proc/cmdline 2>/dev/null; then
    # /proc/cmdline is normally immutable; a writable bind would not be.
    mount -o remount,bind,ro /proc/cmdline 2>/dev/null ||
        log "corrected /proc/cmdline is writable: read-only remount failed"
    log "remapped ostree= to ${found}; /KERNEL is stale and is regenerated after boot"
else
    log "could not overmount /proc/cmdline"
    rm -f "${MARKER}" 2>/dev/null || log "stale restore marker remains at ${MARKER}"
fi
# The mount holds the inode, so dropping the pathname leaves no writable alias:
# a read-only bind target does not make the source read-only.
rm -f /run/armada/cmdline 2>/dev/null || log "writable alias remains at /run/armada/cmdline"
exit 0
