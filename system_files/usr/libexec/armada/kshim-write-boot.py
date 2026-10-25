#!/usr/bin/env python3
"""Write one prepared kShimC boot image to the boot_a partition, verified.

This is a raw writer, not an installer. The caller must supply the candidate
image plus a separately prepared fallback image (built from the current native
BLS), and both must carry their own offline-verification reports. Report paths
are historical evidence only: every byte read or written here comes from the
explicit CLI paths.

The write is NOT atomic. The target partition is overwritten in place; a crash,
power loss, or hardware fault in the middle can leave a mixture of old, new,
and fallback bytes. Only a clean run that ends with readback verification says
that this run wrote the candidate; it says nothing about the image booting, and
this tool never claims a known-good state. Caller obligations:

  * keep the native BLS ordering untouched until the new image has been proven;
  * retain before.img and transaction.json after any failure;
  * treat exit 2/3 and any interrupted run as requiring explicit recovery;
  * never run this against a mounted or otherwise in-use partition.

The tool only performs preflight validation, full-partition hashing, an
exclusive backup, the bounded write, cache flush, and readback verification.
It does not mount, format, reboot, call bootc/ostree, touch boot_b or misc,
rewrite native boot entries, or use the network.
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import struct
import subprocess
import sys
import uuid

BOOT_BYTES = 100663296
MAGIC = b"ANDROID!"
CHUNK = 1 << 20
SPACE_MARGIN = 1 << 20
BLKGETSIZE64 = 0x80081272
BLKFLSBUF = 0x1261
TARGET_DEFAULT = "/dev/disk/by-partlabel/boot_a"
PART_NAME = "boot_a"
PART_SCHEME = "gpt"
HEX64 = re.compile(r"[0-9a-f]{64}\Z")

class WriteError(Exception):
    pass


def need(condition, message):
    if not condition:
        raise WriteError(message)


def checksum_text(value, label):
    need(isinstance(value, str) and HEX64.fullmatch(value) is not None,
         "%s must be 64 lowercase hex characters: %r" % (label, value))
    return value


def canonical_uuid(value, label="UUID"):
    need(isinstance(value, str) and value != "", "%s must be a UUID: %r" % (label, value))
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise WriteError("%s must be a UUID: %r" % (label, value))


def require_root():
    need(os.geteuid() == 0, "must run as root")


def read_json(path, label):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError) as error:
        raise WriteError("%s report unreadable: %s: %s" % (label, path, error))
    need(isinstance(document, dict), "%s report is not a JSON object" % label)
    return document


def hash_fd(fd, length):
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    remaining = length
    while remaining:
        chunk = os.read(fd, min(CHUNK, remaining))
        if not chunk:
            raise WriteError("short read with %d bytes left" % remaining)
        digest.update(chunk)
        remaining -= len(chunk)
    return digest.hexdigest()


def write_all(fd, data):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise WriteError("short write to fd %d" % fd)
        view = view[written:]


def write_image(fd, image_fd, length):
    """Copy exactly length bytes from image_fd to fd with bounded chunks."""
    remaining = length
    os.lseek(image_fd, 0, os.SEEK_SET)
    os.lseek(fd, 0, os.SEEK_SET)
    while remaining:
        chunk = os.read(image_fd, min(CHUNK, remaining))
        if not chunk:
            raise WriteError("image ended with %d bytes left to write" % remaining)
        write_all(fd, chunk)
        remaining -= len(chunk)


def flush_block_cache(fd):
    fcntl.ioctl(fd, BLKFLSBUF)


def verify_written(fd, expected):
    actual = hash_fd(fd, BOOT_BYTES)
    need(actual == expected,
         "readback hash %s does not match expected %s" % (actual, expected))


def install(fd, image_fd, expected):
    write_image(fd, image_fd, BOOT_BYTES)
    os.fsync(fd)
    flush_block_cache(fd)
    verify_written(fd, expected)


def block_size(fd):
    return struct.unpack("Q", fcntl.ioctl(fd, BLKGETSIZE64, struct.pack("Q", 0)))[0]


def export_fields(text):
    fields = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        fields[key.strip()] = value.strip()
    return fields


def blkid_fields(target):
    try:
        proc = subprocess.run(["blkid", "-p", "-o", "export", target],
                              capture_output=True, text=True, check=False)
    except OSError as error:
        raise WriteError("blkid could not run: %s" % error)
    need(proc.returncode == 0,
         "blkid -p -o export %s failed (%d): %s"
         % (target, proc.returncode, proc.stderr.strip()))
    return export_fields(proc.stdout)


def parse_partition_fields(fields, partuuid):
    """Check only the three GPT partition-entry fields this tool relies on."""
    need(fields.get("PART_ENTRY_SCHEME") == PART_SCHEME,
         "target partition scheme is not %s" % PART_SCHEME)
    name = fields.get("PART_ENTRY_NAME")
    need(name == PART_NAME, "target partition is %r, not %s" % (name, PART_NAME))
    on_disk = fields.get("PART_ENTRY_UUID")
    need(on_disk is not None, "blkid output has no PART_ENTRY_UUID")
    need(canonical_uuid(on_disk, "target partition UUID") == partuuid,
         "target partition UUID %s does not match --partuuid %s" % (on_disk, partuuid))


def probe_target(target, partuuid):
    """Validate a block-device alias and return its concrete identity."""
    real = os.path.realpath(target)
    try:
        info = os.stat(real)
    except OSError as error:
        raise WriteError("target missing: %s: %s" % (target, error))
    need(stat.S_ISBLK(info.st_mode), "target is not a block device: %s" % real)
    parse_partition_fields(blkid_fields(real), partuuid)
    fd = os.open(real, os.O_RDONLY | os.O_CLOEXEC)
    try:
        need(os.fstat(fd).st_rdev == info.st_rdev,
             "target changed identity while being inspected: %s" % real)
        size = block_size(fd)
    finally:
        os.close(fd)
    need(size == BOOT_BYTES,
         "target size is %d bytes, expected %d" % (size, BOOT_BYTES))
    return {"path": real, "partuuid": partuuid, "rdev": info.st_rdev,
            "major": os.major(info.st_rdev), "minor": os.minor(info.st_rdev),
            "size": size}


def verify_locked(fd, identity):
    info = os.fstat(fd)
    need(stat.S_ISBLK(info.st_mode), "reopened target is not a block device")
    need(info.st_rdev == identity["rdev"], "reopened target is a different device")
    need(block_size(fd) == BOOT_BYTES, "reopened target has the wrong size")


def open_target_ro(identity):
    fd = os.open(identity["path"], os.O_RDONLY | os.O_CLOEXEC)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise WriteError("target is busy: %s" % error)
        verify_locked(fd, identity)
    except Exception:
        os.close(fd)
        raise
    return fd


def open_target_rw(identity):
    """Exclusive, locked read-write open; a mounted partition fails here."""
    try:
        fd = os.open(identity["path"], os.O_RDWR | os.O_EXCL | os.O_CLOEXEC)
    except OSError as error:
        raise WriteError("cannot open target exclusively: %s" % error)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise WriteError("target is busy: %s" % error)
        verify_locked(fd, identity)
    except Exception:
        os.close(fd)
        raise
    return fd


def open_image(path, label):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    except OSError as error:
        raise WriteError("%s image unreadable: %s: %s" % (label, path, error))
    try:
        info = os.fstat(fd)
        need(stat.S_ISREG(info.st_mode), "%s image is not a regular file" % label)
        need(info.st_size == BOOT_BYTES,
             "%s image is %d bytes, expected %d" % (label, info.st_size, BOOT_BYTES))
        os.lseek(fd, 0, os.SEEK_SET)
        need(os.read(fd, len(MAGIC)) == MAGIC, "%s image has no ANDROID! magic" % label)
        digest = hash_fd(fd, BOOT_BYTES)
        os.lseek(fd, 0, os.SEEK_SET)
    except Exception:
        os.close(fd)
        raise
    return fd, digest


def validate_report(report, image_sha, label):
    need(report.get("layout_mode") == "installed",
         "%s report layout_mode is not installed" % label)
    need(report.get("offline_verified") is True,
         "%s report is not offline_verified" % label)
    checks = report.get("checks")
    need(isinstance(checks, list) and checks != [],
         "%s report has no checks" % label)
    for index, check in enumerate(checks):
        need(isinstance(check, dict), "%s report check %d is not an object" % (label, index))
        need(check.get("ok") is True, "%s report check %d failed" % (label, index))

    outputs = report.get("outputs")
    need(isinstance(outputs, dict), "%s report has no outputs" % label)
    final = outputs.get("final_boot")
    need(isinstance(final, dict), "%s report has no final_boot output" % label)
    length = final.get("length")
    need(type(length) is int and length == BOOT_BYTES,
         "%s report final_boot length is not %d" % (label, BOOT_BYTES))
    need(checksum_text(final.get("sha256"), "%s final_boot sha256" % label) == image_sha,
         "%s report final_boot sha256 does not match the image" % label)

    metadata = report.get("installed_metadata")
    need(isinstance(metadata, dict), "%s report has no installed_metadata" % label)
    checksum_text(metadata.get("deployment_checksum"),
                  "%s deployment_checksum" % label)
    uuids = metadata.get("filesystem_uuids")
    need(isinstance(uuids, dict), "%s report has no filesystem_uuids" % label)
    for key in ("root", "boot"):
        canonical_uuid(uuids.get(key), "%s filesystem_uuids.%s" % (label, key))
    options = metadata.get("options")
    need(isinstance(options, str) and options.strip() != "",
         "%s report has no options" % label)


def installation_key(report):
    metadata = report["installed_metadata"]
    inputs = report.get("inputs")
    need(isinstance(inputs, dict), "report has no inputs")
    stock = inputs.get("stock_boot")
    need(isinstance(stock, dict), "report has no inputs.stock_boot")
    uuids = metadata["filesystem_uuids"]
    return {
        "stock_boot": checksum_text(stock.get("sha256"), "inputs.stock_boot.sha256"),
        "root": canonical_uuid(uuids["root"]),
        "boot": canonical_uuid(uuids["boot"]),
    }


def prepare_transaction_dir(path):
    if os.path.exists(path):
        need(os.path.isdir(path), "transaction dir is not a directory: %s" % path)
        need(os.listdir(path) == [], "transaction dir is not empty: %s" % path)
    else:
        os.makedirs(path, mode=0o700)
    need(os.path.isdir(path), "transaction dir unusable: %s" % path)


def write_transaction(path, record):
    final = os.path.join(path, "transaction.json")
    temp = os.path.join(path, ".transaction.json.tmp")
    payload = json.dumps(record, indent=2, sort_keys=True) + "\n"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o600)
    try:
        write_all(fd, payload.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temp, final)
    dir_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def verify_backup_file(path, expected):
    """Re-open the saved backup read-only and verify its real size and hash."""
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        size = os.fstat(fd).st_size
        need(size == BOOT_BYTES,
             "before.img is %d bytes, expected %d" % (size, BOOT_BYTES))
        saved = hash_fd(fd, BOOT_BYTES)
    finally:
        os.close(fd)
    need(saved == expected,
         "saved before.img hash %s does not match current target %s" % (saved, expected))
    return saved


def backup_target(path, fd, current):
    need(os.statvfs(path).f_bavail * os.statvfs(path).f_frsize
         >= BOOT_BYTES + SPACE_MARGIN,
         "not enough space for before.img in %s" % path)
    backup = os.path.join(path, "before.img")
    out = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    digest = hashlib.sha256()
    try:
        remaining = BOOT_BYTES
        os.lseek(fd, 0, os.SEEK_SET)
        while remaining:
            chunk = os.read(fd, min(CHUNK, remaining))
            if not chunk:
                raise WriteError("short read while backing up with %d bytes left" % remaining)
            write_all(out, chunk)
            digest.update(chunk)
            remaining -= len(chunk)
        os.fsync(out)
    finally:
        os.close(out)
    stream = digest.hexdigest()
    need(stream == current,
         "before.img hash %s does not match current target %s" % (stream, current))
    saved = verify_backup_file(backup, current)
    return {"path": backup, "length": BOOT_BYTES, "sha256": saved}


def base_record(identity, expected, current, candidate, fallback):
    return {
        "tool": "kshim-write-boot",
        "target": {
            "path": identity["path"],
            "partuuid": identity["partuuid"],
            "device": "%d:%d" % (identity["major"], identity["minor"]),
            "length": identity["size"],
        },
        "expected_current_sha256": expected,
        "current_sha256": current,
        "candidate_sha256": candidate,
        "fallback_sha256": fallback,
        "backup": None,
        "status": None,
        "error": None,
        "recovery_error": None,
    }


def record_best_effort(path, record, label):
    """Write a status record without masking the physical outcome."""
    try:
        write_transaction(path, record)
    except (Exception, KeyboardInterrupt) as failure:
        sys.stderr.write("kshim-write-boot: could not record %s status: %s\n"
                         % (label, failure))


def restore_fallback(path, record, target_fd, fallback_fd, fallback_sha, error):
    """Restore the explicit fallback after a failed candidate write phase."""
    record["error"] = str(error)
    try:
        install(target_fd, fallback_fd, fallback_sha)
    except (Exception, KeyboardInterrupt) as recovery:
        record["status"] = "recovery-failed"
        record["recovery_error"] = str(recovery)
        record_best_effort(path, record, "recovery-failed")
        sys.stderr.write(
            "kshim-write-boot: fallback recovery failed: %s (original error: %s)\n"
            % (recovery, error))
        return 3
    record["status"] = "fallback-restored"
    record_best_effort(path, record, "fallback-restored")
    sys.stderr.write(
        "kshim-write-boot: write failed and the fallback was restored: %s\n" % error)
    return 2


def run(args):
    require_root()
    partuuid = canonical_uuid(args.partuuid, "--partuuid")
    expected = checksum_text(args.expected_current_sha256, "--expected-current-sha256")

    candidate_fd = fallback_fd = target_fd = None
    try:
        candidate_fd, candidate_sha = open_image(args.image, "candidate")
        fallback_fd, fallback_sha = open_image(args.fallback_image, "fallback")
        candidate_report = read_json(args.report, "candidate")
        fallback_report = read_json(args.fallback_report, "fallback")
        validate_report(candidate_report, candidate_sha, "candidate")
        validate_report(fallback_report, fallback_sha, "fallback")
        need(installation_key(candidate_report) == installation_key(fallback_report),
             "candidate and fallback reports describe different installations")

        identity = probe_target(args.target, partuuid)
        prepare_transaction_dir(args.transaction_dir)

        reader = open_target_ro(identity)
        try:
            current = hash_fd(reader, BOOT_BYTES)
        finally:
            os.close(reader)

        record = base_record(identity, expected, current, candidate_sha, fallback_sha)
        need(current == expected,
             "current target hash %s does not match --expected-current-sha256 %s"
             % (current, expected))
        if current == candidate_sha:
            record["status"] = "unchanged"
            write_transaction(args.transaction_dir, record)
            print("kshim-write-boot: target already contains the candidate; unchanged")
            return 0

        target_fd = open_target_rw(identity)
        try:
            record["backup"] = backup_target(args.transaction_dir, target_fd, current)
        except (Exception, KeyboardInterrupt) as error:
            record["status"] = "backup-failed"
            record["error"] = str(error)
            write_transaction(args.transaction_dir, record)
            raise

        try:
            record["status"] = "prepared"
            write_transaction(args.transaction_dir, record)
        except (Exception, KeyboardInterrupt) as error:
            sys.stderr.write(
                "kshim-write-boot: prepared transaction record could not be persisted: %s\n"
                % error)
            raise

        try:
            install(target_fd, candidate_fd, candidate_sha)
            record["status"] = "written"
            write_transaction(args.transaction_dir, record)
        except (Exception, KeyboardInterrupt) as error:
            return restore_fallback(args.transaction_dir, record, target_fd,
                                    fallback_fd, fallback_sha, error)

        print("kshim-write-boot: wrote candidate %s and verified readback" % candidate_sha)
        return 0
    finally:
        if target_fd is not None:
            os.close(target_fd)
        if fallback_fd is not None:
            os.close(fallback_fd)
        if candidate_fd is not None:
            os.close(candidate_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Verified raw writer for one prepared kShimC boot_a image. "
                    "The write is not atomic; the caller must keep the separately "
                    "prepared fallback and must not change native boot ordering.")
    parser.add_argument("--image", required=True,
                        help="prepared candidate image, exactly %d bytes" % BOOT_BYTES)
    parser.add_argument("--report", required=True, help="candidate offline report")
    parser.add_argument("--fallback-image", required=True,
                        help="prepared fallback image, exactly %d bytes" % BOOT_BYTES)
    parser.add_argument("--fallback-report", required=True, help="fallback offline report")
    parser.add_argument("--target", default=TARGET_DEFAULT,
                        help="boot_a block device or alias (default: %(default)s)")
    parser.add_argument("--partuuid", required=True, help="expected GPT partition UUID")
    parser.add_argument("--expected-current-sha256", required=True,
                        help="hash of the full partition before any write")
    parser.add_argument("--transaction-dir", required=True,
                        help="absent or empty persistent directory for before.img "
                             "and transaction.json")
    args = parser.parse_args(argv)
    try:
        return run(args)
    except WriteError as error:
        sys.stderr.write("kshim-write-boot: %s\n" % error)
        return 1
    except OSError as error:
        sys.stderr.write("kshim-write-boot: %s\n" % error)
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("kshim-write-boot: interrupted\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
