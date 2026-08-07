from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .models import read_text_file


@dataclass(frozen=True)
class FeishuConfig:
    """Credentials for one Feishu custom app.

    `encrypt_key` and `verification_token` stay optional on purpose: the long
    connection transport carries its own encryption and authentication, so a
    local run needs only the app credentials. They become required once events
    arrive over a public webhook instead.
    """

    app_id: str
    app_secret: str
    encrypt_key: str = ""
    verification_token: str = ""

    def redacted(self) -> dict[str, str]:
        return {
            "app_id": self.app_id,
            "app_secret": f"***{self.app_secret[-4:]}" if self.app_secret else "",
            "encrypt_key_set": str(bool(self.encrypt_key)),
            "verification_token_set": str(bool(self.verification_token)),
        }


def read_local_env(path: str | Path = ".env.local") -> dict[str, str]:
    """Read KEY=VALUE lines from an ignored local env file.

    Missing files are not an error here; the process environment is allowed to
    be the only source so that container runs never need the file.
    """

    env_path = Path(path)
    if not env_path.exists():
        return {}
    values: dict[str, str] = {}
    for line in read_text_file(env_path).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def load_feishu_config(path: str | Path = ".env.local") -> FeishuConfig:
    """Resolve Feishu credentials from the process environment, then .env.local.

    The process environment wins so a deployment can override the file without
    editing it.
    """

    local = read_local_env(path)

    def resolve(name: str) -> str:
        return (os.environ.get(name) or local.get(name) or "").strip()

    app_id = resolve("FEISHU_APP_ID")
    app_secret = resolve("FEISHU_APP_SECRET")
    missing = [
        name
        for name, value in (("FEISHU_APP_ID", app_id), ("FEISHU_APP_SECRET", app_secret))
        if not value
    ]
    if missing:
        # Naming the keys that *are* present turns the most common mistake --
        # pasting the Feishu console's display labels ("App ID") straight into
        # the file -- into a one-glance fix. Only key names are shown; a value
        # in this file is a credential and must never reach a log.
        found = sorted(local) if local else []
        hint = (
            f" {path} currently defines: {', '.join(found)}."
            if found
            else f" {path} defines nothing (or does not exist)."
        )
        raise ValueError(
            f"{' and '.join(missing)} must be set in the environment or {path}."
            f"{hint} The keys must be named exactly FEISHU_APP_ID and "
            "FEISHU_APP_SECRET, not the console labels 'App ID' / 'App Secret'."
        )
    return FeishuConfig(
        app_id=app_id,
        app_secret=app_secret,
        encrypt_key=resolve("FEISHU_ENCRYPT_KEY"),
        verification_token=resolve("FEISHU_VERIFICATION_TOKEN"),
    )
