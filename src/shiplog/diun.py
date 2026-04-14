"""Parse diun script notifier environment variables."""

import os
from dataclasses import dataclass


@dataclass
class DiunEvent:
    """A container update event from diun's script notifier."""
    status: str       # "new" or "update"
    image: str        # e.g. "docker.io/crazymax/diun:v4.31.0"
    hub_link: str     # e.g. "https://hub.docker.com/r/crazymax/diun"
    digest: str       # sha256:...
    created: str      # image creation timestamp
    platform: str     # e.g. "linux/amd64"
    provider: str     # e.g. "docker", "file"

    @property
    def image_name(self) -> str:
        """Image without tag — e.g. 'docker.io/crazymax/diun'."""
        name, _ = split_image_ref(self.image)
        return name

    @property
    def tag(self) -> str:
        """Tag portion — e.g. 'v4.31.0'. Defaults to 'latest'."""
        _, tag = split_image_ref(self.image)
        return tag


def split_image_ref(ref: str) -> tuple[str, str]:
    """Split an image reference into (image, tag).

    Handles port numbers: 'registry.local:5000/app:v1' → ('registry.local:5000/app', 'v1')
    Without tag: 'registry.local:5000/app' → ('registry.local:5000/app', 'latest')
    """
    if "/" in ref:
        last_slash = ref.rfind("/")
        after_slash = ref[last_slash + 1:]
        if ":" in after_slash:
            colon_pos = last_slash + 1 + after_slash.rfind(":")
            return ref[:colon_pos], ref[colon_pos + 1:]
        return ref, "latest"

    # No slash — simple name:tag or just name
    if ":" in ref:
        parts = ref.rsplit(":", 1)
        return parts[0], parts[1]
    return ref, "latest"


class DiunParseError(Exception):
    """Raised when required diun env vars are missing."""


def parse_env(environ: dict[str, str] | None = None) -> DiunEvent:
    """Parse DIUN_* environment variables into a DiunEvent.

    Args:
        environ: Dict to read from. Defaults to os.environ.

    Raises:
        DiunParseError: If required variables are missing.
    """
    env = environ if environ is not None else os.environ

    missing = []
    for var in ("DIUN_ENTRY_STATUS", "DIUN_ENTRY_IMAGE"):
        if not env.get(var):
            missing.append(var)

    if missing:
        raise DiunParseError(
            f"Missing required diun environment variables: {', '.join(missing)}"
        )

    return DiunEvent(
        status=env["DIUN_ENTRY_STATUS"],
        image=env["DIUN_ENTRY_IMAGE"],
        hub_link=env.get("DIUN_ENTRY_HUBLINK", ""),
        digest=env.get("DIUN_ENTRY_DIGEST", ""),
        created=env.get("DIUN_ENTRY_CREATED", ""),
        platform=env.get("DIUN_ENTRY_PLATFORM", ""),
        provider=env.get("DIUN_ENTRY_PROVIDER", ""),
    )
