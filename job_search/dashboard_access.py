"""Explicit opt-in to the host's Tailscale Serve identity boundary.

The backend must stay on host loopback. Local host processes are trusted; do not
give untrusted containers host networking or otherwise expose this listener.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class DashboardAccess:
    https_origin: str = ""
    allowed_tailscale_login: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.https_origin, str) or not isinstance(
            self.allowed_tailscale_login, str
        ):
            raise ValueError("dashboard HTTPS origin and login must be strings")
        if bool(self.https_origin) != bool(self.allowed_tailscale_login):
            raise ValueError(
                "dashboard_https_origin and dashboard_allowed_tailscale_login "
                "must be configured together"
            )
        if not self.https_origin:
            return
        # Serve certificates use machine.tailnet.ts.net. Reject URL paths,
        # credentials, wildcards, alternate ports and implicit normalizations.
        label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
        if not re.fullmatch(
            rf"https://{label}\.{label}\.ts\.net(?::443)?", self.https_origin
        ):
            raise ValueError(
                "dashboard_https_origin must be an HTTPS machine.tailnet.ts.net "
                "origin without a path (port 443 only)"
            )
        if not re.fullmatch(r"[\x21-\x7e]{1,320}", self.allowed_tailscale_login) or any(
            char in self.allowed_tailscale_login for char in ",;<>"
        ):
            raise ValueError("dashboard_allowed_tailscale_login must be one ASCII login")
        # Browser Origin serialization omits the default HTTPS port.
        if self.https_origin.endswith(":443"):
            object.__setattr__(self, "https_origin", self.https_origin[:-4])

    @property
    def host(self) -> str:
        return urlsplit(self.https_origin).netloc
