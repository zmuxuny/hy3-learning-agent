"""Digest envelopes with explicit self-field exclusion rules."""

from __future__ import annotations

from collections.abc import Mapping

from .canonical import sha256_digest


def _without_field(value: Mapping[str, object], field: str) -> dict[str, object]:
    return {key: item for key, item in value.items() if key != field}


def context_summary_digest(context: Mapping[str, object]) -> str:
    """Hash a state context without its ``context_sha256`` self field."""

    return sha256_digest(_without_field(context, "context_sha256"))


def environment_manifest_digest(manifest: Mapping[str, object]) -> str:
    """Hash an Environment Manifest without its ``manifest_sha256`` self field."""

    return sha256_digest(_without_field(manifest, "manifest_sha256"))


def oracle_envelope_digest(oracle: Mapping[str, object]) -> str:
    """Hash an Oracle envelope without its ``envelope_sha256`` self field."""

    return sha256_digest(_without_field(oracle, "envelope_sha256"))


def decision_episode_digest(episode: Mapping[str, object]) -> str:
    """Hash an Episode without ``provenance.episode_sha256``.

    The nested Environment and Oracle digests remain in the Episode payload,
    so any change to either contract also changes the Episode identity.
    """

    payload = dict(episode)
    provenance = payload.get("provenance")
    if not isinstance(provenance, Mapping):
        raise TypeError("Decision Episode provenance must be an object")
    payload["provenance"] = _without_field(provenance, "episode_sha256")
    return sha256_digest(payload)
