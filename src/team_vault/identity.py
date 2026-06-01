import re
from dataclasses import dataclass
from pathlib import Path

import yaml

OWNER_PATTERN = re.compile(r"[^a-zA-Z0-9_.-]+")


@dataclass(frozen=True, slots=True)
class ClientIdentity:
    hostname: str | None
    os_user: str | None


@dataclass(frozen=True, slots=True)
class OwnerResolver:
    hostname_map: dict[str, str]
    trust_os_user_fallback: bool
    default_owner: str

    @classmethod
    def from_file(
        cls,
        path: Path | None,
        *,
        trust_os_user_fallback: bool,
        default_owner: str,
    ) -> "OwnerResolver":
        if path is None or not path.exists():
            return cls({}, trust_os_user_fallback, sanitize_owner(default_owner))

        with path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}

        mappings_raw = raw.get("mappings", raw)
        mappings = {str(key): sanitize_owner(str(value)) for key, value in mappings_raw.items()}
        return cls(mappings, trust_os_user_fallback, sanitize_owner(default_owner))

    def resolve(self, identity: ClientIdentity) -> str:
        if identity.hostname in self.hostname_map:
            return self.hostname_map[identity.hostname]
        if self.trust_os_user_fallback and identity.os_user:
            return sanitize_owner(identity.os_user)
        return self.default_owner


def sanitize_owner(value: str) -> str:
    cleaned = OWNER_PATTERN.sub("-", value.strip()).strip(".-")
    if cleaned:
        return cleaned.lower()
    return "unknown"
