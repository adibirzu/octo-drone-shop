"""GET /api/version — returns image tag + git SHA + schema version.

Fed by env vars the build pipeline stamps into the container:
    APP_IMAGE_TAG, GIT_SHA, SCHEMA_VERSION
Absence falls back to ``unknown``.

Having this endpoint means:
- On-call can `curl /api/version` and paste the output in a ticket
  without asking which image is actually running.
- Blue/green cutovers have a verifiable target (check the old version
  first, flip, curl the new version, confirm it flipped).
- Upgrade tooling (KG-040) can poll /api/version after a rollout to
  confirm every replica is on the new tag before proceeding.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter

router = APIRouter(tags=["meta"])


def build_identity() -> dict[str, str]:
    return {
        "source_commit": os.getenv("GIT_SHA") or "unknown",
        "version": os.getenv("APP_IMAGE_TAG") or "unknown",
    }


@router.get("/api/version")
async def version() -> dict[str, Any]:
    return {
        "service": os.getenv("APP_NAME", "octo-drone-shop"),
        "image_tag": os.getenv("APP_IMAGE_TAG", "unknown"),
        "git_sha": build_identity()["source_commit"],
        "schema_version": os.getenv("SCHEMA_VERSION", "unknown"),
        "environment": os.getenv("ENVIRONMENT", "unknown"),
    }
