#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline Gen4 TB322FC Linux first-boot packaging driver (Linux, stdlib only).

Drives the external MBKP packer, the supplied UnpackTool, the supplied
v4 boot repacker (repack_boot_v4.py), gzip and the official avbtool over one
fixed multi-boot layout and asserts byte-level round-trip correctness.  Two
layouts are supported: the 5-entry maintenance layout (stock Android,
maintenance Linux + DTB, initramfs Blob, attached Shim) and the 7-entry
installed layout (stock Android, internal Linux owner, a diagnostic alias that
shares the owner's padded Linux payload, internal DTB, diagnostic DTB, install
initramfs Blob, attached Shim).
Offline verification only: no device access, no flashing, no reboot, no
partition or UFS access, no network and no dependency installation.

All tool paths and the bootargs string are supplied explicitly and resolved to
absolute paths before any cwd change.

Manifest ABI source of truth (external MultiBootKernelPatcher inc/manifest.h):
ManifestHeader 60B, ManifestEntry 56B, entry name padded to 4B. The five u64
values after the manifest header are a BACKUP of the original stock base-image
header (slots @0x00, @0x10, @0x20, @0x28, @0x30), NOT packed sizes. The patched
raw ARM64 header stores the packed runtime size @0x10, the manifest offset
@0x20, the manifest size @0x28 and the attached-shim offset @0x30.
"""

import argparse
import configparser
import gzip
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True

BOOT_SIZE = 96 * 1024 * 1024
RUNTIME_SIZE = 4 * 1024 * 1024
MAX_RAW = 0x08000000  # conservative 128 MiB raw fallback for unrecognized stock
ARM64_HDR_SIZE = 0x40
ARM64_MAGIC = b"ARM\x64"
QC_ABOOT_MAGIC = b"UNCOMPRESSED_IMG"
GZIP_MAGIC = b"\x1f\x8b"
BOOT_MAGIC = b"ANDROID!"
MANIFEST_MAGIC = 0x464D504B
MANIFEST_HEADER_SIZE = 60
MANIFEST_ENTRY_SIZE = 56
MANIFEST_ENTRY_ALIGN = 4
FDT_MAGIC = 0xD00DFEED
AVB_MAGIC = b"AVBf"

MF_TYPE_LINUX = 1
MF_TYPE_SHIM = 3
MF_TYPE_BLOB = 4
MF_TYPE_DTB = 5
FLAG_BASE = 1
FLAG_EXEC = 4
FLAG_HAS_DTB = 8
FLAG_QC = 16

# BL signed-imm26 forward-branch bound: a SEPARATE strict 128 MiB constraint,
# literally 0x08000000 and never an alias of MAX_RAW or a selected raw budget.
BL_RANGE = 0x08000000

# Verified local .259 profile selector.  Only full-hash equality selects the
# derived budget; there is no CLI override and no generalized registry.
STOCK_BOOT_SHA256 = (
    "b428a31dadc5db3f91620c1f4114f5ee7702e34e0789ed9bf84a878a49898f20")
STOCK_PROFILE_KNOWN = "tb322fc-stock259-verified"
STOCK_PROFILE_DEFAULT = "conservative-default"
# Normal loader raw/image_size room for this .259 ABL/XBL and the three stock
# Android ramdisk headers:
#   268435456 - 524288 - align_up(11090911, 4096) - 8192 - 2097152 = 254713856
KNOWN_RAW_BUDGET = 254713856
# Conservative fallback retained for every unrecognized stock boot.
DEFAULT_RAW_BUDGET = 134217728

# Explicit manifest default entry: CLI key -> (config label, manifest index).
DEFAULT_ENTRY_CHOICES = {
    "android": ("Image-00", 0),
    "maintenance": ("Image-01", 1),
}
DEFAULT_ENTRY = "android"
# Installed-layout default entry: the owner (internal) entry is the default.
INSTALLED_DEFAULT_ENTRY_CHOICES = {
    "android": ("Image-00", 0),
    "internal": ("Image-01", 1),
}
TIMEOUT_MS = 8000

BASE_NAME = "原厂 Android"
LINUX_NAME = "Linux 诊断模式"
DTB_NAME = "Linux Diagnostic DTB"
BLOB_NAME = "Maintenance initramfs"
SHIM_NAME = "Attached Shim"

# Installed-layout entry names (approved STEP 2 contract).
INTERNAL_NAME = "机身 Linux"
INTERNAL_DTB_NAME = "TB322FC Internal DTB"
INSTALLED_BLOB_NAME = "Installed initramfs"

# Fixed manifest entry order (config section order; the shim is auto-added last).
# index, name, type, flags, alignment exponent, device-tree reference
EXPECTED_ENTRIES = [
    (0, BASE_NAME, MF_TYPE_LINUX, FLAG_BASE | FLAG_EXEC, 21, 0),
    (1, LINUX_NAME, MF_TYPE_LINUX, FLAG_EXEC | FLAG_HAS_DTB, 21, 2),
    (2, DTB_NAME, MF_TYPE_DTB, 0, 3, 0),
    (3, BLOB_NAME, MF_TYPE_BLOB, 0, 16, 0),
    (4, SHIM_NAME, MF_TYPE_SHIM, FLAG_EXEC, 2, 0),
]

# Fixed installed-layout manifest entry order: the internal owner (Image-01)
# and its diagnostic alias (Image-02) share the padded Linux payload but point
# at distinct device trees (Image-03 internal, Image-04 diagnostic).
# index, name, type, flags, alignment exponent, device-tree reference
INSTALLED_EXPECTED_ENTRIES = [
    (0, BASE_NAME, MF_TYPE_LINUX, FLAG_BASE | FLAG_EXEC, 21, 0),
    (1, INTERNAL_NAME, MF_TYPE_LINUX, FLAG_EXEC | FLAG_HAS_DTB, 21, 3),
    (2, LINUX_NAME, MF_TYPE_LINUX, FLAG_EXEC | FLAG_HAS_DTB, 21, 4),
    (3, INTERNAL_DTB_NAME, MF_TYPE_DTB, 0, 3, 0),
    (4, DTB_NAME, MF_TYPE_DTB, 0, 3, 0),
    (5, INSTALLED_BLOB_NAME, MF_TYPE_BLOB, 0, 16, 0),
    (6, SHIM_NAME, MF_TYPE_SHIM, FLAG_EXEC, 2, 0),
]

# The only /chosen properties this driver may add, change or remove. Any other
# structural difference between the source DTB and the derived DTB is rejected.
ALLOWED_CHOSEN_PROPS = frozenset((
    "bootargs",
    "armada,boot-stage",
    "kshim,initrd-entry",
    "linux,initrd-start",
    "linux,initrd-end",
))

# Known Linux board identifications: device_info.c requires these exact
# pairings. The TB321FU (SM8650 Kirby) pairing matches the vendored
# sm8650-lenovo-tb321fu.dts from armada-tb321fu.
REQUIRED_COMPATIBLE = ("lenovo,elden", "qcom,sm8750")
REQUIRED_COMPATIBLE_TB321FU = ("lenovo,tb321fu", "qcom,sm8650")
# Gen5 fingerprints that must never appear in the Gen4 diagnostic DTB.
FORBIDDEN_COMPATIBLE = frozenset((
    "lenovo,baldur",
    "lenovo,tb323fu",
    "qcom,sm8850",
    "qcom,canoe",
    "qcom,canoep",
    "qcom,kaanapali",
))

STAGE = "init"
CHECKS = []
COMMANDS = []

OUT = None
OUT_OWNED = False
WORK = None
LOGS = None
TMP = None
ENV = None

TOOL = {}


class StageError(Exception):
    def __init__(self, stage, message):
        super().__init__(message)
        self.stage = stage
        self.message = message


def set_stage(stage):
    global STAGE
    STAGE = stage


def check(name, condition, detail=""):
    CHECKS.append({"check": name, "ok": bool(condition), "detail": detail})
    if not condition:
        raise StageError(STAGE, name + ((": " + detail) if detail else ""))


def select_raw_budget(stock_boot_sha256):
    """Return the raw-payload byte budget for a stock boot SHA256.

    Exact full-hash equality with the verified local .259 image selects the
    derived ABL/XBL/stock-ramdisk profile; every other hash (including a
    one-character mismatch, a label or a prefix) keeps the conservative
    128 MiB policy.  Returns a fresh JSON-serializable dict; no CLI override
    and no generalized registry.
    """
    if stock_boot_sha256 == STOCK_BOOT_SHA256:
        return {
            "profile": STOCK_PROFILE_KNOWN,
            "max_raw_bytes": KNOWN_RAW_BUDGET,
            "stock_boot_sha256": stock_boot_sha256,
            "evidence": (
                "ABL259-CAPACITY-REVIEW.zh-CN.txt: normal loader raw/image_size "
                "room for this .259 ABL/XBL and the three stock Android ramdisk "
                "headers = 268435456 - 524288 - align_up(11090911,4096) - 8192 "
                "- 2097152 = 254713856 bytes; trial19 raw130163816 reached the "
                "final Linux initrd, which is NOT hardware acceptance of a new "
                ">128MiB image."),
        }
    return {
        "profile": STOCK_PROFILE_DEFAULT,
        "max_raw_bytes": DEFAULT_RAW_BUDGET,
        "stock_boot_sha256": stock_boot_sha256,
        "evidence": (
            "ABL259-CAPACITY-REVIEW.zh-CN.txt: unrecognized stock retains the "
            "existing conservative 128 MiB policy; this is not a measured "
            "loader limit."),
    }


def check_raw_size(raw_length, budget):
    check("raw:size<=loader-budget",
          raw_length <= budget["max_raw_bytes"],
          "len=%d max=%d profile=%s evidence=%s"
          % (raw_length, budget["max_raw_bytes"], budget["profile"],
             budget["evidence"]))


def check_bl_target(shim_offset):
    check("patch:w0-target-positive", shim_offset > 0, "%d" % shim_offset)
    check("patch:w0-target-in-range", shim_offset < BL_RANGE,
          "shim offset %d exceeds %d" % (shim_offset, BL_RANGE))


def le32(buf, offset):
    return struct.unpack_from("<I", buf, offset)[0]


def le64(buf, offset):
    return struct.unpack_from("<Q", buf, offset)[0]


def be32(buf, offset):
    return struct.unpack_from(">I", buf, offset)[0]


def align_up(value, alignment):
    return (value + alignment - 1) // alignment * alignment


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_bytes(path, label):
    if not os.path.isfile(path):
        raise StageError(STAGE, "%s not found: %s" % (label, path))
    with open(path, "rb") as handle:
        return handle.read()


def run(cmd, name, stdout_path=None, expect_success=True, cwd=None):
    effective_cwd = WORK if cwd is None else cwd
    COMMANDS.append({"name": name, "argv": list(cmd), "cwd": effective_cwd})
    err_log = os.path.join(LOGS, name + ".stderr")
    out_log = os.path.join(LOGS, name + ".stdout")
    if stdout_path is not None:
        with open(stdout_path, "wb") as fout, open(err_log, "wb") as ferr:
            proc = subprocess.run(cmd, stdout=fout, stderr=ferr,
                                  cwd=effective_cwd, env=ENV)
    else:
        with open(out_log, "wb") as fout, open(err_log, "wb") as ferr:
            proc = subprocess.run(cmd, stdout=fout, stderr=ferr,
                                  cwd=effective_cwd, env=ENV)
    if expect_success and proc.returncode != 0:
        raise StageError(
            STAGE, "command failed rc=%d: %s" % (proc.returncode, " ".join(cmd)))
    return proc.returncode, out_log, err_log


def run_capture(cmd, name, expect_success=True):
    """Run a command whose stdout is parsed (fdtget/dtc). Logs stay in OUT."""
    COMMANDS.append({"name": name, "argv": list(cmd)})
    out_log = os.path.join(LOGS, name + ".stdout")
    err_log = os.path.join(LOGS, name + ".stderr")
    with open(out_log, "wb") as fout, open(err_log, "wb") as ferr:
        proc = subprocess.run(cmd, stdout=fout, stderr=ferr, cwd=WORK, env=ENV)
    if expect_success and proc.returncode != 0:
        raise StageError(
            STAGE, "command failed rc=%d: %s" % (proc.returncode, " ".join(cmd)))
    with open(out_log, "rb") as handle:
        return proc.returncode, handle.read()


def require_tool(name):
    path = shutil.which(name)
    check("tool:%s" % name, path is not None, "not found in PATH")
    TOOL[name] = path
    return path


def parse_boot_header(data):
    return {
        "magic": data[0:8],
        "kernel_size": le32(data, 8),
        "ramdisk_size": le32(data, 12),
        "header_size": le32(data, 20),
        "version": le32(data, 40),
        "signature_size": le32(data, 1580),
    }


def parse_arm64(data, label):
    check(label + ":size>=64", len(data) >= ARM64_HDR_SIZE, "len=%d" % len(data))
    check(label + ":raw-not-qc",
          data[0:len(QC_ABOOT_MAGIC)] != QC_ABOOT_MAGIC,
          "QC aboot wrapper present")
    check(label + ":raw-not-gzip", data[0:2] != GZIP_MAGIC,
          "gzip stream present; raw ARM64 Image required")
    check(label + ":magic", data[56:60] == ARM64_MAGIC, "bad ARM64 magic at 0x38")
    text_offset = le64(data, 8)
    image_size = le64(data, 16)
    check(label + ":image_size", image_size >= 64, "image_size=%d" % image_size)
    slot = max(len(data), image_size)
    return {"text_offset": text_offset, "image_size": image_size,
            "length": len(data), "effective_slot": slot}


def parse_manifest(buf, offset):
    magic, vmaj, vmin, count, default, timeout = struct.unpack_from(
        "<IHHIII", buf, offset)
    backup = struct.unpack_from("<QQQQQ", buf, offset + 20)
    entries = []
    cursor = offset + MANIFEST_HEADER_SIZE
    for _ in range(count):
        (e_off, e_size, e_type, e_flags, e_load, e_entry, e_dtb, e_align,
         e_copy, e_namelen) = struct.unpack_from("<QQIIQIIIQI", buf, cursor)
        name_bytes = buf[cursor + MANIFEST_ENTRY_SIZE:
                         cursor + MANIFEST_ENTRY_SIZE + e_namelen]
        record = align_up(MANIFEST_ENTRY_SIZE + e_namelen, MANIFEST_ENTRY_ALIGN)
        pad = buf[cursor + MANIFEST_ENTRY_SIZE + e_namelen: cursor + record]
        entries.append({
            "offset": e_off, "size": e_size, "type": e_type, "flags": e_flags,
            "load_address": e_load, "entry_offset": e_entry,
            "dtb_index": e_dtb, "alignment": e_align, "copy_size_max": e_copy,
            "name_len": e_namelen, "name_bytes": name_bytes, "pad": pad,
            "record_size": record,
        })
        cursor += record
    return {"magic": magic, "version_major": vmaj, "version_minor": vmin,
            "count": count, "default": default, "timeout": timeout,
            "backup": backup, "entries": entries, "end": cursor}


def payload(raw, entry):
    return raw[entry["offset"]:entry["offset"] + entry["size"]]


def is_branch(word):
    return (word & 0xfc000000) == 0x14000000


def parse_dts(text):
    """Parse canonical dtc DTS text into {node_path: {property: raw_value}}.

    Only used to prove the derived DTB differs from the source DTB nowhere
    except the explicitly allowlisted /chosen properties. Values are compared as
    raw text; no numeric interpretation is attempted.
    """
    result = {}
    stack = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("//") or line.startswith("/*") \
                or line.startswith("*") or line.startswith("/memreserve/"):
            continue
        if line.startswith("/dts-v1/"):
            continue
        if line.endswith("{"):
            name = line[:-1].strip()
            if ":" in name:
                name = name.split(":", 1)[1].strip()
            if name == "":
                name = "/"
            stack.append(name)
            continue
        if line.startswith("}"):
            if stack:
                stack.pop()
            continue
        if line.endswith(";"):
            body = line[:-1].strip()
            if "=" in body:
                key, val = body.split("=", 1)
                key, val = key.strip(), val.strip()
            else:
                key, val = body.strip(), None
            path = "/" + "/".join(stack[1:]) if len(stack) > 1 else "/"
            result.setdefault(path, {})[key] = val
    return result


def dts_changes(before, after):
    changed = []
    for path in set(before) | set(after):
        props_b = before.get(path, {})
        props_a = after.get(path, {})
        for key in set(props_b) | set(props_a):
            if props_b.get(key, "<absent>") != props_a.get(key, "<absent>"):
                changed.append((path, key))
    return sorted(changed)


def config_safe(path, label):
    check("config:safe:" + label,
          not any(ch in path for ch in "\n\r#;"),
          "unsafe character in path: %r" % path)
    return path


def fdtget_strings(dtb, node, prop, name):
    rc, out = run_capture([TOOL["fdtget"], "-t", "s", dtb, node, prop], name)
    return [token for token in out.decode("utf-8", "replace").split() if token]


def fdtget_string(dtb, node, prop, name):
    rc, out = run_capture([TOOL["fdtget"], "-t", "s", dtb, node, prop], name)
    text = out.decode("utf-8", "replace")
    if text.endswith("\n"):
        text = text[:-1]
    return text


def fdtget_hex(dtb, node, prop, name):
    rc, out = run_capture([TOOL["fdtget"], "-t", "x", dtb, node, prop], name)
    return out.decode("utf-8", "replace").strip()


# Fixed BOOT-relative location of the Elden device tree, relative to fdtdir.
INSTALLED_ELDEN_DT_REL = "qcom/sm8750-lenovo-elden.dtb"


def resolve_installed_inputs(installer_result_path, boot_dir):
    """Resolve the mounted-BOOT inputs and the selected BLS boot entry.

    Reads the installer-result JSON, validates the four BOOT-relative paths
    (bls / image / initrd / fdtdir), the fixed Elden device tree and the
    selected BLS entry, then returns (selected_inputs, bootargs, metadata).

    selected_inputs holds absolute regular files only -- linux_image, initrd,
    dtb, installer_result and bls -- which the caller later checks and hashes
    with the existing prepare_environment.  metadata carries the real mounted
    boot_dir, the original deployment_checksum, the filesystem_uuids object,
    the selected BOOT-relative paths (bls / image / initrd / fdtdir) and the
    verbatim BLS options string.

    This helper is strictly read-only: it performs no source/BOOT write, no
    subprocess and no tool execution, does not verify an OCI image and never
    infers a deployment.  Every validation failure is a StageError at stage
    'installed-inputs' carrying an 'installed:' diagnostic.
    """
    set_stage("installed-inputs")

    result_path = os.path.abspath(installer_result_path)
    check("installed:result-file", os.path.isfile(result_path), result_path)
    try:
        with open(result_path, "r", encoding="utf-8") as handle:
            result = json.loads(handle.read())
        json_ok, json_detail = True, ""
    except (OSError, ValueError) as error:
        result = None
        json_ok, json_detail = False, "%s: %s" % (type(error).__name__, error)
    check("installed:result-json", json_ok, json_detail)
    check("installed:result-object", isinstance(result, dict),
          "installer-result is not a JSON object")

    check("installed:boot-dir", os.path.isdir(boot_dir), str(boot_dir))
    real_boot = os.path.realpath(boot_dir)

    def required(key):
        value = result.get(key)
        check("installed:result-%s" % key,
              isinstance(value, str) and value != "",
              "missing or empty %r" % key)
        return value

    deployment_checksum = required("deployment_checksum")
    bls_rel = required("bls")
    image_rel = required("image")
    initrd_rel = required("initrd")
    fdtdir_rel = required("fdtdir")
    options_json = required("options")

    uuids = result.get("filesystem_uuids")
    check("installed:uuids-object", isinstance(uuids, dict),
          "filesystem_uuids is not an object")
    boot_uuid = uuids.get("boot") if isinstance(uuids, dict) else None
    root_uuid = uuids.get("root") if isinstance(uuids, dict) else None
    check("installed:uuids-boot",
          isinstance(boot_uuid, str) and boot_uuid != "",
          "missing non-empty filesystem_uuids.boot")
    check("installed:uuids-root",
          isinstance(root_uuid, str) and root_uuid != "",
          "missing non-empty filesystem_uuids.root")

    def resolve_boot_path(value, label, allow_boot_prefix):
        """Normalize an optional /boot/ prefix, then resolve strictly inside
        the real mounted BOOT.  Absolute paths, empty values and '..'
        components are rejected.  Native loader symlinks are accepted only
        while the fully resolved path stays inside the mounted BOOT."""
        text = value
        if allow_boot_prefix and text.startswith("/boot/"):
            text = text[len("/boot/"):]
        check("installed:%s-relative" % label,
              text != "" and not os.path.isabs(text),
              "path must be BOOT-relative: %r" % value)
        check("installed:%s-no-parent" % label,
              ".." not in text.split("/"),
              "path must not contain '..': %r" % value)
        candidate = os.path.join(real_boot, text)
        real = os.path.realpath(candidate)
        check("installed:%s-inside-boot" % label,
              real == real_boot or real.startswith(real_boot + os.sep),
              "path escapes the mounted BOOT: %r -> %s" % (value, real))
        return candidate, real

    image_path, image_real = resolve_boot_path(image_rel, "image", False)
    initrd_path, initrd_real = resolve_boot_path(initrd_rel, "initrd", False)
    bls_path, bls_real = resolve_boot_path(bls_rel, "bls", False)
    fdtdir_path, fdtdir_real = resolve_boot_path(fdtdir_rel, "fdtdir", False)

    check("installed:image-file", os.path.isfile(image_path), image_path)
    check("installed:initrd-file", os.path.isfile(initrd_path), initrd_path)
    check("installed:bls-file", os.path.isfile(bls_path), bls_path)
    check("installed:fdtdir-dir", os.path.isdir(fdtdir_path), fdtdir_path)

    dtb_path = os.path.join(fdtdir_path, INSTALLED_ELDEN_DT_REL)
    dtb_real = os.path.realpath(dtb_path)
    check("installed:dtb-inside-boot",
          dtb_real == real_boot or dtb_real.startswith(real_boot + os.sep),
          "Elden DT escapes the mounted BOOT: %s" % dtb_real)
    check("installed:dtb-file", os.path.isfile(dtb_path), dtb_path)

    fields = {"linux": [], "initrd": [], "fdtdir": [], "options": []}
    with open(bls_path, "r", encoding="utf-8") as handle:
        bls_text = handle.read()
    for raw_line in bls_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        key = parts[0]
        if key in fields:
            fields[key].append(parts[1].strip() if len(parts) > 1 else "")

    for key in ("linux", "initrd", "fdtdir", "options"):
        check("installed:bls-%s-single" % key,
              len(fields[key]) == 1,
              "expected exactly one %r line, found %d"
              % (key, len(fields[key])))

    bls_linux_path, bls_linux_real = resolve_boot_path(
        fields["linux"][0], "bls-linux", True)
    bls_initrd_path, bls_initrd_real = resolve_boot_path(
        fields["initrd"][0], "bls-initrd", True)
    bls_fdtdir_path, bls_fdtdir_real = resolve_boot_path(
        fields["fdtdir"][0], "bls-fdtdir", True)

    check("installed:bls-linux-selected", bls_linux_real == image_real,
          "BLS linux does not name the installer-result image")
    check("installed:bls-initrd-selected", bls_initrd_real == initrd_real,
          "BLS initrd does not name the installer-result initrd")
    check("installed:bls-fdtdir-selected", bls_fdtdir_real == fdtdir_real,
          "BLS fdtdir does not name the installer-result fdtdir")

    bootargs = fields["options"][0]
    check("installed:options-match", bootargs == options_json,
          "BLS options differ from installer-result options")
    check("installed:options-single-line",
          not any(ch in bootargs for ch in "\n\r\x00"),
          "options must be a single line without NUL")

    tokens = bootargs.split()
    root_values = [t[len("root="):] for t in tokens if t.startswith("root=")]
    boot_values = [t[len("boot="):] for t in tokens if t.startswith("boot=")]
    ostree_values = [t[len("ostree="):] for t in tokens
                     if t.startswith("ostree=")]
    check("installed:arg-root-single", len(root_values) == 1,
          "expected exactly one root= argument, found %d" % len(root_values))
    check("installed:arg-boot-single", len(boot_values) == 1,
          "expected exactly one boot= argument, found %d" % len(boot_values))
    check("installed:arg-ostree-single", len(ostree_values) == 1,
          "expected exactly one ostree= argument, found %d"
          % len(ostree_values))
    check("installed:arg-ostree-nonempty", ostree_values[0] != "",
          "ostree= must be non-empty")
    check("installed:arg-root-uuid", root_values[0] == "UUID=" + root_uuid,
          "root= must be UUID=%s" % root_uuid)
    check("installed:arg-boot-uuid", boot_values[0] == "UUID=" + boot_uuid,
          "boot= must be UUID=%s" % boot_uuid)

    selected_inputs = {
        "linux_image": image_path,
        "initrd": initrd_path,
        "dtb": dtb_path,
        "installer_result": result_path,
        "bls": bls_path,
    }
    metadata = {
        "boot_dir": real_boot,
        "deployment_checksum": deployment_checksum,
        "filesystem_uuids": {"boot": boot_uuid, "root": root_uuid},
        "bls": bls_rel,
        "image": image_rel,
        "initrd": initrd_rel,
        "fdtdir": fdtdir_rel,
        "options": bootargs,
    }
    return selected_inputs, bootargs, metadata


def main():
    global OUT, WORK, LOGS, TMP, ENV

    parser = argparse.ArgumentParser(
        description="Offline Gen4 TB322FC Linux first-boot packaging driver")
    parser.add_argument("--layout", choices=("maintenance", "installed"),
                        default="maintenance")
    parser.add_argument("--stock-boot", required=True)
    parser.add_argument("--linux-image")
    parser.add_argument("--dtb")
    parser.add_argument("--initrd")
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--packer", required=True)
    parser.add_argument("--unpacker", required=True)
    parser.add_argument("--repacker", required=True)
    parser.add_argument("--avbtool", required=True)
    parser.add_argument("--bootargs")
    parser.add_argument("--installer-result")
    parser.add_argument("--boot-dir")
    parser.add_argument("--out", required=True)
    parser.add_argument("--source-label", default="")
    parser.add_argument("--default-entry",
                        choices=sorted(set(DEFAULT_ENTRY_CHOICES)
                                       | set(INSTALLED_DEFAULT_ENTRY_CHOICES)),
                        default=None)
    args = parser.parse_args()

    layout = args.layout

    # Conditional input requirements, enforced before any path resolution or
    # output creation.  parser.error() exits 2; argparse's own invalid-choice
    # handling for --default-entry is unchanged.
    manual_inputs = (
        ("--linux-image", args.linux_image),
        ("--dtb", args.dtb),
        ("--initrd", args.initrd),
        ("--bootargs", args.bootargs),
    )
    if layout == "maintenance":
        missing = [flag for flag, value in manual_inputs if value is None]
        if missing:
            parser.error("--layout maintenance requires " + ", ".join(missing))
        if args.installer_result is not None or args.boot_dir is not None:
            parser.error("--installer-result and --boot-dir are only valid "
                         "with --layout installed")
        default_entry = args.default_entry
        if default_entry is None:
            default_entry = DEFAULT_ENTRY
        if default_entry not in DEFAULT_ENTRY_CHOICES:
            parser.error("--layout maintenance allows --default-entry "
                         "android or maintenance")
    else:
        supplied = [flag for flag, value in manual_inputs if value is not None]
        if supplied:
            parser.error("%s must not be supplied with --layout installed"
                         % ", ".join(supplied))
        if args.installer_result is None:
            parser.error("--layout installed requires --installer-result")
        if args.boot_dir is None:
            parser.error("--layout installed requires --boot-dir")
        default_entry = args.default_entry
        if default_entry is None:
            default_entry = "internal"
        if default_entry not in INSTALLED_DEFAULT_ENTRY_CHOICES:
            parser.error("--layout installed allows --default-entry "
                         "android or internal")

    # Resolve every common input and tool to an absolute path BEFORE any cwd
    # change.
    inputs = {
        "stock_boot": os.path.abspath(args.stock_boot),
        "runtime": os.path.abspath(args.runtime),
        "packer": os.path.abspath(args.packer),
        "unpacker": os.path.abspath(args.unpacker),
        "repacker": os.path.abspath(args.repacker),
        "avbtool": os.path.abspath(args.avbtool),
    }
    OUT = os.path.abspath(args.out)

    try:
        if layout == "installed":
            selected_inputs, bootargs, installed_metadata = \
                resolve_installed_inputs(args.installer_result, args.boot_dir)
            inputs.update(selected_inputs)
        else:
            inputs["linux_image"] = os.path.abspath(args.linux_image)
            inputs["dtb"] = os.path.abspath(args.dtb)
            inputs["initrd"] = os.path.abspath(args.initrd)
            bootargs = args.bootargs
            installed_metadata = None
        return run_driver(inputs, args.source_label, bootargs, default_entry,
                          layout=layout, installed_metadata=installed_metadata)
    except StageError as error:
        stage, message = error.stage, error.message
    except Exception as error:  # unexpected failure still leaves evidence
        stage, message = STAGE, "%s: %s" % (type(error).__name__, error)

    failure = {
        "stage": stage,
        "error": message,
        "source_label": args.source_label,
        "offline_verified": False,
        "offline_only": True,
        "hardware_tested": False,
        "checks": CHECKS,
        "commands": COMMANDS,
    }
    if OUT_OWNED:
        try:
            with open(os.path.join(OUT, "failure.json"), "w",
                      encoding="utf-8") as handle:
                json.dump(failure, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
        except OSError:
            pass
    sys.stderr.write("FAILED at stage '%s': %s\n" % (stage, message))
    return 1


def args_out_raw():
    for index, value in enumerate(sys.argv):
        if value == "--out" and index + 1 < len(sys.argv):
            return sys.argv[index + 1]
    return OUT


def prepare_environment(inputs):
    global WORK, LOGS, TMP, ENV, OUT_OWNED

    set_stage("prepare")
    for key, path in inputs.items():
        check("input:%s" % key, os.path.isfile(path), path)
    check("out:not-symlink", not os.path.islink(args_out_raw()), "output is a symlink")
    if os.path.exists(OUT):
        check("out:is-dir", os.path.isdir(OUT), "exists and is not a directory")
        check("out:empty", not os.listdir(OUT), "existing directory is not empty")
    else:
        os.makedirs(OUT)

    real_out = os.path.realpath(OUT)
    for key, path in inputs.items():
        real_in = os.path.realpath(path)
        check("out:distinct:%s" % key, real_in != real_out, "output equals input")
        check("out:not-inside:%s" % key,
              not real_in.startswith(real_out + os.sep),
              "input %s lives inside --out" % key)

    OUT_OWNED = True
    WORK = os.path.join(OUT, "work")
    LOGS = os.path.join(OUT, "logs")
    TMP = os.path.join(OUT, "tmp")
    for path in (WORK, LOGS, TMP):
        os.makedirs(path)

    ENV = dict(os.environ)
    ENV["PYTHONDONTWRITEBYTECODE"] = "1"
    ENV["TMPDIR"] = TMP
    ENV["TEMP"] = TMP
    ENV["TMP"] = TMP
    tempfile.tempdir = TMP
    os.chdir(WORK)


def build_dtb(inputs, bootargs, boot_stage="maintenance", root_medium=None,
              initrd_index=3, tag=""):
    """Copy the supplied DTB into WORK and set only the allowlisted /chosen
    properties. Returns (derived_path, derived_bytes, changed, dts_paths).

    The default call (boot_stage='maintenance', no root_medium, initrd_index=3,
    empty tag) reproduces the legacy maintenance artifact byte-for-byte.  A
    non-empty tag gives the derived/source DTS files and every command log a
    distinct name so two installed device trees never overwrite each other's
    evidence.
    """
    set_stage("dtb")

    def tagged(name):
        return (tag + "-" + name) if tag else name

    allowed_chosen = ALLOWED_CHOSEN_PROPS
    if root_medium is not None:
        allowed_chosen = ALLOWED_CHOSEN_PROPS | {"armada,root-medium"}

    source = read_bytes(inputs["dtb"], "source dtb")
    check("dtb:magic", len(source) >= 8 and be32(source, 0) == FDT_MAGIC,
          "bad FDT magic")
    check("dtb:totalsize", len(source) >= 8 and be32(source, 4) == len(source),
          "totalsize=%s len=%d" % (be32(source, 4) if len(source) >= 8 else -1,
                                   len(source)))

    compatible = fdtget_strings(inputs["dtb"], "/", "compatible",
                                tagged("fdtget-compat-src"))
    # Accept either known Linux board pairing; everything else is rejected.
    is_elden = all(entry in compatible for entry in REQUIRED_COMPATIBLE)
    is_tb321fu = all(entry in compatible
                     for entry in REQUIRED_COMPATIBLE_TB321FU)
    check("dtb:compatible-known-board", is_elden or is_tb321fu,
          "compatible=%r" % (compatible,))
    for forbidden in sorted(FORBIDDEN_COMPATIBLE):
        check("dtb:compatible-forbidden:%s" % forbidden,
              forbidden not in compatible, "compatible=%r" % compatible)

    derived = os.path.join(WORK, tagged("derived.dtb"))
    with open(derived, "wb") as handle:
        handle.write(source)

    # Ensure /chosen exists.
    rc, listing = run_capture([TOOL["fdtget"], "-l", derived, "/"],
                              tagged("fdtget-list-root"))
    nodes = listing.decode("utf-8", "replace").split()
    if "chosen" not in nodes:
        run_capture([TOOL["fdtput"], "-c", derived, "/chosen"],
                    tagged("fdtput-create-chosen"))
    run_capture([TOOL["fdtget"], "-l", derived, "/chosen"],
                tagged("fdtget-list-chosen"))

    # Before snapshot (canonical DTS).
    before_dts = os.path.join(WORK, tagged("source.dts"))
    run_capture([TOOL["dtc"], "-I", "dtb", "-O", "dts", "-o", before_dts,
                 inputs["dtb"]], tagged("dtc-source"))
    with open(before_dts, encoding="utf-8", errors="replace") as handle:
        before_text = handle.read()

    # Remove any stale initrd address properties before writing the entry cell.
    rc, props = run_capture([TOOL["fdtget"], "-p", derived, "/chosen"],
                            tagged("fdtget-props-chosen"))
    present = set(props.decode("utf-8", "replace").split())
    for stale in ("linux,initrd-start", "linux,initrd-end"):
        if stale in present:
            run_capture([TOOL["fdtput"], "-d", derived, "/chosen", stale],
                        tagged("fdtput-remove-" + stale.replace(",", "_")))

    run_capture([TOOL["fdtput"], "-t", "s", derived, "/chosen", "bootargs",
                 bootargs], tagged("fdtput-bootargs"))
    run_capture([TOOL["fdtput"], "-t", "s", derived, "/chosen",
                 "armada,boot-stage", boot_stage], tagged("fdtput-boot-stage"))
    run_capture([TOOL["fdtput"], "-t", "x", derived, "/chosen",
                 "kshim,initrd-entry", str(initrd_index)],
                tagged("fdtput-initrd-entry"))
    if root_medium is not None:
        run_capture([TOOL["fdtput"], "-t", "s", derived, "/chosen",
                     "armada,root-medium", root_medium],
                    tagged("fdtput-root-medium"))

    after_dts = os.path.join(WORK, tagged("derived.dts"))
    run_capture([TOOL["dtc"], "-I", "dtb", "-O", "dts", "-o", after_dts,
                 derived], tagged("dtc-derived"))
    with open(after_dts, encoding="utf-8", errors="replace") as handle:
        after_text = handle.read()

    changed = dts_changes(parse_dts(before_text), parse_dts(after_text))
    for path, prop in changed:
        check("dtb:change-allowed:%s:%s" % (path, prop),
              path == "/chosen" and prop in allowed_chosen,
              "unexpected structural change at %s %s" % (path, prop))

    # Re-read the derived DTB properties and compatible list.
    derived_bytes = read_bytes(derived, "derived dtb")
    check("dtb:derived-magic", be32(derived_bytes, 0) == FDT_MAGIC, "bad magic")
    check("dtb:derived-totalsize", be32(derived_bytes, 4) == len(derived_bytes),
          "totalsize mismatch")
    d_compat = fdtget_strings(derived, "/", "compatible",
                              tagged("fdtget-compat-derived"))
    check("dtb:derived-compatible-known-board",
          all(entry in d_compat for entry in REQUIRED_COMPATIBLE) or
          all(entry in d_compat for entry in REQUIRED_COMPATIBLE_TB321FU),
          "compatible=%r" % (d_compat,))
    for forbidden in sorted(FORBIDDEN_COMPATIBLE):
        check("dtb:derived-compatible-forbidden:%s" % forbidden,
              forbidden not in d_compat, "compatible=%r" % d_compat)

    got_bootargs = fdtget_string(
        derived, "/chosen", "bootargs", tagged("fdtget-bootargs"))
    check("dtb:derived-bootargs", got_bootargs == bootargs,
          "bootargs=%r expected=%r" % (got_bootargs, bootargs))
    stage = "".join(fdtget_strings(derived, "/chosen", "armada,boot-stage",
                                   tagged("fdtget-boot-stage")))
    check("dtb:derived-stage", stage == boot_stage, "stage=%r" % stage)
    entry = fdtget_hex(derived, "/chosen", "kshim,initrd-entry",
                       tagged("fdtget-initrd-entry"))
    check("dtb:derived-initrd-entry-single", entry and " " not in entry,
          "not exactly one 32-bit cell: %r" % entry)
    check("dtb:derived-initrd-entry", int(entry, 16) == initrd_index,
          "initrd-entry=%s expected=%d" % (entry, initrd_index))
    if root_medium is not None:
        got_medium = fdtget_string(derived, "/chosen", "armada,root-medium",
                                   tagged("fdtget-root-medium"))
        check("dtb:derived-root-medium", got_medium == root_medium,
              "root-medium=%r expected=%r" % (got_medium, root_medium))

    return derived, derived_bytes, changed, {"source_dts": before_dts,
                                             "derived_dts": after_dts}


def write_pack_config(config_path, inputs, stock_path, padded_path, derived_path,
                      packed_kernel, default_label, layout="maintenance",
                      diagnostic_path=None):
    if layout == "installed":
        lines = [
            "[Pack]",
            "Shim=" + config_safe(inputs["runtime"], "shim"),
            "Output=" + config_safe(packed_kernel, "output"),
            "Default=" + default_label,
            "Timeout=%d" % TIMEOUT_MS,
            "",
            "[Image-00]",
            "Name=" + BASE_NAME,
            "Path=" + config_safe(stock_path, "image00"),
            "Type=Linux",
            "BaseImage=true",
            "Align=21",
            "",
            "[Image-01]",
            "Name=" + INTERNAL_NAME,
            "Path=" + config_safe(padded_path, "image01"),
            "Type=Linux",
            "BaseImage=false",
            "Align=21",
            "EntryOffset=0",
            "DeviceTreeBlob=Image-03",
            "",
            # Diagnostic alias: shares Image-01's payload; the packer disallows
            # even BaseImage=false here, so only PayloadFrom/Type/Align remain.
            "[Image-02]",
            "Name=" + LINUX_NAME,
            "Type=Linux",
            "PayloadFrom=Image-01",
            "Align=21",
            "EntryOffset=0",
            "DeviceTreeBlob=Image-04",
            "",
            "[Image-03]",
            "Name=" + INTERNAL_DTB_NAME,
            "Path=" + config_safe(derived_path, "image03"),
            "Type=DTB",
            "BaseImage=false",
            "Align=3",
            "",
            "[Image-04]",
            "Name=" + DTB_NAME,
            "Path=" + config_safe(diagnostic_path, "image04"),
            "Type=DTB",
            "BaseImage=false",
            "Align=3",
            "",
            "[Image-05]",
            "Name=" + INSTALLED_BLOB_NAME,
            "Path=" + config_safe(inputs["initrd"], "image05"),
            "Type=Blob",
            "BaseImage=false",
            "Align=16",
            "",
            "[Manifest]",
            "Type=Manifest",
            "",
        ]
    else:
        lines = [
            "[Pack]",
            "Shim=" + config_safe(inputs["runtime"], "shim"),
            "Output=" + config_safe(packed_kernel, "output"),
            "Default=" + default_label,
            "Timeout=%d" % TIMEOUT_MS,
            "",
            "[Image-00]",
            "Name=" + BASE_NAME,
            "Path=" + config_safe(stock_path, "image00"),
            "Type=Linux",
            "BaseImage=true",
            "Align=21",
            "",
            "[Image-01]",
            "Name=" + LINUX_NAME,
            "Path=" + config_safe(padded_path, "image01"),
            "Type=Linux",
            "BaseImage=false",
            "Align=21",
            "EntryOffset=0",
            "DeviceTreeBlob=Image-02",
            "",
            "[Image-02]",
            "Name=" + DTB_NAME,
            "Path=" + config_safe(derived_path, "image02"),
            "Type=DTB",
            "BaseImage=false",
            "Align=3",
            "",
            "[Image-03]",
            "Name=" + BLOB_NAME,
            "Path=" + config_safe(inputs["initrd"], "image03"),
            "Type=Blob",
            "BaseImage=false",
            "Align=16",
            "",
            "[Manifest]",
            "Type=Manifest",
            "",
        ]
    with open(config_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


def run_driver(inputs, source_label, bootargs, default_entry=None,
               layout="maintenance", installed_metadata=None):
    # Layout profile: manifest entry table, default choices and the profile
    # indices used throughout the pipeline.  The owner entry is always Image-01;
    # only the DTB/Blob/Shim indices and the alias/diagnostic slots differ.
    if layout == "installed":
        choice_map = INSTALLED_DEFAULT_ENTRY_CHOICES
        expected_entries = INSTALLED_EXPECTED_ENTRIES
        owner_index, dtb_index, blob_index, shim_index = 1, 3, 5, 6
        alias_index, diagnostic_index = 2, 4
    else:
        choice_map = DEFAULT_ENTRY_CHOICES
        expected_entries = EXPECTED_ENTRIES
        owner_index, dtb_index, blob_index, shim_index = 1, 2, 3, 4
        alias_index, diagnostic_index = None, None
    if default_entry is None:
        default_entry = "internal" if layout == "installed" else DEFAULT_ENTRY
    default_label, default_index = choice_map[default_entry]
    prepare_environment(inputs)

    for name in ("fdtput", "fdtget", "dtc", "gzip"):
        require_tool(name)

    set_stage("validate-inputs")
    stock_boot = read_bytes(inputs["stock_boot"], "stock boot")
    linux_image = read_bytes(inputs["linux_image"], "linux image")
    runtime = read_bytes(inputs["runtime"], "runtime shim")

    check("stock_boot:size", len(stock_boot) == BOOT_SIZE,
          "len=%d expected=%d" % (len(stock_boot), BOOT_SIZE))
    boot = parse_boot_header(stock_boot)
    check("stock_boot:magic", boot["magic"] == BOOT_MAGIC, repr(boot["magic"]))
    check("stock_boot:version", boot["version"] == 4, "version=%d" % boot["version"])
    check("stock_boot:header_size", boot["header_size"] == 1584,
          "header_size=%d" % boot["header_size"])
    check("stock_boot:ramdisk_size", boot["ramdisk_size"] == 0,
          "ramdisk_size=%d" % boot["ramdisk_size"])
    check("stock_boot:signature_size", boot["signature_size"] == 0,
          "signature_size=%d" % boot["signature_size"])
    kernel_size = boot["kernel_size"]
    check("stock_boot:kernel_size",
          kernel_size != 0 and 4096 + kernel_size <= len(stock_boot),
          "kernel_size=%d" % kernel_size)
    check("stock_boot:avb-footer",
          len(stock_boot) >= 64 and stock_boot[-64:-60] == AVB_MAGIC,
          "stock boot image has no AVB footer")

    stock_image = stock_boot[4096:4096 + kernel_size]
    check("stock_image:len", len(stock_image) == kernel_size,
          "len=%d kernel_size=%d" % (len(stock_image), kernel_size))
    stock_info = parse_arm64(stock_image, "stock_image")

    check("runtime:size", len(runtime) == RUNTIME_SIZE,
          "len=%d expected=%d" % (len(runtime), RUNTIME_SIZE))

    linux_info = parse_arm64(linux_image, "linux_image")
    target = max(len(linux_image), linux_info["image_size"])
    padded = linux_image + b"\x00" * (target - len(linux_image))
    check("linux:padded", len(padded) == target, "len=%d" % len(padded))
    linux_reserved = align_up(len(padded), 4096)
    bss_pad = linux_reserved - len(padded)

    for key in ("packer", "unpacker", "repacker", "avbtool"):
        check("tool:%s" % key, os.path.isfile(inputs[key]), inputs[key])

    source_digests = {key: sha256_file(path) for key, path in inputs.items()}
    source_sizes = {key: os.path.getsize(path) for key, path in inputs.items()}

    raw_budget = select_raw_budget(source_digests["stock_boot"])

    stock_path = os.path.join(WORK, "StockImage")
    with open(stock_path, "wb") as handle:
        handle.write(stock_image)
    padded_path = os.path.join(WORK, "padded-Image")
    with open(padded_path, "wb") as handle:
        handle.write(padded)

    if layout == "installed":
        # Primary (internal) device tree: internal boot stage, internal root
        # medium and the real formal initramfs Blob entry cell.  The same
        # selected source DTB is derived again for the diagnostic alias with a
        # distinct tag, so artifact and log names never collide.  The selected
        # BLS options are preserved verbatim -- the diagnostic tree appends only
        # a pre-mount break -- and are written over /chosen bootargs, replacing
        # the source DT bootargs wholesale so a stale rdinit=/init from the
        # source DT is never inherited; no separate maintenance initrd is
        # substituted -- both device trees reference the same formal initrd
        # Blob.
        derived_path, derived_bytes, dtb_changed, dts_paths = build_dtb(
            inputs, bootargs, boot_stage="os", root_medium="internal",
            initrd_index=blob_index, tag="internal")
        diagnostic_bootargs = bootargs + " rd.break=pre-mount"
        (diagnostic_path, diagnostic_bytes, diagnostic_changed,
         diagnostic_dts) = build_dtb(
            inputs, diagnostic_bootargs, boot_stage="maintenance",
            root_medium="internal", initrd_index=blob_index, tag="diagnostic")
    else:
        derived_path, derived_bytes, dtb_changed, dts_paths = build_dtb(
            inputs, bootargs)
        diagnostic_path = None
        diagnostic_bootargs = None
        diagnostic_bytes = None
        diagnostic_changed = None
        diagnostic_dts = None

    set_stage("config")
    packed_kernel = os.path.join(OUT, "PackedKernel")
    config_path = os.path.join(WORK, "build.cfg")
    write_pack_config(config_path, inputs, stock_path, padded_path, derived_path,
                      packed_kernel, default_label, layout=layout,
                      diagnostic_path=diagnostic_path)

    set_stage("pack")
    run([inputs["packer"], config_path], "packer")
    raw = read_bytes(packed_kernel, "PackedKernel")

    set_stage("assert-pack")
    check_raw_size(len(raw), raw_budget)
    check("raw:arm64-magic", raw[56:60] == ARM64_MAGIC, "bad magic")
    check("raw:image_size==len", le64(raw, 16) == len(raw),
          "image_size=%d len=%d" % (le64(raw, 16), len(raw)))
    check("raw:text_offset-intact", le64(raw, 8) == stock_info["text_offset"],
          "text_offset changed")
    check("raw:flags-intact", le64(raw, 0x18) == le64(stock_image, 0x18),
          "flags changed")
    manifest_offset = le64(raw, 32)
    manifest_size = le64(raw, 40)
    shim_offset = le64(raw, 48)
    check("raw:manifest-bounds",
          manifest_size >= MANIFEST_HEADER_SIZE
          and manifest_offset + manifest_size <= len(raw),
          "off=%d size=%d len=%d" % (manifest_offset, manifest_size, len(raw)))

    manifest = parse_manifest(raw, manifest_offset)
    check("manifest:magic", manifest["magic"] == MANIFEST_MAGIC,
          hex(manifest["magic"]))
    check("manifest:version",
          manifest["version_major"] == 0 and manifest["version_minor"] == 1,
          "%d.%d" % (manifest["version_major"], manifest["version_minor"]))
    check("manifest:count", manifest["count"] == len(expected_entries),
          "%d" % manifest["count"])
    check("manifest:default", manifest["default"] == default_index,
          "%d" % manifest["default"])
    check("manifest:timeout", manifest["timeout"] == TIMEOUT_MS,
          "%d" % manifest["timeout"])
    check("manifest:end", manifest["end"] == manifest_offset + manifest_size,
          "entry table does not fill manifest")

    expected_backup = (
        le64(stock_image, 0),
        le64(stock_image, 16),
        le64(stock_image, 32),
        le64(stock_image, 40),
        le64(stock_image, 48),
    )
    check("manifest:backup", manifest["backup"] == expected_backup,
          "backup=%r expected=%r" % (manifest["backup"], expected_backup))

    entries = manifest["entries"]
    for index, name, e_type, e_flags, e_align, e_dtb in expected_entries:
        entry = entries[index]
        check("entry%d:name" % index,
              entry["name_bytes"].decode("utf-8", "replace") == name,
              entry["name_bytes"].decode("utf-8", "replace"))
        check("entry%d:name_len" % index,
              entry["name_len"] == len(name.encode("utf-8")),
              "%d" % entry["name_len"])
        check("entry%d:padding" % index,
              entry["pad"] == b"\x00" * len(entry["pad"]),
              "non-zero record padding")
        check("entry%d:type" % index, entry["type"] == e_type, "%d" % entry["type"])
        check("entry%d:flags" % index, entry["flags"] == e_flags,
              "%d" % entry["flags"])
        check("entry%d:align" % index, entry["alignment"] == e_align,
              "%d" % entry["alignment"])
        check("entry%d:load" % index, entry["load_address"] == 0,
              "%d" % entry["load_address"])
        check("entry%d:entry_offset" % index, entry["entry_offset"] == 0,
              "%d" % entry["entry_offset"])
        check("entry%d:copy_size_max" % index, entry["copy_size_max"] == 0,
              "%d" % entry["copy_size_max"])
        check("entry%d:dtb_index" % index, entry["dtb_index"] == e_dtb,
              "%d" % entry["dtb_index"])

    base = entries[0]
    check("base:offset", base["offset"] == 0, "%d" % base["offset"])
    check("base:size", base["size"] == len(stock_image),
          "%d expected=%d" % (base["size"], len(stock_image)))
    check("base:no-has-dtb", (base["flags"] & FLAG_HAS_DTB) == 0, "HAS_DTB set")
    check("base:no-qc", (base["flags"] & FLAG_QC) == 0, "QC flag set")

    linux = entries[owner_index]
    check("linux:type", linux["type"] == MF_TYPE_LINUX, "%d" % linux["type"])
    check("linux:size==padded", linux["size"] == len(padded),
          "%d expected=%d" % (linux["size"], len(padded)))

    if layout == "installed":
        # The diagnostic alias is NOT a second physical interval: it must share
        # the owner's offset and size.  Checked here, before any later check
        # that would otherwise fail first for a deliberately shifted alias.
        alias = entries[alias_index]
        check("linux:alias-shared-owner",
              alias["offset"] == linux["offset"]
              and alias["size"] == linux["size"],
              "alias off=%d size=%d owner off=%d size=%d"
              % (alias["offset"], alias["size"], linux["offset"],
                 linux["size"]))

    dtb_entry = entries[dtb_index]
    check("dtb:entry-type", dtb_entry["type"] == MF_TYPE_DTB, "%d" % dtb_entry["type"])
    check("dtb:entry-size==source", dtb_entry["size"] == len(derived_bytes),
          "%d expected=%d" % (dtb_entry["size"], len(derived_bytes)))

    if layout == "installed":
        diagnostic_entry = entries[diagnostic_index]
        check("diagnostic-dtb:entry-type",
              diagnostic_entry["type"] == MF_TYPE_DTB,
              "%d" % diagnostic_entry["type"])
        check("diagnostic-dtb:entry-size==source",
              diagnostic_entry["size"] == len(diagnostic_bytes),
              "%d expected=%d" % (diagnostic_entry["size"],
                                  len(diagnostic_bytes)))
    else:
        diagnostic_entry = None

    blob_entry = entries[blob_index]
    check("blob:type", blob_entry["type"] == MF_TYPE_BLOB, "%d" % blob_entry["type"])
    check("blob:size==initrd", blob_entry["size"] == source_sizes["initrd"],
          "%d expected=%d" % (blob_entry["size"], source_sizes["initrd"]))
    check("blob:nonempty", blob_entry["size"] > 0, "initrd is empty")

    shim = entries[shim_index]
    check("shim:type", shim["type"] == MF_TYPE_SHIM, "%d" % shim["type"])
    check("shim:offset==header", shim["offset"] == shim_offset,
          "entry=%d header=%d" % (shim["offset"], shim_offset))
    check("shim:size", shim["size"] == RUNTIME_SIZE, "%d" % shim["size"])
    check("shim:payload==runtime", payload(raw, shim) == runtime,
          "shim payload differs from runtime input")

    # Restore the backed-up header slots and require whole-image byte equality.
    restored = bytearray(payload(raw, base))
    for slot, offset in enumerate((0, 16, 32, 40, 48)):
        struct.pack_into("<Q", restored, offset, manifest["backup"][slot])
    check("base:payload==stock_image", bytes(restored) == stock_image,
          "base payload differs from stock image")

    linux_payload = payload(raw, linux)
    check("linux:payload==padded", linux_payload == padded,
          "linux payload differs from padded Image")
    check("linux:prefix==source", linux_payload[:len(linux_image)] == linux_image,
          "linux payload prefix differs from --linux-image")
    check("linux:zero-pad",
          linux_payload[len(linux_image):] == b"\x00" * (len(padded) - len(linux_image)),
          "non-zero Linux tail padding")
    if layout == "installed":
        check("linux:alias-payload==owner", payload(raw, alias) == linux_payload,
              "alias payload differs from owner payload")
        check("linux:alias-dtb-ref", alias["dtb_index"] == 4,
              "alias DT ref=%d" % alias["dtb_index"])
    check("dtb:payload==derived", payload(raw, dtb_entry) == derived_bytes,
          "DTB payload differs from derived DTB")
    if layout == "installed":
        check("diagnostic-dtb:payload==derived",
              payload(raw, diagnostic_entry) == diagnostic_bytes,
              "diagnostic DTB payload differs from derived DTB")
    initrd_bytes = read_bytes(inputs["initrd"], "initrd")
    check("blob:payload==initrd", payload(raw, blob_entry) == initrd_bytes,
          "Blob payload differs from --initrd")

    # Sequential file regions: base, shim, linux, primary DTB, (diagnostic DTB),
    # blob, then manifest.  The alias shares the owner interval, so it adds no
    # region and no artificial gap.  Zero gaps/tails and no overlap; manifest
    # ends at end of file.
    order = [
        ("base", base),
        ("shim", shim),
        ("linux", linux),
        ("dtb", dtb_entry),
    ]
    if layout == "installed":
        order.append(("diagnostic-dtb", diagnostic_entry))
    order.append(("blob", blob_entry))
    regions = []
    for name, entry in order:
        start = entry["offset"]
        end = start + entry["size"]
        check("layout:%s:align" % name, start % (1 << entry["alignment"]) == 0,
              "offset=%d align=2^%d" % (start, entry["alignment"]))
        regions.append((name, start, end, entry["size"]))

    previous_end = 0
    for name, start, end, used in regions:
        check("layout:%s:ordered" % name, start >= previous_end,
              "start=%d previous_end=%d" % (start, previous_end))
        check("layout:%s:in-file" % name, end <= len(raw),
              "end=%d len=%d" % (end, len(raw)))
        if start > previous_end:
            check("layout:%s:gap-zero" % name,
                  raw[previous_end:start] == b"\x00" * (start - previous_end),
                  "non-zero gap before %s" % name)
        previous_end = end

    check("layout:linux:base-text-residue",
          (linux["offset"] % 4096) == (base["offset"] % 4096),
          "linux offset residue differs from base text residue")
    check("layout:blob-after-linux-reserved",
          blob_entry["offset"] >= linux["offset"] + linux_reserved,
          "initrd starts before Linux reserved runtime end")
    check("layout:dtb-after-linux-reserved",
          dtb_entry["offset"] >= linux["offset"] + linux_reserved,
          "DTB starts before Linux reserved runtime end")
    if layout == "installed":
        check("layout:diagnostic-dtb-after-linux-reserved",
              diagnostic_entry["offset"] >= linux["offset"] + linux_reserved,
              "diagnostic DTB starts before Linux reserved runtime end")
    check("layout:linux:bss-pad-zero",
          raw[linux["offset"] + len(padded):linux["offset"] + linux_reserved]
          == b"\x00" * bss_pad,
          "non-zero Linux BSS padding")
    check("layout:manifest-after-payloads", manifest_offset >= previous_end,
          "manifest=%d previous_end=%d" % (manifest_offset, previous_end))
    check("layout:manifest-gap-zero",
          raw[previous_end:manifest_offset] == b"\x00" * (manifest_offset - previous_end),
          "non-zero gap before manifest")
    check("layout:ends-at-manifest", manifest_offset + manifest_size == len(raw),
          "trailing bytes after manifest")

    # Packer patch verifier: the base head is redirected to the attached shim.
    w0 = le32(raw, 0)
    w1 = le32(raw, 4)
    expected_w0 = 0x94000000 | ((shim_offset >> 2) & 0x03ffffff)
    check("patch:w0", w0 == expected_w0,
          "w0=%08x expected=%08x" % (w0, expected_w0))
    imm = w0 & 0x03ffffff
    if imm & 0x02000000:
        imm -= 0x04000000
    check_bl_target(shim_offset)
    check("patch:w0-target", imm * 4 == shim_offset,
          "target=%d shim=%d" % (imm * 4, shim_offset))

    orig_w0 = le32(stock_image, 0)
    orig_w1 = le32(stock_image, 4)
    if is_branch(orig_w0) and not is_branch(orig_w1):
        expected_w1 = 0x14000000 | ((orig_w0 - 1) & 0x03ffffff)
    else:
        check("patch:orig-w1-is-branch", is_branch(orig_w1), "%08x" % orig_w1)
        expected_w1 = orig_w1
    check("patch:w1", w1 == expected_w1, "w1=%08x expected=%08x" % (w1, expected_w1))

    set_stage("unpack")
    unpacked_dir = os.path.join(OUT, "unpacked")
    run([inputs["unpacker"], packed_kernel, unpacked_dir], "unpacker")

    set_stage("assert-unpack")
    restored_base = read_bytes(os.path.join(unpacked_dir, "BaseImage"), "BaseImage")
    check("unpack:base-full", restored_base == stock_image,
          "restored BaseImage differs from stock image")
    restored_shim = read_bytes(os.path.join(unpacked_dir, "Shim.bin"), "Shim.bin")
    check("unpack:shim", restored_shim == runtime, "restored Shim differs")

    emitted = configparser.ConfigParser()
    emitted.optionxform = str
    emitted.read(os.path.join(unpacked_dir, "config.cfg"), encoding="utf-8")

    def emitted_path(section):
        check("unpack:section:%s" % section, emitted.has_section(section),
              "missing emitted section %s" % section)
        value = emitted[section].get("Path")
        check("unpack:path:%s" % section, bool(value),
              "missing Path in emitted section %s" % section)
        if os.path.isabs(value):
            return value
        return os.path.join(unpacked_dir, value)

    if layout == "installed":
        owner_section = "Image-01"
        primary_dtb_section = "Image-03"
        diagnostic_dtb_section = "Image-04"
        blob_section = "Image-05"
    else:
        owner_section = "Image-01"
        primary_dtb_section = "Image-02"
        diagnostic_dtb_section = None
        blob_section = "Image-03"

    emitted_linux = read_bytes(emitted_path(owner_section), "emitted linux")
    check("unpack:linux", emitted_linux == padded, "emitted Linux differs")
    emitted_dtb = read_bytes(emitted_path(primary_dtb_section), "emitted dtb")
    check("unpack:dtb", emitted_dtb == derived_bytes, "emitted DTB differs")
    emitted_blob = read_bytes(emitted_path(blob_section), "emitted blob")
    check("unpack:blob", emitted_blob == initrd_bytes, "emitted Blob differs")

    if layout == "installed":
        # The alias is a shared-payload entry: it never emits its own Path, so
        # validate the PayloadFrom reference instead of requiring an emitted
        # file, and read back the diagnostic device tree.
        check("unpack:alias-section", emitted.has_section("Image-02"),
              "missing emitted section Image-02")
        alias_section = emitted["Image-02"]
        check("unpack:alias-payloadfrom",
              alias_section.get("PayloadFrom") == "Image-01",
              "emitted alias PayloadFrom=%r" % alias_section.get("PayloadFrom"))
        check("unpack:alias-no-path", not alias_section.get("Path"),
              "emitted alias unexpectedly declares Path")
        emitted_diag_dtb = read_bytes(emitted_path(diagnostic_dtb_section),
                                      "emitted diagnostic dtb")
        check("unpack:diagnostic-dtb", emitted_diag_dtb == diagnostic_bytes,
              "emitted diagnostic DTB differs")

    # Packed DTB readback: stage, bootargs, root medium (installed) and the
    # initrd entry cell, for the primary device tree and (installed only) the
    # diagnostic device tree.  Diagnostics use unique artifact/log names.
    packed_dt = os.path.join(WORK, "packed-dtb.dtb")
    with open(packed_dt, "wb") as handle:
        handle.write(payload(raw, dtb_entry))
    packed_stage = "os" if layout == "installed" else "maintenance"
    stage_val = "".join(fdtget_strings(packed_dt, "/chosen", "armada,boot-stage",
                                       "fdtget-packed-stage"))
    check("unpack:packed-stage", stage_val == packed_stage,
          "packed DTB stage=%r" % stage_val)
    ba_val = fdtget_string(packed_dt, "/chosen", "bootargs",
                           "fdtget-packed-bootargs")
    check("unpack:packed-bootargs", ba_val == bootargs, "packed bootargs=%r" % ba_val)
    entry_val = fdtget_hex(packed_dt, "/chosen", "kshim,initrd-entry",
                           "fdtget-packed-initrd-entry")
    packed_cell = blob_index if layout == "installed" else 3
    check("unpack:packed-initrd-entry", int(entry_val, 16) == packed_cell,
          "packed initrd-entry=%s" % entry_val)
    check("unpack:linux-dtref", linux["dtb_index"] == dtb_index,
          "Linux entry DT ref=%d" % linux["dtb_index"])
    if layout == "installed":
        medium_val = fdtget_string(packed_dt, "/chosen", "armada,root-medium",
                                   "fdtget-packed-root-medium")
        check("unpack:packed-root-medium", medium_val == "internal",
              "packed root-medium=%r" % medium_val)

        packed_diag = os.path.join(WORK, "packed-dtb-diagnostic.dtb")
        with open(packed_diag, "wb") as handle:
            handle.write(payload(raw, diagnostic_entry))
        diag_stage = "".join(fdtget_strings(
            packed_diag, "/chosen", "armada,boot-stage",
            "fdtget-packed-diagnostic-stage"))
        check("unpack:packed-diagnostic-stage", diag_stage == "maintenance",
              "packed diagnostic stage=%r" % diag_stage)
        diag_ba = fdtget_string(packed_diag, "/chosen", "bootargs",
                                "fdtget-packed-diagnostic-bootargs")
        check("unpack:packed-diagnostic-bootargs",
              diag_ba == diagnostic_bootargs,
              "packed diagnostic bootargs=%r" % diag_ba)
        diag_medium = fdtget_string(packed_diag, "/chosen",
                                    "armada,root-medium",
                                    "fdtget-packed-diagnostic-root-medium")
        check("unpack:packed-diagnostic-root-medium",
              diag_medium == "internal",
              "packed diagnostic root-medium=%r" % diag_medium)
        diag_cell = fdtget_hex(packed_diag, "/chosen", "kshim,initrd-entry",
                               "fdtget-packed-diagnostic-initrd-entry")
        check("unpack:packed-diagnostic-initrd-entry",
              int(diag_cell, 16) == 5,
              "packed diagnostic initrd-entry=%s" % diag_cell)
        check("unpack:alias-dtref", entries[alias_index]["dtb_index"] == 4,
              "alias entry DT ref=%d" % entries[alias_index]["dtb_index"])

    # Round-trip: repack the emitted config and require byte identity.
    repacked_value = "RepackedKernel"
    if emitted.has_section("Pack") and emitted["Pack"].get("Output"):
        repacked_value = emitted["Pack"]["Output"]
    if os.path.isabs(repacked_value):
        repacked_path = repacked_value
    else:
        repacked_path = os.path.join(unpacked_dir, repacked_value)
    run([inputs["packer"], os.path.join(unpacked_dir, "config.cfg")],
        "repacker-packer", cwd=unpacked_dir)
    repacked = read_bytes(repacked_path, "repacked kernel")
    check("repack:byte-identical", repacked == raw,
          "repacked output differs from original packed kernel")

    set_stage("gzip")
    packed_gz = os.path.join(OUT, "PackedKernel.gz")
    run([TOOL["gzip"], "-n", "-6", "-c", packed_kernel], "gzip",
        stdout_path=packed_gz)
    gz_bytes = read_bytes(packed_gz, "PackedKernel.gz")
    check("gzip:magic", gz_bytes[0:2] == GZIP_MAGIC, "bad gzip magic")
    check("gzip:mtime-zero", gz_bytes[4:8] == b"\x00\x00\x00\x00",
          "gzip mtime is not zero")
    check("gzip:roundtrip", gzip.decompress(gz_bytes) == raw,
          "decompressed bytes differ from packed kernel")

    set_stage("boot")
    final_boot = os.path.join(OUT, "boot.img")
    repack_report = os.path.join(OUT, "repack_report.json")
    run([sys.executable, "-B", inputs["repacker"],
         "--original", inputs["stock_boot"],
         "--kernel", packed_gz,
         "--output", final_boot,
         "--report", repack_report,
         "--avbtool", inputs["avbtool"]], "repacker")

    set_stage("assert-boot")
    final = read_bytes(final_boot, "boot.img")
    final_boot_header = parse_boot_header(final)
    check("boot:size", len(final) == BOOT_SIZE,
          "len=%d expected=%d" % (len(final), BOOT_SIZE))
    check("boot:magic", final[0:8] == BOOT_MAGIC, repr(final[0:8]))
    check("boot:version", final_boot_header["version"] == 4,
          "version=%d" % final_boot_header["version"])
    check("boot:ramdisk_size", final_boot_header["ramdisk_size"] == 0,
          "ramdisk_size=%d" % final_boot_header["ramdisk_size"])
    check("boot:signature_size", final_boot_header["signature_size"] == 0,
          "signature_size=%d" % final_boot_header["signature_size"])
    check("boot:header-prefix", final[0:8] == stock_boot[0:8],
          "first 8 bytes changed")
    check("boot:header-body", final[12:4096] == stock_boot[12:4096],
          "header differs beyond kernel_size field")
    final_kernel_size = final_boot_header["kernel_size"]
    check("boot:kernel_size", final_kernel_size == len(gz_bytes),
          "kernel_size=%d gzip len=%d" % (final_kernel_size, len(gz_bytes)))
    check("boot:kernel-bytes",
          final[4096:4096 + final_kernel_size] == gz_bytes,
          "embedded kernel is not the gzip input")
    check("boot:kernel-decode",
          gzip.decompress(final[4096:4096 + final_kernel_size]) == raw,
          "decoded embedded kernel differs from packed kernel")

    footer = final[-64:]
    avb_magic, avb_major, _avb_minor, ois, vbo, vbs, _reserved = struct.unpack(
        ">4sIIQQQ28s", footer)
    check("avb:footer-magic", avb_magic == AVB_MAGIC, repr(avb_magic))
    check("avb:footer-major", avb_major == 1, "%d" % avb_major)
    expected_ois = 4096 + align_up(len(gz_bytes), 4096)
    check("avb:original_image_size", ois == expected_ois,
          "ois=%d expected=%d" % (ois, expected_ois))
    check("avb:vbmeta-bounds",
          vbs > 0 and vbo >= ois and vbo + vbs <= len(final) - 64,
          "vbo=%d vbs=%d" % (vbo, vbs))
    check("boot:kernel-padding",
          final[4096 + final_kernel_size:ois]
          == b"\x00" * (ois - 4096 - final_kernel_size),
          "non-zero kernel padding")

    _sf = struct.unpack(">4sIIQQQ28s", stock_boot[-64:])
    stock_orig_size, stock_vbo, stock_vbs = _sf[3], _sf[4], _sf[5]
    check("avb:stock-vbmeta-size", stock_vbs > 0, "stock footer vbmeta_size is zero")
    check("avb:stock-vbmeta-bounds",
          stock_vbo >= stock_orig_size and stock_vbo + stock_vbs <= len(stock_boot) - 64,
          "stock footer vbmeta region out of bounds")
    check("avb:fresh-metadata",
          final[vbo:vbo + vbs] != stock_boot[stock_vbo:stock_vbo + stock_vbs],
          "vbmeta block matches stale stock value")

    set_stage("avb")
    verify_log = os.path.join(LOGS, "avbtool_verify_image.stdout")
    run([sys.executable, "-B", inputs["avbtool"], "verify_image", "--image",
         final_boot], "avbtool_verify_image")
    run([sys.executable, "-B", inputs["avbtool"], "info_image", "--image",
         final_boot], "avbtool_info_image")
    info_log = os.path.join(LOGS, "avbtool_info_image.stdout")
    with open(info_log, encoding="utf-8", errors="replace") as handle:
        info_text = handle.read()
    algorithm_match = re.search(r"^\s*Algorithm:\s*(\S+)", info_text, re.M)
    check("avb:algorithm-line", algorithm_match is not None,
          "no anchored Algorithm line in info_image output")
    check("avb:algorithm-none", algorithm_match.group(1) == "NONE",
          "algorithm=%s" % algorithm_match.group(1))

    set_stage("recheck-sources")
    for key, path in inputs.items():
        check("source-unchanged:%s" % key,
              sha256_file(path) == source_digests[key], "input changed: %s" % path)

    set_stage("report")
    report_entries = []
    for index, name, e_type, e_flags, e_align, e_dtb in expected_entries:
        entry = entries[index]
        report_entries.append({
            "index": index,
            "name": name,
            "type": e_type,
            "flags": entry["flags"],
            "offset": entry["offset"],
            "size": entry["size"],
            "entry_offset": entry["entry_offset"],
            "dtb_index": entry["dtb_index"],
            "alignment": entry["alignment"],
            "sha256": sha256_bytes(payload(raw, entry)),
        })

    if layout == "installed":
        caveats = [
            "Offline byte-level round-trip plus official avbtool success is "
            "NOT device or hardware validation.",
            "The supplied .259 stock image and the current accepted 4 MiB runtime "
            "are trusted as given; input hashes attest identity only and do not "
            "attest real-device boot behaviour.",
            "Installed layout: the internal owner device tree (Image-03) carries "
            "/chosen armada,boot-stage=os and armada,root-medium=internal; the "
            "diagnostic alias device tree (Image-04) carries "
            "armada,boot-stage=maintenance with bootargs equal to the selected "
            "BLS options plus ' rd.break=pre-mount'.  Neither establishes USB "
            "shell or device support, the diagnostic entry cannot be selected "
            "as the installed default and no automatic reboot happens.  Both "
            "device trees are written into the packed image and are NOT flashed "
            "anywhere by this script.",
            "No UFS / partition operation, no flashing, no reboot and no "
            "persistence change happened; no real device ran this image.",
            "hardware_tested is false: this run is offline verification only.",
        ]
    else:
        caveats = [
            "Offline byte-level round-trip plus official avbtool success is "
            "NOT device or hardware validation.",
            "The supplied .259 stock image and the current accepted 4 MiB runtime "
            "are trusted as given; input hashes attest identity only and do not "
            "attest real-device boot behaviour.",
            "The diagnostic status is carried by the derived DTB's "
            "/chosen armada,boot-stage=maintenance; the derived DTB is written "
            "into the packed image and is NOT flashed anywhere by this script.",
            "No UFS / partition operation, no flashing, no reboot and no "
            "persistence change happened; no real device ran this image.",
            "hardware_tested is false: this run is offline verification only.",
        ]

    report = {
        "offline_verified": True,
        "offline_only": True,
        "hardware_tested": False,
        "source_label": source_label,
        "layout_mode": layout,
        "bootargs": bootargs,
        "inputs": {
            key: {"path": inputs[key], "length": source_sizes[key],
                  "sha256": source_digests[key]}
            for key in sorted(inputs)
        },
        "tools": dict(TOOL),
        "stock_image": {
            "length": len(stock_image),
            "sha256": sha256_bytes(stock_image),
            "text_offset": stock_info["text_offset"],
            "image_size": stock_info["image_size"],
            "effective_slot": stock_info["effective_slot"],
        },
        "linux_image": {
            "length": len(linux_image),
            "sha256": sha256_bytes(linux_image),
            "image_size": linux_info["image_size"],
            "padded_length": len(padded),
            "bss_pad": bss_pad,
            "reserved_span": linux_reserved,
        },
        "dtb": {
            "source_sha256": source_digests["dtb"],
            "derived_sha256": sha256_bytes(derived_bytes),
            "derived_length": len(derived_bytes),
            "changed_properties": [{"node": p, "property": k} for p, k in dtb_changed],
            "source_dts": dts_paths["source_dts"],
            "derived_dts": dts_paths["derived_dts"],
        },
        "runtime": {
            "length": len(runtime),
            "sha256": source_digests["runtime"],
        },
        "config_path": config_path,
        "default_entry": default_entry,
        "default_label": default_label,
        "default_index": default_index,
        "timeout_ms": TIMEOUT_MS,
        "loader_budget": raw_budget,
        "bl_range_bytes": BL_RANGE,
        "manifest": {
            "magic": hex(manifest["magic"]),
            "version": "%d.%d" % (manifest["version_major"],
                                  manifest["version_minor"]),
            "count": manifest["count"],
            "offset": manifest_offset,
            "size": manifest_size,
            "backup": list(manifest["backup"]),
        },
        "entries": report_entries,
        "layout": {
            "total_raw_length": len(raw),
            "bss_pad": bss_pad,
            "regions": [
                {"name": name, "start": start, "end": end, "used": used}
                for name, start, end, used in regions
            ],
            "linux_range": [linux["offset"], linux["offset"] + linux["size"]],
            "blob_range": [blob_entry["offset"],
                           blob_entry["offset"] + blob_entry["size"]],
            "manifest": [manifest_offset, manifest_offset + manifest_size],
        },
        "outputs": {
            "packed_kernel": {"path": packed_kernel, "length": len(raw),
                              "sha256": sha256_bytes(raw)},
            "packed_kernel_gz": {"path": packed_gz, "length": len(gz_bytes),
                                 "sha256": sha256_bytes(gz_bytes)},
            "unpacked_dir": unpacked_dir,
            "final_boot": {"path": final_boot, "length": len(final),
                           "sha256": sha256_bytes(final)},
            "repack_report": repack_report,
        },
        "avb": {
            "algorithm": algorithm_match.group(1),
            "original_image_size": ois,
            "vbmeta_offset": vbo,
            "vbmeta_size": vbs,
            "verify_image_log": verify_log,
            "info_image_log": info_log,
        },
        "caveats": caveats,
        "commands": COMMANDS,
        "checks": CHECKS,
    }

    if layout == "installed":
        report["diagnostic_dtb"] = {
            "source_sha256": source_digests["dtb"],
            "derived_sha256": sha256_bytes(diagnostic_bytes),
            "derived_length": len(diagnostic_bytes),
            "changed_properties": [{"node": p, "property": k}
                                   for p, k in diagnostic_changed],
            "source_dts": diagnostic_dts["source_dts"],
            "derived_dts": diagnostic_dts["derived_dts"],
        }
        report["diagnostic_bootargs"] = diagnostic_bootargs
        report["installed_metadata"] = installed_metadata

    with open(os.path.join(OUT, "report.json"), "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    sys.stdout.write("offline verification PASSED; report at %s\n"
                     % os.path.join(OUT, "report.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
