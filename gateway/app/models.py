"""Validated records accepted from the gateway control plane."""

import re
from ipaddress import IPv4Address
from typing import Literal

from pydantic import BaseModel, SecretStr, ValidationError, field_validator

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,62}$")


def validate_name(value: str) -> str:
    """Return a safe display identifier suitable for generated configuration."""
    if not _SAFE_NAME.fullmatch(value):
        raise ValueError("name must be 1–63 safe characters")
    return value


class ExitCreate(BaseModel):
    """A new foreign egress node request; SSH access is root-only by design."""

    name: str
    host: IPv4Address
    login: Literal["root"]
    password: SecretStr

    @field_validator("name")
    @classmethod
    def require_safe_name(cls, value: str) -> str:
        return validate_name(value)


__all__ = ["ExitCreate", "ValidationError", "validate_name"]
