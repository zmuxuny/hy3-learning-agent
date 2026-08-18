# Deterministic database fixtures

These SQL sources are public, synthetic, and immutable test inputs. Tests execute
them only into a per-test SQLite file under pytest's temporary directory; no test
opens a source fixture or a runtime database for writing.

- `empty.sql` represents a valid database with no application schema.
- `v1_1_1_full.sql` reproduces the `fe3db33` v1.1.1 schema and contains one
  synthetic row in every business table.
- `review_boundary.sql` is a review-scale V2 ledger containing separate plans
  with 0, 1, 500, 501, and 10,000 Evidence rows plus a 10,000-message Session.

All identifiers, text, URLs, and timestamps are fixed fixture values. The files
must not be regenerated from `data/`, `data/backups/`, `.env`, Context snapshots,
or any user workspace. `manifest.json` records source hashes and semantic counts.

When a fixture changes intentionally, review the SQL, run the hardening fixture
tests, then update its SHA-256 and expected counts in the manifest. Never weaken
the secret/personal-data scan to make a copied runtime database pass.
