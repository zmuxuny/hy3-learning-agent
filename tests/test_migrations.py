from pathlib import Path

from app.core.paths import lexical_absolute
from app.db.migrations import (
    CANONICAL_SCHEMA_CHECKSUM,
    CURRENT_MIGRATION_NAME,
    CURRENT_SCHEMA_VERSION,
    MIGRATION_REGISTRY,
    MigrationError,
)


FROZEN_H1_SCHEMA_CHECKSUM = (
    "e7130a9013e7bd4754c3318520d9101f18d18b88c11e8ebbe7c965ead4c5e293"
)
FROZEN_H2_SCHEMA_CHECKSUM = (
    "7f42435d235b1497a358abc4353ff6b52771514a4b252c5ba74acec6e1493de1"
)
FROZEN_H3_SCHEMA_CHECKSUM = (
    "b69ed9f0844106e54936e008c22b4d4ebd7e089a8cb38989a4306ad25c7239de"
)
FROZEN_H4_SCHEMA_CHECKSUM = (
    "851f34b9c3d455208b73c6815856da70edc57e52d5676f8b6b391e8ddf1b0ace"
)


def test_current_migration_identity_is_explicit_and_stable() -> None:
    assert CURRENT_SCHEMA_VERSION == 4
    assert CURRENT_MIGRATION_NAME == "h4_evidence_competency_facts"
    assert CANONICAL_SCHEMA_CHECKSUM == FROZEN_H4_SCHEMA_CHECKSUM


def test_migration_registry_is_literal_contiguous_and_unique() -> None:
    registry_contract = tuple(
        (revision.version, revision.name, revision.checksum)
        for revision in MIGRATION_REGISTRY
    )

    assert registry_contract == (
        (1, "h1_canonical_schema", FROZEN_H1_SCHEMA_CHECKSUM),
        (2, "h2_transaction_outbox", FROZEN_H2_SCHEMA_CHECKSUM),
        (3, "h3_durable_runtime", FROZEN_H3_SCHEMA_CHECKSUM),
        (4, "h4_evidence_competency_facts", FROZEN_H4_SCHEMA_CHECKSUM),
    )
    versions = [revision.version for revision in MIGRATION_REGISTRY]
    names = [revision.name for revision in MIGRATION_REGISTRY]
    checksums = [revision.checksum for revision in MIGRATION_REGISTRY]
    assert versions == list(range(1, CURRENT_SCHEMA_VERSION + 1))
    assert len(versions) == len(set(versions))
    assert len(names) == len(set(names))
    assert all(len(checksum) == 64 for checksum in checksums)


def test_migration_error_exposes_machine_code_and_recovery_backup() -> None:
    backup = Path("synthetic-backup")

    error = MigrationError("synthetic_failure", "safe public message", recovery_backup=backup)

    assert error.code == "synthetic_failure"
    assert error.recovery_backup == backup
    assert str(error) == "safe public message"


def test_lexical_absolute_normalizes_dot_segments_without_following_symlinks(
    tmp_path: Path,
) -> None:
    real_directory = tmp_path / "real"
    real_directory.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real_directory, target_is_directory=True)
    (tmp_path / "decoy").mkdir()

    normalized = lexical_absolute(
        tmp_path / "decoy" / ".." / "link" / "database.sqlite3"
    )

    assert normalized == tmp_path / "link" / "database.sqlite3"
    assert normalized != real_directory / "database.sqlite3"
