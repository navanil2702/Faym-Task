"""Platform adapter registry."""

from __future__ import annotations

from ..models import Platform
from .amazon import AmazonAdapter
from .base import PlatformAdapter
from .flipkart import FlipkartAdapter

ADAPTERS: dict[Platform, type[PlatformAdapter]] = {
    Platform.FLIPKART: FlipkartAdapter,
    Platform.AMAZON: AmazonAdapter,
}


def adapter_for(platform: Platform) -> type[PlatformAdapter]:
    if platform not in ADAPTERS:
        raise KeyError(f"No adapter registered for platform {platform!r}")
    return ADAPTERS[platform]


__all__ = ["ADAPTERS", "adapter_for", "PlatformAdapter", "FlipkartAdapter", "AmazonAdapter"]
