# Deterministic database fixtures

These SQL sources are public, synthetic, and immutable test inputs. Tests execute
them only into a per-test SQLite file under pytest's temporary directory; no test
opens a source fixture or a runtime database for writing.

- `empty.sql` represents a valid database with no application schema.
- `v1_1_1_full.sql` reproduces the `fe3db33` v1.1.1 schema and contains one
  synthetic row in every business table.
- `review_boundary.sql` is a review-scale V2 ledger containing separate plans
  with 0, 1, 500, 501, and 10,000 Evidence rows plus a 10,000-message Session.
- `pre_h1/metadata_only.sql`, `pre_h1/fresh_create_schema.sql`, and
  `pre_h1/v1_additive_upgrade.sql` are schema-only provenance snapshots rebuilt
  from revision `baf2564`. They preserve the three pre-H1 schema fingerprints
  accepted by the migration classifier without importing today's ORM metadata.
  They contain DDL only: no `INSERT` statements or business rows.

All identifiers, text, URLs, and timestamps are fixed fixture values. The files
must not be regenerated from `data/`, `data/backups/`, `.env`, Context snapshots,
or any user workspace. `manifest.json` records source hashes and semantic counts.
For the pre-H1 snapshots it also freezes the source revision, source-artifact
hashes, exact DDL hashes, generation recipes, and production semantic checksums.

When a fixture changes intentionally, review the SQL, run the hardening fixture
tests, then update its SHA-256 and expected counts in the manifest. Pre-H1 DDL
must be reconstructed from the recorded revision in an isolated environment,
reviewed as text, and materialized into a temporary SQLite file before updating
its frozen checksum. Never weaken the secret/personal-data scan to make a copied
runtime database pass.
