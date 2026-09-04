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


def snapshot_entity_digest(entity: Mapping[str, object]) -> str:
    """Hash a v2 Snapshot entity without its self field."""

    return sha256_digest(_without_field(entity, "entity_sha256"))


def state_snapshot_digest(snapshot: Mapping[str, object]) -> str:
    """Hash a v2 state Snapshot without its self field."""

    return sha256_digest(_without_field(snapshot, "snapshot_sha256"))


def state_delta_digest(delta: Mapping[str, object]) -> str:
    """Hash a v2 State Delta without its self field."""

    return sha256_digest(_without_field(delta, "delta_sha256"))


def episode_completeness_digest(completeness: Mapping[str, object]) -> str:
    """Hash a v2 completeness envelope without its self field."""

    return sha256_digest(_without_field(completeness, "completeness_sha256"))


def rule_result_digest(result: Mapping[str, object]) -> str:
    """Hash a Rule Result without its self field."""

    return sha256_digest(_without_field(result, "result_sha256"))


def integrity_result_digest(result: Mapping[str, object]) -> str:
    """Hash an integrity result without its self field."""

    return sha256_digest(_without_field(result, "result_sha256"))


def artifact_manifest_digest(manifest: Mapping[str, object]) -> str:
    """Hash a versioned evaluation manifest without its self field."""

    return sha256_digest(_without_field(manifest, "manifest_sha256"))


def judge_result_digest(result: Mapping[str, object]) -> str:
    """Hash a Judge Result without its self field."""

    return sha256_digest(_without_field(result, "result_sha256"))


def aggregate_result_digest(result: Mapping[str, object]) -> str:
    """Hash an Episode or track aggregate without its self field."""

    return sha256_digest(_without_field(result, "result_sha256"))


def case_spec_digest(case_spec: Mapping[str, object]) -> str:
    """Hash a CaseSpec without its ``case_spec_sha256`` self field."""

    return sha256_digest(_without_field(case_spec, "case_spec_sha256"))


def judge_reference_digest(reference: Mapping[str, object]) -> str:
    """Hash a JudgeReference without its ``reference_sha256`` self field."""

    return sha256_digest(_without_field(reference, "reference_sha256"))


def provider_attestation_digest(attestation: Mapping[str, object]) -> str:
    """Hash provider attribution without its ``attestation_sha256`` self field."""

    return sha256_digest(_without_field(attestation, "attestation_sha256"))


def model_visible_context_digest(context: Mapping[str, object]) -> str:
    """Hash exact model-visible context without its self field."""

    return sha256_digest(_without_field(context, "context_sha256"))


def runtime_failure_digest(failure: Mapping[str, object]) -> str:
    """Hash a runtime Failure without its ``failure_sha256`` self field."""

    return sha256_digest(_without_field(failure, "failure_sha256"))


def protocol_release_digest(release: Mapping[str, object]) -> str:
    """Hash an Evaluation Protocol Release without its self field."""

    return sha256_digest(_without_field(release, "release_sha256"))


def benchmark_release_digest(release: Mapping[str, object]) -> str:
    """Hash a Benchmark Release Manifest without its self field."""

    return sha256_digest(_without_field(release, "manifest_sha256"))


def source_bundle_digest(bundle: Mapping[str, object]) -> str:
    """Hash a source bundle manifest without its self field."""

    return sha256_digest(_without_field(bundle, "bundle_sha256"))


def schema_lock_digest(lock: Mapping[str, object]) -> str:
    """Hash a Schema Lock Manifest without its self field."""

    return sha256_digest(_without_field(lock, "manifest_sha256"))


def trusted_registry_digest(registry: Mapping[str, object]) -> str:
    """Hash a trusted Benchmark registry without its self field."""

    return sha256_digest(_without_field(registry, "registry_sha256"))
