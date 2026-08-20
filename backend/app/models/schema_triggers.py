"""Canonical SQLite guards for immutable H4 facts and cross-row scopes."""

from __future__ import annotations

from sqlalchemy import event

from app.db.database import Base


SCHEMA_TRIGGER_SQL: tuple[str, ...] = (
    """
    CREATE TRIGGER IF NOT EXISTS trg_evidence_observations_no_update
    BEFORE UPDATE ON evidence_observations
    BEGIN
      SELECT RAISE(ABORT, 'evidence_observations are append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_evidence_observations_no_delete
    BEFORE DELETE ON evidence_observations
    BEGIN
      SELECT RAISE(ABORT, 'evidence_observations are append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_evidence_observations_target_scope
    AFTER INSERT ON evidence_observations
    WHEN NEW.target_observation_id IS NOT NULL
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM evidence_observations AS target
        WHERE target.id = NEW.target_observation_id
          AND target.id < NEW.id
          AND target.owner_id = NEW.owner_id
          AND target.plan_id IS NEW.plan_id
          AND target.task_id IS NEW.task_id
          AND (
            (NEW.fact_kind IN ('amendment', 'invalidation')
             AND target.fact_kind IN ('observation', 'amendment'))
            OR
            (NEW.fact_kind = 'reinstatement'
             AND target.fact_kind = 'invalidation')
          )
      ) THEN RAISE(ABORT, 'invalid evidence target scope or lifecycle') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_evidence_observations_source_scope
    AFTER INSERT ON evidence_observations
    BEGIN
      SELECT CASE WHEN NOT (
        (NEW.run_id IS NULL OR EXISTS (
          SELECT 1 FROM agent_runs
          WHERE id=NEW.run_id AND owner_id=NEW.owner_id
        ))
        AND (NEW.session_id IS NULL OR EXISTS (
          SELECT 1 FROM sessions
          WHERE id=NEW.session_id AND owner_id=NEW.owner_id
        ))
        AND (NEW.plan_id IS NULL OR EXISTS (
          SELECT 1 FROM plans
          WHERE id=NEW.plan_id AND owner_id=NEW.owner_id
        ))
        AND (NEW.task_id IS NULL OR (
          NEW.plan_id IS NOT NULL AND EXISTS (
            SELECT 1
            FROM tasks
            JOIN stages ON stages.id=tasks.stage_id
            JOIN plans ON plans.id=stages.plan_id
            WHERE tasks.id=NEW.task_id
              AND plans.id=NEW.plan_id
              AND plans.owner_id=NEW.owner_id
          )
        ))
      ) THEN RAISE(ABORT, 'evidence source scope mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_artifacts_no_update
    BEFORE UPDATE ON artifacts
    BEGIN
      SELECT RAISE(ABORT, 'artifacts are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_artifacts_no_delete
    BEFORE DELETE ON artifacts
    BEGIN
      SELECT RAISE(ABORT, 'artifacts are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_artifacts_source_scope
    AFTER INSERT ON artifacts
    BEGIN
      SELECT CASE WHEN NOT (
        (NEW.run_id IS NULL OR EXISTS (
          SELECT 1 FROM agent_runs
          WHERE id=NEW.run_id AND owner_id=NEW.owner_id
        ))
        AND (NEW.session_id IS NULL OR EXISTS (
          SELECT 1 FROM sessions
          WHERE id=NEW.session_id AND owner_id=NEW.owner_id
        ))
        AND (NEW.plan_id IS NULL OR EXISTS (
          SELECT 1 FROM plans
          WHERE id=NEW.plan_id AND owner_id=NEW.owner_id
        ))
        AND (NEW.task_id IS NULL OR (
          NEW.plan_id IS NOT NULL AND EXISTS (
            SELECT 1
            FROM tasks
            JOIN stages ON stages.id=tasks.stage_id
            JOIN plans ON plans.id=stages.plan_id
            WHERE tasks.id=NEW.task_id
              AND plans.id=NEW.plan_id
              AND plans.owner_id=NEW.owner_id
          )
        ))
      ) THEN RAISE(ABORT, 'artifact source scope mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_evidence_artifact_links_no_update
    BEFORE UPDATE ON evidence_artifact_links
    BEGIN
      SELECT RAISE(ABORT, 'evidence artifact links are append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_evidence_artifact_links_no_delete
    BEFORE DELETE ON evidence_artifact_links
    BEGIN
      SELECT RAISE(ABORT, 'evidence artifact links are append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_evidence_artifact_links_scope
    AFTER INSERT ON evidence_artifact_links
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM evidence_observations AS observation
        JOIN artifacts AS artifact ON artifact.id = NEW.artifact_id
        WHERE observation.id = NEW.observation_id
          AND artifact.owner_id = observation.owner_id
          AND artifact.plan_id IS observation.plan_id
          AND artifact.task_id IS observation.task_id
          AND artifact.content_hash = NEW.content_hash_snapshot
      ) THEN RAISE(ABORT, 'evidence artifact scope or hash mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_evidence_competency_links_no_update
    BEFORE UPDATE ON evidence_competency_links
    BEGIN
      SELECT RAISE(ABORT, 'evidence competency links are append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_evidence_competency_links_no_delete
    BEFORE DELETE ON evidence_competency_links
    BEGIN
      SELECT RAISE(ABORT, 'evidence competency links are append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_evidence_competency_links_scope
    AFTER INSERT ON evidence_competency_links
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM evidence_observations AS observation
        JOIN competencies AS competency ON competency.id = NEW.competency_id
        WHERE observation.id = NEW.observation_id
          AND competency.owner_id = observation.owner_id
          AND NEW.competency_key_snapshot = competency."key"
          AND (
            competency.scope = 'global'
            OR (competency.scope = 'plan' AND competency.plan_id IS observation.plan_id)
          )
      ) THEN RAISE(ABORT, 'evidence competency scope mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_operation_evidence_links_no_update
    BEFORE UPDATE ON operation_evidence_links
    BEGIN
      SELECT RAISE(ABORT, 'operation evidence links are append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_operation_evidence_links_no_delete
    BEFORE DELETE ON operation_evidence_links
    BEGIN
      SELECT RAISE(ABORT, 'operation evidence links are append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_operation_evidence_links_scope
    AFTER INSERT ON operation_evidence_links
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM operations AS operation
        JOIN evidence_observations AS observation
          ON observation.id = NEW.observation_id
        WHERE operation.id = NEW.operation_id
          AND operation.owner_id = observation.owner_id
      ) THEN RAISE(ABORT, 'operation evidence owner mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_operation_dependencies_no_update
    BEFORE UPDATE ON operation_dependencies
    BEGIN
      SELECT RAISE(ABORT, 'operation dependencies are append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_operation_dependencies_no_delete
    BEFORE DELETE ON operation_dependencies
    BEGIN
      SELECT RAISE(ABORT, 'operation dependencies are append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_operation_dependencies_scope
    AFTER INSERT ON operation_dependencies
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM operations AS dependent
        JOIN operations AS prerequisite
          ON prerequisite.id = NEW.depends_on_operation_id
        WHERE dependent.id = NEW.operation_id
          AND dependent.owner_id = prerequisite.owner_id
      ) THEN RAISE(ABORT, 'operation dependency owner mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_competency_graph_mutations_no_update
    BEFORE UPDATE ON competency_graph_mutations
    BEGIN
      SELECT RAISE(ABORT, 'competency graph mutations are append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_competency_graph_mutations_no_delete
    BEFORE DELETE ON competency_graph_mutations
    BEGIN
      SELECT RAISE(ABORT, 'competency graph mutations are append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_competency_graph_mutations_scope
    AFTER INSERT ON competency_graph_mutations
    WHEN NEW.operation_id IS NOT NULL
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM operations AS operation
        WHERE operation.id = NEW.operation_id
          AND operation.owner_id = NEW.owner_id
      ) THEN RAISE(ABORT, 'competency graph mutation owner mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_competency_edges_scope
    AFTER INSERT ON competency_edges
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM competencies AS source
        JOIN competencies AS target ON target.id = NEW.target_id
        WHERE source.id = NEW.source_id
          AND source.owner_id = NEW.owner_id
          AND target.owner_id = NEW.owner_id
          AND NOT (
            source.scope = 'plan'
            AND target.scope = 'plan'
            AND source.plan_id IS NOT target.plan_id
          )
      ) THEN RAISE(ABORT, 'competency edge scope mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_competency_edges_acyclic
    AFTER INSERT ON competency_edges
    WHEN NEW.relation IN ('prerequisite', 'part_of')
    BEGIN
      WITH RECURSIVE reachable(node_id) AS (
        SELECT NEW.target_id
        UNION
        SELECT edges.target_id
        FROM competency_edges AS edges
        JOIN reachable ON edges.source_id = reachable.node_id
        WHERE edges.owner_id = NEW.owner_id
          AND edges.relation IN ('prerequisite', 'part_of')
      )
      SELECT CASE WHEN EXISTS (
        SELECT 1 FROM reachable WHERE node_id = NEW.source_id
      ) THEN RAISE(ABORT, 'competency edge would create a cycle') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_plan_competency_links_scope
    AFTER INSERT ON plan_competency_links
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM plans AS plan
        JOIN competencies AS competency ON competency.id = NEW.competency_id
        WHERE plan.id = NEW.plan_id
          AND plan.owner_id = NEW.owner_id
          AND competency.owner_id = NEW.owner_id
          AND (competency.scope = 'global' OR competency.plan_id = plan.id)
      ) THEN RAISE(ABORT, 'plan competency scope mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_task_competency_links_scope
    AFTER INSERT ON task_competency_links
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM tasks AS task
        JOIN stages AS stage ON stage.id = task.stage_id
        JOIN plans AS plan ON plan.id = stage.plan_id
        JOIN competencies AS competency ON competency.id = NEW.competency_id
        WHERE task.id = NEW.task_id
          AND plan.owner_id = NEW.owner_id
          AND competency.owner_id = NEW.owner_id
          AND (competency.scope = 'global' OR competency.plan_id = plan.id)
      ) THEN RAISE(ABORT, 'task competency scope mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_resource_competency_links_scope
    AFTER INSERT ON resource_competency_links
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM learning_resources AS resource
        JOIN competencies AS competency ON competency.id = NEW.competency_id
        WHERE resource.id = NEW.resource_id
          AND resource.owner_id = NEW.owner_id
          AND competency.owner_id = NEW.owner_id
          AND (
            competency.scope = 'global'
            OR resource.plan_id IS NULL
            OR (resource.plan_id IS NOT NULL AND competency.plan_id = resource.plan_id)
          )
      ) THEN RAISE(ABORT, 'resource competency scope mismatch') END;
    END
    """,
)


def install_schema_triggers(connection) -> None:
    """Install the exact trigger set used by migration and metadata fixtures."""

    for statement in SCHEMA_TRIGGER_SQL:
        connection.exec_driver_sql(statement)


@event.listens_for(Base.metadata, "after_create")
def _install_metadata_schema_triggers(_metadata, connection, **_kwargs) -> None:
    install_schema_triggers(connection)
