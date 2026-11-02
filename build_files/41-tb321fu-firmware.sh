#!/bin/bash
# Install the Lenovo TB321FU firmware (ADSP/CDSP, GPU zap, VPU, touch, speaker
# amp, Wi-Fi board data) and the sensor core configuration (usr/share/qcom).
# Proprietary, so it lives in a separate repo; a tarball dropped next to this
# script is used instead for offline builds.
#
# Gated: only runs when the image is built for the TB321FU, via
#   ARMADA_ENABLE_TB321FU=1
# so the device-generic Freedom build keeps its current behavior.
set -euo pipefail

if [[ "${ARMADA_ENABLE_TB321FU:-0}" != "1" ]]; then
    echo "41-tb321fu-firmware: skipped (set ARMADA_ENABLE_TB321FU=1 to install the TB321FU firmware)"
    exit 0
fi

source /ctx/build_files/tb321fu-firmware.env
tarball=/tmp/firmware-lenovo-tb321fu.tar.gz
local_tarball="/ctx/build_files/firmware-lenovo-tb321fu-${TB321FU_FW_VERSION}.tar.gz"

if [[ -f ${local_tarball} ]]; then
    cp "${local_tarball}" "${tarball}"
else
    curl --retry 12 --retry-delay 10 -fsSL -o "${tarball}" "${TB321FU_FW_URL}"
fi
echo "${TB321FU_FW_SHA256}  ${tarball}" | sha256sum -c -
tar -xzf "${tarball}" -C / --no-same-owner usr/lib/firmware usr/share/qcom
rm -f "${tarball}"
