import json
import subprocess

UPDATE = "/usr/libexec/armada/armada-update"
SELECT = "/usr/libexec/armada/armada-select-branch"
CHANNELS = ("stable", "beta", "preview")
FREEDOM = "ghcr.io/mydanyi/armadaos-freedom"
REPOSITORIES = (FREEDOM,)
# Transport prefixes bootc may report on a staged image reference.
TRANSPORTS = (
    "ostree-image-signed:docker://",
    "ostree-unverified-image:docker://",
    "ostree-unverified-registry:docker://",
    "ostree-unverified-registry:",
    "docker://",
)


def command(arguments, timeout=15):
    try:
        return subprocess.run(arguments, check=True, capture_output=True, text=True, timeout=timeout).stdout
    except subprocess.CalledProcessError as error:
        raise RuntimeError((error.stderr or "").strip() or str(error)) from error


def ref_repository_tag(image):
    """Return (repository, tag) for a supported image ref, else (None, None).

    Raw references carry no transport prefix, which is how bootc reports staged
    images. An unknown scheme, a missing tag, or a malformed repository never
    matches, so a prefix lookalike cannot be mistaken for the Freedom channel.
    """
    if not image or any(character.isspace() for character in image):
        return None, None
    for prefix in TRANSPORTS:
        if image.startswith(prefix):
            image = image[len(prefix):]
            break
    else:
        if "://" in image:
            return None, None
    image = image.split("@", 1)[0]
    repository, separator, tag = image.rpartition(":")
    if not separator or "/" in tag or "/" not in repository or ":" in repository:
        return None, None
    return repository, tag


def staged_matches_channel(staged, channel):
    if not staged:
        return False
    repository, tag = ref_repository_tag(staged.get("image", ""))
    if repository != FREEDOM:
        return False
    return tag == ("testing" if channel == "preview" else channel)


def deployment(value):
    if not value:
        return None
    image = value.get("image") or {}
    ostree = value.get("ostree") or {}
    return {"version": image.get("version") or image.get("imageDigest") or ostree.get("checksum") or "Unknown version",
            "image": (image.get("image") or {}).get("image", ""),
            "checksum": ostree.get("checksum"), "serial": ostree.get("deploySerial"),
            "stateroot": ostree.get("stateroot"), "incompatible": bool(value.get("incompatible"))}


def snapshot():
    raw = json.loads(command(["/usr/bin/bootc", "status", "--format", "json"]))
    status = raw.get("status") or {}
    booted = deployment(status.get("booted"))
    if not booted or not booted["checksum"]:
        raise RuntimeError("No booted OSTree deployment is available")
    try:
        target = command(["/bin/bash", "-c", 'source /usr/lib/armada/update-lib; armada_update_target "$(armada_booted_ref)"'], timeout=60).strip().split()
    except (subprocess.SubprocessError, RuntimeError):
        target = []
    repository, channel = target if len(target) == 2 else ("", "")
    if channel == "testing":
        channel = "preview"
    state = {"booted": booted, "rollback": deployment(status.get("rollback")),
             "staged": deployment(status.get("staged")), "rollback_queued": bool(status.get("rollbackQueued")),
             "channel": channel, "repository": repository,
             "channels": list(CHANNELS) if repository in REPOSITORIES and channel in CHANNELS else []}
    return state


def status():
    state = snapshot()
    # A reboot is ready for an explicit rollback, or when the staged image is
    # actually the Freedom channel this deployment now targets.
    state["reboot_ready"] = bool(
        state["rollback_queued"] or staged_matches_channel(state["staged"], state["channel"]))
    return state


def select_channel(channel):
    if channel not in CHANNELS:
        raise ValueError("Unknown update channel")
    if channel not in snapshot()["channels"]:
        raise RuntimeError("Channel selection is unavailable for this image")
    command([SELECT, channel])
    return {"ok": True}


def restart():
    if not status()["reboot_ready"]:
        raise RuntimeError("There is no pending OS change to restart into")
    command(["/usr/bin/systemctl", "reboot"])
    return {"ok": True}


def execute(operation, checksum=None):
    state = snapshot()
    if operation == "update":
        if not state["repository"] or not state["channel"]:
            raise RuntimeError("This deployment has no supported update target")
        subprocess.run([UPDATE], check=True)
        current = snapshot()
        staged = current["staged"]
        if not staged:
            return {"message": "No new image was needed."}
        if not staged_matches_channel(staged, current["channel"]):
            raise RuntimeError(
                f"An unsupported pending update is staged from {staged['image']}. "
                "Refresh and confirm the update again.")
        return {"message": "Update installed. Restart to apply."}
    if operation != "rollback":
        raise ValueError("Unknown OS operation")
    target = state["rollback"]
    if not target or not target["checksum"] or target["incompatible"]:
        raise RuntimeError("No compatible previous deployment is available")
    if not checksum or checksum != target["checksum"]:
        raise RuntimeError("The rollback target changed. Refresh and confirm rollback again.")
    if not state["rollback_queued"]:
        print("Selecting the previous OS deployment", flush=True)
        subprocess.run(["/usr/bin/bootc", "rollback"], check=True)
    selected = snapshot()
    if not selected["rollback_queued"] or not selected["rollback"] or selected["rollback"]["checksum"] != checksum:
        raise RuntimeError("The expected rollback deployment was not selected")
    return {"message": "Rollback ready. Restart to apply."}
