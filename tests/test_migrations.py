from app.db.migrations import SQLITE_COLUMNS


def test_incremental_migrations_cover_new_columns():
    agent_columns = set(SQLITE_COLUMNS["agent_runs"])
    assert {"checkpoint", "pending_approval", "budget_usage", "output", "created_plan_id"} <= agent_columns
    memory_columns = set(SQLITE_COLUMNS["memories"])
    assert {
        "embedding",
        "embedding_provider",
        "archived_from_status",
        "archived_reason",
        "supersedes_id",
        "superseded_by_id",
        "last_accessed_at",
        "access_count",
        "last_reinforced_at",
    } <= memory_columns
