from app.db.migrations import SQLITE_COLUMNS


def test_incremental_migrations_cover_new_columns():
    agent_columns = set(SQLITE_COLUMNS["agent_runs"])
    assert {"checkpoint", "pending_approval", "budget_usage", "output"} <= agent_columns
    memory_columns = set(SQLITE_COLUMNS["memories"])
    assert {"embedding", "embedding_provider"} <= memory_columns
