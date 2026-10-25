#!/usr/bin/env python3
"""Export one native BLS entry's boot metadata as the packer installer JSON.

Read-only and explicit-entry: reads exactly the BLS file named by --entry,
follows its ostree= symlink to the real deployment under the sysroot, and
writes the existing installer-result schema. The deployment checksum comes
from the resolved deployment directory name, never from a boot.0/boot.1
boot checksum. No mutation, no subprocess, no block/device access.
"""
import argparse
import json
import os
import re
import sys

DTB_REL = "qcom/sm8750-lenovo-elden.dtb"
DEPLOYMENT_RE = re.compile(r"([0-9a-f]{64})\.([0-9]+)")


class ExportError(Exception):
    pass


def need(ok, message):
    if not ok:
        raise ExportError(message)


def inside(parent, child):
    try:
        return os.path.commonpath([parent, child]) == parent
    except ValueError:
        return False


def split_field(raw):
    body = raw.lstrip(" \t")
    if not body or body.startswith("#"):
        return None
    end = 0
    while end < len(body) and body[end] not in " \t":
        end += 1
    rest = body[end:]
    if rest:
        rest = rest.lstrip(" \t")
    return body[:end], rest


def parse_fields(path):
    fields = {"linux": [], "initrd": [], "fdtdir": [], "options": []}
    with open(path, "r", encoding="utf-8") as handle:
        for raw in handle.read().splitlines():
            parsed = split_field(raw)
            if parsed is None:
                continue
            key, value = parsed
            if key in fields:
                fields[key].append(value)
    return fields


def resolve_entry(boot, raw):
    """Resolve --entry (absolute or BOOT-relative) to (BLS-relative name, real file)."""
    need(raw != "", "--entry must not be empty")
    if raw.startswith("/boot/"):
        raw = raw[len("/boot/"):]
    if os.path.isabs(raw):
        path = os.path.normpath(raw)
    else:
        path = os.path.normpath(os.path.join(boot, raw))
        need(inside(boot, path), "BLS entry outside boot dir: %r" % raw)
    real = os.path.realpath(path)
    need(inside(boot, real), "BLS entry resolves outside boot dir: %r" % raw)
    need(os.path.isfile(real), "BLS entry missing: %s" % path)
    # Keep the caller's BLS-relative name when it is inside BOOT; a boot dir
    # reached through a symlink may only be identifiable by its real path.
    rel = os.path.relpath(path, boot) if inside(boot, path) else os.path.relpath(real, boot)
    return rel.replace(os.sep, "/"), real


def deployment_from_link(link, sysroot):
    """Resolve the single BLS ostree= value to its real deployment directory.

    The boot.0/boot.1 component is a boot checksum and serial, not a
    deployment commit: only the symlink target is authoritative.
    """
    need(os.path.isabs(link), "ostree= must be absolute: %r" % link)
    real = os.path.realpath(os.path.join(sysroot, link.lstrip("/")))
    need(inside(sysroot, real), "ostree= escapes sysroot: %r -> %s" % (link, real))
    rel = os.path.relpath(real, sysroot)
    parts = rel.split(os.sep)
    need(len(parts) == 5 and parts[0] == "ostree" and parts[1] == "deploy"
         and parts[3] == "deploy",
         "ostree= does not resolve to ostree/deploy/<stateroot>/deploy/<name>: %r -> %s"
         % (link, real))
    match = DEPLOYMENT_RE.fullmatch(parts[4])
    need(match is not None,
         "deployment name is not <64 lowercase hex>.<numeric serial>: %r" % parts[4])
    deploy_dir = os.path.realpath(
        os.path.join(sysroot, "ostree", "deploy", parts[2], "deploy"))
    need(os.path.dirname(real) == deploy_dir,
         "deployment is not in the native deploy directory: %s" % real)
    need(os.path.isdir(real), "deployment not installed: %s" % real)
    return real, match.group(1)


def boot_relative(value, label):
    text = value[len("/boot/"):] if value.startswith("/boot/") else value
    need(text != "" and not os.path.isabs(text),
         "%s must be BOOT-relative: %r" % (label, value))
    need(".." not in text.split("/"),
         "%s must not contain '..': %r" % (label, value))
    return text


def boot_asset(boot, value, label):
    """Normalize an asset path and ensure its real target stays inside BOOT."""
    rel = boot_relative(value, label)
    real = os.path.realpath(os.path.join(boot, rel))
    need(inside(boot, real), "%s resolves outside boot dir: %r" % (label, value))
    return rel, real


def export(args):
    boot = os.path.realpath(args.boot_dir)
    sysroot = os.path.realpath(args.sysroot)
    need(os.path.isdir(boot), "boot dir missing: %s" % boot)
    need(os.path.isdir(sysroot), "sysroot missing: %s" % sysroot)

    bls, conf_real = resolve_entry(boot, args.entry)
    fields = parse_fields(conf_real)
    for key in ("linux", "initrd", "fdtdir", "options"):
        need(len(fields[key]) == 1, "expected exactly one %r line in %s, found %d"
             % (key, conf_real, len(fields[key])))

    options = fields["options"][0]
    image, image_real = boot_asset(boot, fields["linux"][0], "linux")
    initrd, initrd_real = boot_asset(boot, fields["initrd"][0], "initrd")
    fdtdir, fdtdir_real = boot_asset(boot, fields["fdtdir"][0], "fdtdir")
    need(os.path.isfile(image_real), "image missing: %s" % image)
    need(os.path.isfile(initrd_real), "initrd missing: %s" % initrd)
    need(os.path.isdir(fdtdir_real), "fdtdir missing: %s" % fdtdir)
    dtb = os.path.realpath(os.path.join(fdtdir_real, DTB_REL))
    need(inside(boot, dtb) and os.path.isfile(dtb),
         "Elden DT missing or outside boot: %s/%s" % (fdtdir, DTB_REL))

    tokens = options.split()
    ostree = [t[len("ostree="):] for t in tokens if t.startswith("ostree=")]
    need(len(ostree) == 1, "expected exactly one ostree= in %s, found %d"
         % (conf_real, len(ostree)))
    deploy_abs, commit = deployment_from_link(ostree[0], sysroot)

    def uuid_arg(prefix):
        values = [t[len(prefix):] for t in tokens if t.startswith(prefix)]
        need(len(values) == 1, "expected exactly one %s, found %d in %s"
             % (prefix, len(values), conf_real))
        need(values[0].startswith("UUID=") and len(values[0]) > len("UUID="),
             "%s must be UUID=<uuid>: %r" % (prefix, values[0]))
        return values[0][len("UUID="):]

    result = {
        "deployment_checksum": commit,
        "filesystem_uuids": {"root": uuid_arg("root="), "boot": uuid_arg("boot=")},
        "bls": bls,
        "image": image, "initrd": initrd, "fdtdir": fdtdir,
        "options": options,
    }
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    print("selected bls: %s" % result["bls"])
    print("deployment: %s" % deploy_abs)


def main():
    parser = argparse.ArgumentParser(
        description="Export one native BLS entry's boot metadata for kShimC packaging")
    parser.add_argument("--boot-dir", required=True)
    parser.add_argument("--sysroot", required=True)
    parser.add_argument("--entry", required=True,
                        help="actual BLS .conf path (absolute or BOOT-relative)")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        export(args)
    except (ExportError, OSError) as error:
        sys.stderr.write("kshim-select-boot: %s\n" % error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
