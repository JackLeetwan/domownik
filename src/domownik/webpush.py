"""Web Push plumbing: the server's VAPID key pair and delivery to one subscription."""

from __future__ import annotations

import base64
from functools import lru_cache
from urllib.parse import urlparse

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

from domownik.config import get_settings
from domownik.models import PushSubscription

TTL_SECONDS = 12 * 3600  # a digest older than half a day is not worth showing


class PushError(Exception):
    """Temporary delivery problem – worth retrying later."""


def _key_path():
    return get_settings().secrets_dir / "vapid_private.pem"


@lru_cache
def vapid() -> Vapid:
    """Generated once and kept in data/secrets (not in cloud backups – devices re-subscribe after a restore)."""
    path = _key_path()
    if not path.exists():
        v = Vapid()
        v.generate_keys()
        path.parent.mkdir(parents=True, exist_ok=True)
        v.save_key(str(path))
        path.chmod(0o600)
    return Vapid.from_file(str(path))


def public_key() -> str:
    """applicationServerKey for pushManager.subscribe() – base64url, uncompressed P-256 point."""
    raw = vapid().public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _subject() -> str:
    base = get_settings().base_url
    # push services (Apple in particular) want an https URL or a mailto: contact
    return base if base.startswith("https://") else "mailto:domownik@example.com"


def deliver(sub: PushSubscription, data: str, *, urgent: bool = False) -> bool:
    """Send one encrypted message. False = the subscription is gone (delete it)."""
    try:
        webpush(
            subscription_info=sub.info(),
            data=data,
            vapid_private_key=vapid(),
            vapid_claims={"sub": _subject(), "aud": "{0.scheme}://{0.netloc}".format(urlparse(sub.endpoint))},
            ttl=TTL_SECONDS,
            headers={"Urgency": "high" if urgent else "normal"},
            timeout=15,
        )
    except WebPushException as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status in (404, 410):
            return False
        raise PushError(f"{status or ''} {exc}".strip()) from exc
    except Exception as exc:  # network errors from requests
        raise PushError(str(exc)) from exc
    return True
