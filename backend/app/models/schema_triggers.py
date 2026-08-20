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


H5_SCHEMA_TRIGGER_SQL: tuple[str, ...] = (
    """
    CREATE TRIGGER IF NOT EXISTS trg_chat_messages_versioned_content
    BEFORE UPDATE OF content, version, content_hash ON chat_messages
    WHEN NOT (
      (
        NEW.content<>OLD.content
        AND NEW.version=OLD.version+1
        AND length(NEW.content_hash)=64
        AND NEW.content_hash<>OLD.content_hash
      )
      OR
      (
        NEW.content=OLD.content
        AND NEW.version=OLD.version
        AND (
          NEW.content_hash=OLD.content_hash
          OR (OLD.content_hash='' AND length(NEW.content_hash)=64)
        )
      )
    )
    BEGIN
      SELECT RAISE(ABORT, 'chat message content requires next version and digest');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_chat_messages_canonical_intervention_immutable
    BEFORE UPDATE OF content, version, content_hash, role, session_id, run_id
    ON chat_messages
    WHEN EXISTS (
      SELECT 1 FROM interventions WHERE canonical_message_id=OLD.id
    )
    BEGIN
      SELECT RAISE(ABORT, 'canonical intervention message is immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_chat_message_revisions_no_update
    BEFORE UPDATE ON chat_message_revisions
    BEGIN
      SELECT RAISE(ABORT, 'chat message revisions are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_chat_message_revisions_no_delete
    BEFORE DELETE ON chat_message_revisions
    BEGIN
      SELECT RAISE(ABORT, 'chat message revisions are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_chat_message_revisions_scope
    AFTER INSERT ON chat_message_revisions
    BEGIN
      SELECT CASE WHEN length(NEW.content_hash)<>64 OR NOT EXISTS (
        SELECT 1
        FROM chat_messages AS message
        JOIN sessions AS session ON session.id=message.session_id
        WHERE message.id=NEW.message_id
          AND message.session_id=NEW.session_id
          AND message.version=NEW.version
          AND message.content=NEW.content
          AND message.content_hash=NEW.content_hash
          AND (
            NEW.previous_run_id IS NULL
            OR EXISTS (
              SELECT 1 FROM agent_runs AS previous_run
              WHERE previous_run.id=NEW.previous_run_id
                AND previous_run.owner_id=session.owner_id
                AND previous_run.session_id=NEW.session_id
            )
          )
      ) THEN RAISE(ABORT, 'chat message revision scope or version mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_agent_runs_captured_candidate_immutable
    BEFORE UPDATE OF proactive_candidate_state, proactive_candidate_key,
      proactive_candidate_kind, proactive_candidate_payload,
      proactive_candidate_digest, proactive_source_watermark,
      proactive_source_projection_digest, proactive_detected_at
    ON agent_runs
    WHEN OLD.proactive_candidate_state='captured' AND NOT (
      NEW.proactive_candidate_state=OLD.proactive_candidate_state
      AND NEW.proactive_candidate_key IS OLD.proactive_candidate_key
      AND NEW.proactive_candidate_kind IS OLD.proactive_candidate_kind
      AND NEW.proactive_candidate_payload=OLD.proactive_candidate_payload
      AND NEW.proactive_candidate_digest IS OLD.proactive_candidate_digest
      AND NEW.proactive_source_watermark IS OLD.proactive_source_watermark
      AND NEW.proactive_source_projection_digest IS OLD.proactive_source_projection_digest
      AND NEW.proactive_detected_at IS OLD.proactive_detected_at
    )
    BEGIN
      SELECT RAISE(ABORT, 'captured proactive candidate is immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_context_states_generation
    BEFORE UPDATE ON context_states
    WHEN NEW.generation <> OLD.generation + 1
    BEGIN
      SELECT RAISE(ABORT, 'context generation must advance exactly once');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_context_states_no_delete
    BEFORE DELETE ON context_states
    BEGIN
      SELECT RAISE(ABORT, 'context states cannot be deleted');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_provenance_nodes_no_update
    BEFORE UPDATE ON provenance_nodes
    BEGIN
      SELECT RAISE(ABORT, 'provenance nodes are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_provenance_nodes_no_delete
    BEFORE DELETE ON provenance_nodes
    BEGIN
      SELECT RAISE(ABORT, 'provenance nodes are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_provenance_nodes_scope
    AFTER INSERT ON provenance_nodes
    BEGIN
      SELECT CASE WHEN NOT (
        EXISTS (SELECT 1 FROM owners WHERE id=NEW.owner_id)
        AND (NEW.plan_id IS NULL OR EXISTS (
          SELECT 1 FROM plans WHERE id=NEW.plan_id AND owner_id=NEW.owner_id
        ))
        AND (NEW.session_id IS NULL OR EXISTS (
          SELECT 1 FROM sessions WHERE id=NEW.session_id AND owner_id=NEW.owner_id
            AND plan_id IS NEW.plan_id
        ))
        AND (NEW.kind<>'legacy_notification' OR EXISTS (
          SELECT 1 FROM notifications
          WHERE CAST(id AS TEXT)=NEW.entity_key AND owner_id=NEW.owner_id
            AND plan_id IS NEW.plan_id AND NEW.session_id IS NULL
        ))
      ) THEN RAISE(ABORT, 'provenance node scope mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_provenance_edges_no_update
    BEFORE UPDATE ON provenance_edges
    BEGIN
      SELECT RAISE(ABORT, 'provenance edges are append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_provenance_edges_no_delete
    BEFORE DELETE ON provenance_edges
    BEGIN
      SELECT RAISE(ABORT, 'provenance edges are append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_provenance_edges_scope
    AFTER INSERT ON provenance_edges
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM provenance_nodes AS source
        JOIN provenance_nodes AS target ON target.id=NEW.target_node_id
        WHERE source.id=NEW.source_node_id
          AND source.owner_id=NEW.owner_id
          AND target.owner_id=NEW.owner_id
          AND (
            source.plan_id IS NULL OR source.plan_id IS target.plan_id
            OR (
              NEW.relation IN ('context_retained', 'context_dropped')
              AND target.plan_id IS NULL AND source.kind='plan_summary'
            )
          )
          AND (
            (NEW.relation='summary_source' AND source.kind='message'
             AND target.kind='session_summary'
             AND source.session_id IS target.session_id)
            OR
            (NEW.relation='summary_base' AND source.kind='session_summary'
             AND target.kind='session_summary'
             AND source.session_id IS target.session_id)
            OR
            (NEW.relation='memory_source' AND target.kind='memory'
             AND source.kind IN ('message', 'session_summary', 'session_handoff', 'agent_run')
             AND (target.session_id IS NULL OR source.session_id IS target.session_id))
            OR
            (NEW.relation='handoff_source' AND target.kind='session_handoff'
             AND source.kind IN ('message', 'session_summary', 'agent_run',
                                 'plan_proposal', 'planning_intake')
             AND EXISTS (
               SELECT 1 FROM session_handoffs AS handoff
               WHERE handoff.id=target.entity_key
                 AND source.session_id=handoff.source_session_id
             ))
            OR
            (NEW.relation IN ('context_retained', 'context_dropped')
             AND target.kind='context_snapshot'
             AND (source.session_id IS NULL OR source.session_id IS target.session_id))
          )
      ) THEN RAISE(ABORT, 'provenance edge scope or kind mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_session_compression_states_scope
    AFTER INSERT ON session_compression_states
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM sessions
        WHERE id=NEW.session_id AND owner_id=NEW.owner_id
      ) THEN RAISE(ABORT, 'session compression owner mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_session_compression_states_no_delete
    BEFORE DELETE ON session_compression_states
    BEGIN
      SELECT RAISE(ABORT, 'session compression cursor cannot be deleted');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_session_compression_states_identity
    BEFORE UPDATE ON session_compression_states
    WHEN NEW.session_id<>OLD.session_id OR NEW.owner_id<>OLD.owner_id
    BEGIN
      SELECT RAISE(ABORT, 'session compression identity is immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_session_compression_states_targets
    AFTER INSERT ON session_compression_states
    BEGIN
      SELECT CASE WHEN NOT (
        (NEW.covered_through_message_id IS NULL OR EXISTS (
          SELECT 1 FROM chat_messages
          WHERE id=NEW.covered_through_message_id AND session_id=NEW.session_id
            AND version=NEW.covered_through_message_version
        ))
        AND (NEW.claim_token IS NULL OR (
          EXISTS (
            SELECT 1 FROM chat_messages
            WHERE id=NEW.claim_start_message_id AND session_id=NEW.session_id
          )
          AND EXISTS (
            SELECT 1 FROM chat_messages
            WHERE id=NEW.claim_end_message_id AND session_id=NEW.session_id
          )
          AND (NEW.claim_base_summary_id IS NULL OR EXISTS (
            SELECT 1 FROM session_summaries
            WHERE id=NEW.claim_base_summary_id AND session_id=NEW.session_id
              AND owner_id=NEW.owner_id AND validity_state='valid'
          ))
          AND json_array_length(NEW.claim_source_manifest)>0
          AND CAST(json_extract(NEW.claim_source_manifest, '$[0].id') AS INTEGER)=
              NEW.claim_start_message_id
          AND CAST(json_extract(
                NEW.claim_source_manifest,
                '$[' || (json_array_length(NEW.claim_source_manifest)-1) || '].id'
              ) AS INTEGER)=NEW.claim_end_message_id
          AND NOT EXISTS (
            SELECT 1 FROM json_each(NEW.claim_source_manifest) AS manifest
            WHERE NOT EXISTS (
              SELECT 1 FROM chat_messages AS message
              WHERE message.id=CAST(json_extract(manifest.value, '$.id') AS INTEGER)
                AND message.session_id=NEW.session_id
                AND message.version=CAST(json_extract(manifest.value, '$.version') AS INTEGER)
                AND message.role=json_extract(manifest.value, '$.role')
                AND message.content_hash=json_extract(manifest.value, '$.content_digest')
                AND length(CAST(message.content AS BLOB))=
                    CAST(json_extract(manifest.value, '$.source_bytes') AS INTEGER)
                AND message.validity_state='active'
            )
          )
        ))
      ) THEN RAISE(ABORT, 'session compression target scope mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_session_compression_states_update_targets
    AFTER UPDATE OF covered_through_message_id, covered_through_message_version,
      claim_token, claim_start_message_id, claim_end_message_id,
      claim_base_summary_id, claim_source_manifest, claim_source_digest
    ON session_compression_states
    BEGIN
      SELECT CASE WHEN NOT (
        (NEW.covered_through_message_id IS NULL OR EXISTS (
          SELECT 1 FROM chat_messages
          WHERE id=NEW.covered_through_message_id AND session_id=NEW.session_id
            AND version=NEW.covered_through_message_version
        ))
        AND (NEW.claim_token IS NULL OR (
          EXISTS (
            SELECT 1 FROM chat_messages
            WHERE id=NEW.claim_start_message_id AND session_id=NEW.session_id
          )
          AND EXISTS (
            SELECT 1 FROM chat_messages
            WHERE id=NEW.claim_end_message_id AND session_id=NEW.session_id
          )
          AND (NEW.claim_base_summary_id IS NULL OR EXISTS (
            SELECT 1 FROM session_summaries
            WHERE id=NEW.claim_base_summary_id AND session_id=NEW.session_id
              AND owner_id=NEW.owner_id AND validity_state='valid'
          ))
          AND json_array_length(NEW.claim_source_manifest)>0
          AND CAST(json_extract(NEW.claim_source_manifest, '$[0].id') AS INTEGER)=
              NEW.claim_start_message_id
          AND CAST(json_extract(
                NEW.claim_source_manifest,
                '$[' || (json_array_length(NEW.claim_source_manifest)-1) || '].id'
              ) AS INTEGER)=NEW.claim_end_message_id
          AND NOT EXISTS (
            SELECT 1 FROM json_each(NEW.claim_source_manifest) AS manifest
            WHERE NOT EXISTS (
              SELECT 1 FROM chat_messages AS message
              WHERE message.id=CAST(json_extract(manifest.value, '$.id') AS INTEGER)
                AND message.session_id=NEW.session_id
                AND message.version=CAST(json_extract(manifest.value, '$.version') AS INTEGER)
                AND message.role=json_extract(manifest.value, '$.role')
                AND message.content_hash=json_extract(manifest.value, '$.content_digest')
                AND length(CAST(message.content AS BLOB))=
                    CAST(json_extract(manifest.value, '$.source_bytes') AS INTEGER)
                AND message.validity_state='active'
            )
          )
        ))
      ) THEN RAISE(ABORT, 'session compression target scope mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_session_summaries_scope
    AFTER INSERT ON session_summaries
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM sessions
        WHERE id=NEW.session_id AND owner_id=NEW.owner_id
      ) THEN RAISE(ABORT, 'session summary owner mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_session_summaries_lifecycle
    BEFORE UPDATE ON session_summaries
    WHEN NOT (
      OLD.validity_state='building' AND NEW.validity_state='valid'
      AND NEW.owner_id=OLD.owner_id AND NEW.session_id=OLD.session_id
      AND NEW.version=OLD.version AND NEW.content=OLD.content
      AND NEW.coverage_start_message_id IS OLD.coverage_start_message_id
      AND NEW.covered_through_message_id IS OLD.covered_through_message_id
      AND NEW.coverage_count=OLD.coverage_count
      AND NEW.source_message_ids=OLD.source_message_ids
      AND NEW.method=OLD.method
      AND NEW.source_digest=OLD.source_digest AND NEW.content_hash=OLD.content_hash
      AND NEW.algorithm_version=OLD.algorithm_version
      AND NEW.invalidated_at IS OLD.invalidated_at
      AND NEW.invalidation_reason=OLD.invalidation_reason
      AND NEW.created_at=OLD.created_at
      OR
      OLD.validity_state IN ('valid', 'legacy_unverified') AND NEW.validity_state='invalid'
      AND NEW.invalidated_at IS NOT NULL AND length(NEW.invalidation_reason)>0
      AND NEW.owner_id=OLD.owner_id AND NEW.session_id=OLD.session_id
      AND NEW.version=OLD.version AND NEW.content=OLD.content
      AND NEW.coverage_start_message_id IS OLD.coverage_start_message_id
      AND NEW.covered_through_message_id IS OLD.covered_through_message_id
      AND NEW.coverage_count=OLD.coverage_count
      AND NEW.source_message_ids=OLD.source_message_ids
      AND NEW.method=OLD.method
      AND NEW.source_digest=OLD.source_digest AND NEW.content_hash=OLD.content_hash
      AND NEW.algorithm_version=OLD.algorithm_version
      AND NEW.provenance_node_id IS OLD.provenance_node_id
      AND NEW.created_at=OLD.created_at
    )
    BEGIN
      SELECT RAISE(ABORT, 'invalid session summary lifecycle transition');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_session_summaries_no_delete
    BEFORE DELETE ON session_summaries
    BEGIN
      SELECT RAISE(ABORT, 'session summaries are immutable audit facts');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_session_summaries_finalize
    AFTER UPDATE OF validity_state ON session_summaries
    WHEN NEW.validity_state='valid'
    BEGIN
      SELECT CASE WHEN NEW.provenance_node_id IS NULL OR NEW.coverage_count <= 0
        OR NEW.coverage_start_message_id IS NULL
        OR NEW.covered_through_message_id IS NULL
        OR length(NEW.source_digest) <> 64
        OR NOT EXISTS (
          SELECT 1 FROM provenance_nodes
          WHERE id=NEW.provenance_node_id AND owner_id=NEW.owner_id
            AND kind='session_summary' AND entity_key=CAST(NEW.id AS TEXT)
            AND entity_version=NEW.version AND session_id=NEW.session_id
            AND content_digest=NEW.content_hash
        )
        OR (SELECT count(*) FROM provenance_edges
            WHERE target_node_id=NEW.provenance_node_id
              AND relation='summary_source') <> NEW.coverage_count
        OR json_array_length(NEW.source_message_ids) <> NEW.coverage_count
        OR NOT EXISTS (
          SELECT 1
          FROM provenance_edges AS edge
          JOIN provenance_nodes AS source ON source.id=edge.source_node_id
          JOIN chat_messages AS message
            ON CAST(message.id AS TEXT)=source.entity_key
           AND message.version=source.entity_version
          WHERE edge.target_node_id=NEW.provenance_node_id
            AND edge.relation='summary_source' AND edge.ordinal=0
            AND message.id=NEW.coverage_start_message_id
            AND message.session_id=NEW.session_id
            AND message.validity_state='active'
        )
        OR NOT EXISTS (
          SELECT 1
          FROM provenance_edges AS edge
          JOIN provenance_nodes AS source ON source.id=edge.source_node_id
          JOIN chat_messages AS message
            ON CAST(message.id AS TEXT)=source.entity_key
           AND message.version=source.entity_version
          WHERE edge.target_node_id=NEW.provenance_node_id
            AND edge.relation='summary_source'
            AND edge.ordinal=NEW.coverage_count-1
            AND message.id=NEW.covered_through_message_id
            AND message.session_id=NEW.session_id
            AND message.validity_state='active'
        )
        OR EXISTS (
          SELECT 1
          FROM json_each(NEW.source_message_ids) AS manifest
          WHERE NOT EXISTS (
            SELECT 1
            FROM provenance_edges AS edge
            JOIN provenance_nodes AS source ON source.id=edge.source_node_id
            WHERE edge.target_node_id=NEW.provenance_node_id
              AND edge.relation='summary_source'
              AND edge.ordinal=CAST(manifest.key AS INTEGER)
              AND source.kind='message'
              AND source.entity_key=CAST(manifest.value AS TEXT)
          )
        )
        OR (
          SELECT count(*)
          FROM chat_messages AS candidate
          JOIN chat_messages AS first_message ON first_message.id=NEW.coverage_start_message_id
          JOIN chat_messages AS last_message ON last_message.id=NEW.covered_through_message_id
          WHERE candidate.session_id=NEW.session_id
            AND candidate.validity_state='active'
            AND (
              candidate.created_at>first_message.created_at
              OR (candidate.created_at=first_message.created_at AND candidate.id>=first_message.id)
            )
            AND (
              candidate.created_at<last_message.created_at
              OR (candidate.created_at=last_message.created_at AND candidate.id<=last_message.id)
            )
        ) <> NEW.coverage_count
        OR EXISTS (
          SELECT 1
          FROM chat_messages AS candidate
          JOIN chat_messages AS first_message ON first_message.id=NEW.coverage_start_message_id
          JOIN chat_messages AS last_message ON last_message.id=NEW.covered_through_message_id
          WHERE candidate.session_id=NEW.session_id
            AND candidate.validity_state='active'
            AND (
              candidate.created_at>first_message.created_at
              OR (candidate.created_at=first_message.created_at AND candidate.id>=first_message.id)
            )
            AND (
              candidate.created_at<last_message.created_at
              OR (candidate.created_at=last_message.created_at AND candidate.id<=last_message.id)
            )
            AND NOT EXISTS (
              SELECT 1
              FROM provenance_edges AS edge
              JOIN provenance_nodes AS source ON source.id=edge.source_node_id
              WHERE edge.target_node_id=NEW.provenance_node_id
                AND edge.relation='summary_source'
                AND source.kind='message'
                AND source.entity_key=CAST(candidate.id AS TEXT)
                AND source.entity_version=candidate.version
            )
        )
        OR (SELECT count(*) FROM provenance_edges
            WHERE target_node_id=NEW.provenance_node_id
              AND relation='summary_base') > 1
      THEN RAISE(ABORT, 'session summary provenance is incomplete') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_session_handoffs_scope
    AFTER INSERT ON session_handoffs
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM sessions AS source
        JOIN sessions AS target ON target.id=NEW.target_session_id
        JOIN plans AS plan ON plan.id=NEW.plan_id
        WHERE source.id=NEW.source_session_id
          AND source.owner_id=NEW.owner_id
          AND target.owner_id=NEW.owner_id
          AND target.plan_id=NEW.plan_id
          AND plan.owner_id=NEW.owner_id
          AND source.id<>target.id
      ) THEN RAISE(ABORT, 'session handoff scope mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_session_handoffs_lifecycle
    BEFORE UPDATE ON session_handoffs
    WHEN NOT (
      OLD.validity_state='building' AND NEW.validity_state='valid'
      AND NEW.owner_id=OLD.owner_id AND NEW.source_session_id=OLD.source_session_id
      AND NEW.target_session_id=OLD.target_session_id AND NEW.plan_id=OLD.plan_id
      AND NEW.version=OLD.version AND NEW.content=OLD.content
      AND NEW.content_hash=OLD.content_hash
      AND NEW.source_context_generation=OLD.source_context_generation
      AND NEW.provenance_state=OLD.provenance_state AND NEW.created_at=OLD.created_at
      OR
      OLD.validity_state IN ('valid', 'legacy_unverified') AND NEW.validity_state='invalid'
      AND NEW.invalidated_at IS NOT NULL AND length(NEW.invalidation_reason)>0
      AND NEW.owner_id=OLD.owner_id AND NEW.source_session_id=OLD.source_session_id
      AND NEW.target_session_id=OLD.target_session_id AND NEW.plan_id=OLD.plan_id
      AND NEW.version=OLD.version AND NEW.content=OLD.content
      AND NEW.content_hash=OLD.content_hash
      AND NEW.source_context_generation=OLD.source_context_generation
      AND NEW.provenance_state=OLD.provenance_state
      AND NEW.provenance_node_id IS OLD.provenance_node_id
      AND NEW.created_at=OLD.created_at
    )
    BEGIN
      SELECT RAISE(ABORT, 'invalid session handoff lifecycle transition');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_session_handoffs_no_delete
    BEFORE DELETE ON session_handoffs
    BEGIN
      SELECT RAISE(ABORT, 'session handoffs are immutable audit facts');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_session_handoffs_finalize
    AFTER UPDATE OF validity_state ON session_handoffs
    WHEN NEW.validity_state='valid'
    BEGIN
      SELECT CASE WHEN NEW.provenance_state<>'verified'
        OR NEW.provenance_node_id IS NULL
        OR NOT EXISTS (
          SELECT 1 FROM context_states
          WHERE owner_id=NEW.owner_id
            AND generation=NEW.source_context_generation
        )
        OR NOT EXISTS (
          SELECT 1 FROM provenance_nodes
          WHERE id=NEW.provenance_node_id AND owner_id=NEW.owner_id
            AND kind='session_handoff' AND entity_key=NEW.id
            AND entity_version=NEW.version AND content_digest=NEW.content_hash
            AND plan_id=NEW.plan_id AND session_id=NEW.target_session_id
        )
        OR NOT EXISTS (
          SELECT 1 FROM provenance_edges
          WHERE target_node_id=NEW.provenance_node_id
            AND relation='handoff_source'
        )
      THEN RAISE(ABORT, 'session handoff provenance is incomplete') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_memory_lifecycle_events_no_update
    BEFORE UPDATE ON memory_lifecycle_events
    BEGIN
      SELECT RAISE(ABORT, 'memory lifecycle events are append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_memory_lifecycle_events_no_delete
    BEFORE DELETE ON memory_lifecycle_events
    BEGIN
      SELECT RAISE(ABORT, 'memory lifecycle events are append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_memory_lifecycle_events_scope
    AFTER INSERT ON memory_lifecycle_events
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM memories AS memory
        WHERE memory.id=NEW.memory_id AND memory.owner_id=NEW.owner_id
          AND NEW.version=memory.lifecycle_version
          AND NEW.to_status=memory.status
          AND NEW.to_validity_state=memory.validity_state
          AND NEW.reason_code=memory.lifecycle_reason_code
          AND NEW.expires_at_after IS memory.expires_at
          AND (
            (NEW.event_type='legacy_import' AND NEW.version=1
             AND NEW.from_status IS NULL AND NEW.from_validity_state IS NULL
             AND (
               (NEW.to_validity_state='legacy_unverified'
                AND NEW.reason_code='legacy_unverified')
               OR (NEW.to_validity_state='valid'
                   AND NEW.reason_code='legacy_exact_source')
             ))
            OR (NEW.event_type='proposed' AND NEW.from_status IS NULL
                AND NEW.from_validity_state IS NULL AND NEW.to_status='proposed'
                AND NEW.to_validity_state='valid')
            OR (NEW.event_type='confirmed' AND NEW.from_status='proposed'
                AND NEW.to_status='confirmed')
            OR (NEW.event_type='reinforced' AND NEW.from_status='confirmed'
                AND NEW.to_status='confirmed')
            OR (NEW.event_type='review_required'
                AND NEW.to_validity_state='review_required')
            OR (NEW.event_type='invalidated' AND NEW.to_validity_state='invalid')
            OR (NEW.event_type='archived' AND NEW.to_status='archived'
                AND NEW.from_status IN ('proposed', 'confirmed'))
            OR (NEW.event_type='restored' AND NEW.from_status IN ('archived', 'expired')
                AND NEW.to_status IN ('proposed', 'confirmed')
                AND NEW.to_validity_state='valid')
            OR (NEW.event_type='expired' AND NEW.from_status='confirmed'
                AND NEW.to_status='expired')
            OR (NEW.event_type='superseded'
                AND NEW.from_status IN ('proposed', 'confirmed')
                AND NEW.to_status='superseded')
          )
          AND (NEW.source_run_id IS NULL OR EXISTS (
            SELECT 1 FROM agent_runs AS run
            WHERE run.id=NEW.source_run_id AND run.owner_id=NEW.owner_id
              AND (
                (memory.scope='global' AND run.plan_id IS NULL)
                OR (memory.scope='plan'
                    AND CAST(run.plan_id AS TEXT)=memory.scope_id)
                OR (memory.scope='session' AND run.session_id=memory.scope_id)
              )
          ))
          AND (NEW.source_message_id IS NULL OR EXISTS (
            SELECT 1 FROM chat_messages AS message
            JOIN sessions AS session ON session.id=message.session_id
            WHERE message.id=NEW.source_message_id AND session.owner_id=NEW.owner_id
              AND (
                (memory.scope='global' AND session.plan_id IS NULL)
                OR (memory.scope='plan'
                 AND CAST(session.plan_id AS TEXT)=memory.scope_id)
                OR (memory.scope='session' AND session.id=memory.scope_id)
              )
          ))
      ) THEN RAISE(ABORT, 'memory lifecycle fact or scope mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_memories_scope
    AFTER INSERT ON memories
    BEGIN
      SELECT CASE WHEN NOT (
        EXISTS (SELECT 1 FROM owners WHERE id=NEW.owner_id)
        AND (
          (NEW.scope='global' AND NEW.scope_id IS NULL)
          OR (NEW.scope='plan' AND EXISTS (
            SELECT 1 FROM plans
            WHERE CAST(id AS TEXT)=NEW.scope_id AND owner_id=NEW.owner_id
          ))
          OR (NEW.scope='session' AND EXISTS (
            SELECT 1 FROM sessions
            WHERE id=NEW.scope_id AND owner_id=NEW.owner_id
          ))
        )
      ) THEN RAISE(ABORT, 'memory owner or scope mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_memories_identity
    BEFORE UPDATE ON memories
    WHEN NEW.owner_id<>OLD.owner_id OR NEW.scope<>OLD.scope
      OR NEW.scope_id IS NOT OLD.scope_id OR NEW.layer<>OLD.layer
      OR NEW.content<>OLD.content OR NEW.content_hash<>OLD.content_hash
      OR NEW.source_type<>OLD.source_type OR NEW.source_id IS NOT OLD.source_id
      OR NEW.supersedes_id IS NOT OLD.supersedes_id
      OR NEW.created_at<>OLD.created_at
    BEGIN
      SELECT RAISE(ABORT, 'memory identity and content are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_memories_provenance_digest
    BEFORE UPDATE OF provenance_digest ON memories
    WHEN NEW.provenance_digest<>OLD.provenance_digest AND NOT (
      length(NEW.provenance_digest)=64
      AND NEW.provenance_node_id IS NOT OLD.provenance_node_id
    )
    BEGIN
      SELECT RAISE(ABORT, 'memory provenance digest requires a new version pointer');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_memories_superseded_link
    BEFORE UPDATE OF superseded_by_id ON memories
    WHEN NEW.superseded_by_id IS NOT OLD.superseded_by_id AND NOT (
      OLD.superseded_by_id IS NULL AND NEW.superseded_by_id IS NOT NULL
      AND NEW.status='superseded'
      AND EXISTS (
        SELECT 1 FROM memories AS successor
        WHERE successor.id=NEW.superseded_by_id
          AND successor.owner_id=NEW.owner_id
          AND successor.supersedes_id=NEW.id
          AND successor.scope=NEW.scope AND successor.scope_id IS NEW.scope_id
          AND successor.layer=NEW.layer
      )
    )
    BEGIN
      SELECT RAISE(ABORT, 'memory supersession link is immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_memories_provenance_insert
    AFTER INSERT ON memories
    WHEN NEW.provenance_node_id IS NOT NULL
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM provenance_nodes AS node
        WHERE node.id=NEW.provenance_node_id AND node.owner_id=NEW.owner_id
          AND node.kind='memory' AND node.entity_key=CAST(NEW.id AS TEXT)
          AND node.entity_version=NEW.lifecycle_version
          AND node.content_digest=NEW.content_hash
          AND (
            (NEW.scope='global' AND node.plan_id IS NULL AND node.session_id IS NULL)
            OR (NEW.scope='plan' AND CAST(node.plan_id AS TEXT)=NEW.scope_id
                AND node.session_id IS NULL)
            OR (NEW.scope='session' AND node.session_id=NEW.scope_id
                AND node.plan_id IS (
                  SELECT plan_id FROM sessions WHERE id=NEW.scope_id
                ))
          )
      ) THEN RAISE(ABORT, 'memory provenance pointer mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_memories_provenance_update
    AFTER UPDATE OF provenance_node_id, lifecycle_version ON memories
    WHEN NEW.provenance_node_id IS NOT NULL AND (
      NEW.provenance_node_id IS NOT OLD.provenance_node_id
      OR NEW.lifecycle_version<>OLD.lifecycle_version
    )
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM provenance_nodes AS node
        WHERE node.id=NEW.provenance_node_id AND node.owner_id=NEW.owner_id
          AND node.kind='memory' AND node.entity_key=CAST(NEW.id AS TEXT)
          AND node.entity_version=NEW.lifecycle_version
          AND node.content_digest=NEW.content_hash
          AND (
            (NEW.scope='global' AND node.plan_id IS NULL AND node.session_id IS NULL)
            OR (NEW.scope='plan' AND CAST(node.plan_id AS TEXT)=NEW.scope_id
                AND node.session_id IS NULL)
            OR (NEW.scope='session' AND node.session_id=NEW.scope_id
                AND node.plan_id IS (
                  SELECT plan_id FROM sessions WHERE id=NEW.scope_id
                ))
          )
      ) THEN RAISE(ABORT, 'memory provenance pointer mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_memories_no_delete
    BEFORE DELETE ON memories
    BEGIN
      SELECT RAISE(ABORT, 'memories are durable lifecycle facts');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_memories_semantic_version
    BEFORE UPDATE ON memories
    WHEN NOT (
      (
        NEW.status=OLD.status
        AND NEW.archived_from_status IS OLD.archived_from_status
        AND NEW.archived_reason=OLD.archived_reason
        AND NEW.lifecycle_reason_code=OLD.lifecycle_reason_code
        AND NEW.validity_state=OLD.validity_state
        AND NEW.invalidated_at IS OLD.invalidated_at
        AND NEW.invalidation_reason=OLD.invalidation_reason
        AND NEW.expires_at IS OLD.expires_at AND NEW.confidence=OLD.confidence
        AND NEW.lifecycle_version=OLD.lifecycle_version
      )
      OR
      (
        (
          NEW.status<>OLD.status
          OR NEW.archived_from_status IS NOT OLD.archived_from_status
          OR NEW.archived_reason<>OLD.archived_reason
          OR NEW.lifecycle_reason_code<>OLD.lifecycle_reason_code
          OR NEW.validity_state<>OLD.validity_state
          OR NEW.invalidated_at IS NOT OLD.invalidated_at
          OR NEW.invalidation_reason<>OLD.invalidation_reason
          OR NEW.expires_at IS NOT OLD.expires_at OR NEW.confidence<>OLD.confidence
          OR NEW.provenance_node_id IS NOT OLD.provenance_node_id
          OR NEW.provenance_digest<>OLD.provenance_digest
        )
        AND NEW.lifecycle_version=OLD.lifecycle_version+1
      )
    )
    BEGIN
      SELECT RAISE(ABORT, 'memory semantic update requires next lifecycle version');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_context_snapshot_blocks_no_update
    BEFORE UPDATE ON context_snapshot_blocks
    BEGIN
      SELECT RAISE(ABORT, 'context snapshot blocks are append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_context_snapshot_blocks_no_delete
    BEFORE DELETE ON context_snapshot_blocks
    BEGIN
      SELECT RAISE(ABORT, 'context snapshot blocks are append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_context_snapshot_blocks_scope
    AFTER INSERT ON context_snapshot_blocks
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM context_snapshots AS snapshot
        WHERE snapshot.id=NEW.snapshot_id AND snapshot.owner_id=NEW.owner_id
          AND EXISTS (
            SELECT 1 FROM provenance_nodes AS source
            WHERE source.id=NEW.source_node_id AND source.owner_id=NEW.owner_id
              AND (
                source.plan_id IS NULL OR source.plan_id IS snapshot.plan_id
                OR (snapshot.plan_id IS NULL AND source.kind='plan_summary')
              )
              AND (source.session_id IS NULL OR source.session_id IS snapshot.session_id)
          )
      ) THEN RAISE(ABORT, 'context snapshot block scope mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_context_snapshots_scope
    AFTER INSERT ON context_snapshots
    BEGIN
      SELECT CASE WHEN NOT (
        (NEW.plan_id IS NULL OR EXISTS (
          SELECT 1 FROM plans WHERE id=NEW.plan_id AND owner_id=NEW.owner_id
        ))
        AND (NEW.session_id IS NULL OR EXISTS (
          SELECT 1 FROM sessions
          WHERE id=NEW.session_id AND owner_id=NEW.owner_id
            AND plan_id IS NEW.plan_id
        ))
        AND (NEW.run_id IS NULL OR EXISTS (
          SELECT 1 FROM agent_runs
          WHERE id=NEW.run_id AND owner_id=NEW.owner_id
            AND plan_id IS NEW.plan_id AND session_id IS NEW.session_id
        ))
      ) THEN RAISE(ABORT, 'context snapshot scope mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_context_snapshots_lifecycle
    BEFORE UPDATE ON context_snapshots
    WHEN NOT (
      OLD.validity_state='building' AND NEW.validity_state='valid'
      AND NEW.owner_id=OLD.owner_id AND NEW.plan_id IS OLD.plan_id
      AND NEW.session_id IS OLD.session_id AND NEW.run_id IS OLD.run_id
      AND NEW.markdown=OLD.markdown AND NEW.source_manifest=OLD.source_manifest
      AND NEW.dropped_source_manifest=OLD.dropped_source_manifest
      AND NEW.budget_breakdown=OLD.budget_breakdown
      AND NEW.estimated_tokens=OLD.estimated_tokens
      AND NEW.context_generation=OLD.context_generation
      AND NEW.snapshot_version=OLD.snapshot_version
      AND NEW.assembler_version=OLD.assembler_version
      AND NEW.context_digest=OLD.context_digest AND NEW.source_digest=OLD.source_digest
      AND NEW.invalidated_at IS OLD.invalidated_at
      AND NEW.invalidation_reason=OLD.invalidation_reason
      AND NEW.created_at=OLD.created_at
      OR
      OLD.validity_state IN ('valid', 'legacy_unverified') AND NEW.validity_state='invalid'
      AND NEW.invalidated_at IS NOT NULL AND length(NEW.invalidation_reason)>0
      AND NEW.owner_id=OLD.owner_id AND NEW.plan_id IS OLD.plan_id
      AND NEW.session_id IS OLD.session_id AND NEW.run_id IS OLD.run_id
      AND NEW.markdown=OLD.markdown AND NEW.source_manifest=OLD.source_manifest
      AND NEW.dropped_source_manifest=OLD.dropped_source_manifest
      AND NEW.budget_breakdown=OLD.budget_breakdown
      AND NEW.estimated_tokens=OLD.estimated_tokens
      AND NEW.context_generation=OLD.context_generation
      AND NEW.snapshot_version=OLD.snapshot_version
      AND NEW.assembler_version=OLD.assembler_version
      AND NEW.context_digest=OLD.context_digest AND NEW.source_digest=OLD.source_digest
      AND NEW.provenance_node_id IS OLD.provenance_node_id
      AND NEW.created_at=OLD.created_at
    )
    BEGIN
      SELECT RAISE(ABORT, 'invalid context snapshot lifecycle transition');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_context_snapshots_no_delete
    BEFORE DELETE ON context_snapshots
    BEGIN
      SELECT RAISE(ABORT, 'context snapshots are immutable audit facts');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_context_snapshots_finalize
    AFTER UPDATE OF validity_state ON context_snapshots
    WHEN NEW.validity_state='valid'
    BEGIN
      SELECT CASE WHEN NEW.provenance_node_id IS NULL
        OR length(NEW.context_digest)<>64 OR length(NEW.source_digest)<>64
        OR NOT json_valid(NEW.source_manifest)
        OR json_type(NEW.source_manifest)<>'array'
        OR NOT json_valid(NEW.dropped_source_manifest)
        OR json_type(NEW.dropped_source_manifest)<>'array'
        OR NOT json_valid(NEW.budget_breakdown)
        OR json_type(NEW.budget_breakdown)<>'object'
        OR json_type(NEW.budget_breakdown, '$.model_context_window') NOT IN ('integer', 'real')
        OR json_type(NEW.budget_breakdown, '$.context_token_budget') NOT IN ('integer', 'real')
        OR json_type(NEW.budget_breakdown, '$.system_prompt_tokens') NOT IN ('integer', 'real')
        OR json_type(NEW.budget_breakdown, '$.tool_schema_tokens') NOT IN ('integer', 'real')
        OR json_type(NEW.budget_breakdown, '$.context_tokens') NOT IN ('integer', 'real')
        OR json_type(NEW.budget_breakdown, '$.output_reserve_tokens') NOT IN ('integer', 'real')
        OR json_type(NEW.budget_breakdown, '$.tool_result_reserve_tokens') NOT IN ('integer', 'real')
        OR json_type(NEW.budget_breakdown, '$.total_tokens') NOT IN ('integer', 'real')
        OR json_array_length(NEW.source_manifest) <> (
          SELECT count(*) FROM context_snapshot_blocks
          WHERE snapshot_id=NEW.id AND disposition='retained'
        )
        OR json_array_length(NEW.dropped_source_manifest) <> (
          SELECT count(*) FROM context_snapshot_blocks
          WHERE snapshot_id=NEW.id AND disposition='dropped'
        )
        OR EXISTS (
          SELECT 1 FROM json_each(NEW.source_manifest) AS manifest
          WHERE NOT EXISTS (
            SELECT 1 FROM context_snapshot_blocks AS block
            WHERE block.snapshot_id=NEW.id AND block.disposition='retained'
              AND block.ordinal=CAST(manifest.key AS INTEGER)
              AND block.source_type=json_extract(manifest.value, '$.type')
              AND block.source_id=CAST(json_extract(manifest.value, '$.id') AS TEXT)
              AND json_extract(block.metadata, '$.block_id')=
                  json_extract(manifest.value, '$.block_id')
              AND block.block_type=json_extract(manifest.value, '$.block_type')
              AND block.token_count=json_extract(manifest.value, '$.estimated_tokens')
              AND block.priority=json_extract(manifest.value, '$.priority')
              AND block.reason_code=''
              AND json_type(manifest.value, '$.reason_code') IS NULL
          )
        )
        OR EXISTS (
          SELECT 1 FROM json_each(NEW.dropped_source_manifest) AS manifest
          WHERE NOT EXISTS (
            SELECT 1 FROM context_snapshot_blocks AS block
            WHERE block.snapshot_id=NEW.id AND block.disposition='dropped'
              AND block.ordinal=CAST(manifest.key AS INTEGER)
              AND block.source_type=json_extract(manifest.value, '$.type')
              AND block.source_id=CAST(json_extract(manifest.value, '$.id') AS TEXT)
              AND json_extract(block.metadata, '$.block_id')=
                  json_extract(manifest.value, '$.block_id')
              AND block.block_type=json_extract(manifest.value, '$.block_type')
              AND block.token_count=json_extract(manifest.value, '$.estimated_tokens')
              AND block.priority=json_extract(manifest.value, '$.priority')
              AND block.reason_code=json_extract(manifest.value, '$.reason_code')
              AND length(block.reason_code)>0
          )
        )
        OR EXISTS (
          SELECT 1
          FROM context_snapshot_blocks AS block
          WHERE block.snapshot_id=NEW.id
            AND NOT EXISTS (
              SELECT 1
              FROM provenance_nodes AS source
              JOIN provenance_edges AS edge
                ON edge.source_node_id=source.id
               AND edge.target_node_id=NEW.provenance_node_id
              WHERE source.id=block.source_node_id
                AND source.owner_id=NEW.owner_id
                AND source.entity_version=block.source_version
                AND source.content_digest=block.source_digest
                AND edge.owner_id=NEW.owner_id
                AND edge.ordinal=block.ordinal
                AND edge.relation=CASE block.disposition
                  WHEN 'retained' THEN 'context_retained'
                  ELSE 'context_dropped'
                END
                AND edge.disposition=block.disposition
                AND edge.reason_code=block.reason_code
                AND edge.token_count=block.token_count
                AND json_extract(edge.metadata, '$.block_id')=
                    json_extract(block.metadata, '$.block_id')
                AND json_extract(edge.metadata, '$.block_type')=block.block_type
                AND json_extract(edge.metadata, '$.block_digest')=block.block_digest
            )
        )
        OR EXISTS (
          SELECT 1
          FROM provenance_edges AS edge
          WHERE edge.target_node_id=NEW.provenance_node_id
            AND edge.relation IN ('context_retained', 'context_dropped')
            AND NOT EXISTS (
              SELECT 1 FROM context_snapshot_blocks AS block
              WHERE block.snapshot_id=NEW.id
                AND block.source_node_id=edge.source_node_id
                AND block.ordinal=edge.ordinal
                AND edge.relation=CASE block.disposition
                  WHEN 'retained' THEN 'context_retained'
                  ELSE 'context_dropped'
                END
                AND edge.disposition=block.disposition
                AND edge.reason_code=block.reason_code
                AND edge.token_count=block.token_count
                AND json_extract(edge.metadata, '$.block_id')=
                    json_extract(block.metadata, '$.block_id')
                AND json_extract(edge.metadata, '$.block_type')=block.block_type
                AND json_extract(edge.metadata, '$.block_digest')=block.block_digest
            )
        )
        OR NOT EXISTS (
          SELECT 1 FROM context_states
          WHERE owner_id=NEW.owner_id AND generation=NEW.context_generation
        )
        OR NOT EXISTS (
          SELECT 1 FROM provenance_nodes
          WHERE id=NEW.provenance_node_id AND owner_id=NEW.owner_id
            AND kind='context_snapshot' AND entity_key=CAST(NEW.id AS TEXT)
            AND entity_version=NEW.snapshot_version
            AND content_digest=NEW.context_digest
        )
      THEN RAISE(ABORT, 'context snapshot generation or provenance mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_proactive_decisions_scope
    AFTER INSERT ON proactive_decisions
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM agent_runs AS run
        WHERE run.id=NEW.source_run_id AND run.owner_id=NEW.owner_id
          AND run.plan_id IS NEW.plan_id
          AND (NEW.source_invocation_id IS NULL OR EXISTS (
            SELECT 1 FROM tool_invocations AS invocation
            WHERE invocation.id=NEW.source_invocation_id
              AND invocation.owner_id=NEW.owner_id
              AND invocation.run_id=NEW.source_run_id
          ))
      ) THEN RAISE(ABORT, 'proactive decision run scope mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_proactive_decisions_lifecycle
    BEFORE UPDATE ON proactive_decisions
    WHEN NOT (
      OLD.status='building' AND NEW.status='terminal'
      AND NEW.outcome IS NOT NULL AND NEW.decided_at IS NOT NULL
      AND NEW.owner_id=OLD.owner_id AND NEW.plan_id IS OLD.plan_id
      AND NEW.source_run_id=OLD.source_run_id
      AND NEW.source_invocation_id IS OLD.source_invocation_id
      AND NEW.candidate_key=OLD.candidate_key AND NEW.candidate_kind=OLD.candidate_kind
      AND NEW.candidate_payload=OLD.candidate_payload
      AND NEW.candidate_digest=OLD.candidate_digest
      AND NEW.source_watermark IS OLD.source_watermark
      AND NEW.source_projection_digest IS OLD.source_projection_digest
      AND NEW.policy_version=OLD.policy_version AND NEW.policy_digest IS OLD.policy_digest
      AND NEW.created_at=OLD.created_at
    )
    BEGIN
      SELECT RAISE(ABORT, 'invalid proactive decision lifecycle transition');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_proactive_decisions_no_delete
    BEFORE DELETE ON proactive_decisions
    BEGIN
      SELECT RAISE(ABORT, 'proactive decisions are immutable terminal facts');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_interventions_scope
    AFTER INSERT ON interventions
    BEGIN
      SELECT CASE WHEN NOT (
        EXISTS (SELECT 1 FROM sessions WHERE id=NEW.session_id AND owner_id=NEW.owner_id)
        AND (NEW.plan_id IS NULL OR EXISTS (
          SELECT 1 FROM plans WHERE id=NEW.plan_id AND owner_id=NEW.owner_id
        ))
        AND (NEW.source_run_id IS NULL OR EXISTS (
          SELECT 1 FROM agent_runs WHERE id=NEW.source_run_id
            AND owner_id=NEW.owner_id AND plan_id IS NEW.plan_id
        ))
        AND (NEW.proactive_decision_id IS NULL OR EXISTS (
          SELECT 1 FROM proactive_decisions
          WHERE id=NEW.proactive_decision_id AND owner_id=NEW.owner_id
            AND plan_id IS NEW.plan_id
            AND source_run_id IS NEW.source_run_id
        ))
        AND (NEW.source_invocation_id IS NULL OR EXISTS (
          SELECT 1 FROM tool_invocations
          WHERE id=NEW.source_invocation_id AND owner_id=NEW.owner_id
            AND run_id=NEW.source_run_id
        ))
        AND (NEW.canonical_message_id IS NULL OR EXISTS (
          SELECT 1 FROM chat_messages
          WHERE id=NEW.canonical_message_id AND session_id=NEW.session_id
            AND role='assistant' AND content=NEW.body
            AND length(content_hash)=64 AND run_id IS NEW.source_run_id
        ))
      ) THEN RAISE(ABORT, 'intervention scope mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_interventions_lifecycle
    BEFORE UPDATE ON interventions
    WHEN NOT (
      OLD.state='building' AND NEW.state IN ('building', 'active', 'legacy_unverified')
      AND NEW.owner_id=OLD.owner_id AND NEW.plan_id IS OLD.plan_id
      AND NEW.session_id=OLD.session_id AND NEW.source_run_id IS OLD.source_run_id
      AND NEW.source_invocation_id IS OLD.source_invocation_id
      AND (OLD.proactive_decision_id IS NULL
           OR NEW.proactive_decision_id IS OLD.proactive_decision_id)
      AND (OLD.canonical_message_id IS NULL
           OR NEW.canonical_message_id IS OLD.canonical_message_id)
      AND (OLD.provenance_node_id IS NULL
           OR NEW.provenance_node_id IS OLD.provenance_node_id)
      AND NEW.reply_token=OLD.reply_token AND NEW.title=OLD.title AND NEW.body=OLD.body
      AND NEW.content_digest=OLD.content_digest AND NEW.reason_code=OLD.reason_code
      AND NEW.created_at=OLD.created_at
      OR
      OLD.state='active' AND NEW.state IN ('active', 'replied', 'resolved', 'cancelled')
      AND NEW.owner_id=OLD.owner_id AND NEW.plan_id IS OLD.plan_id
      AND NEW.session_id=OLD.session_id AND NEW.source_run_id IS OLD.source_run_id
      AND NEW.source_invocation_id IS OLD.source_invocation_id
      AND NEW.proactive_decision_id IS OLD.proactive_decision_id
      AND NEW.canonical_message_id IS OLD.canonical_message_id
      AND NEW.reply_token=OLD.reply_token AND NEW.title=OLD.title AND NEW.body=OLD.body
      AND NEW.content_digest=OLD.content_digest AND NEW.reason_code=OLD.reason_code
      AND NEW.provenance_node_id IS OLD.provenance_node_id
      AND NEW.created_at=OLD.created_at
      OR
      OLD.state='replied' AND NEW.state IN ('replied', 'resolved', 'cancelled')
      AND NEW.owner_id=OLD.owner_id AND NEW.plan_id IS OLD.plan_id
      AND NEW.session_id=OLD.session_id AND NEW.source_run_id IS OLD.source_run_id
      AND NEW.source_invocation_id IS OLD.source_invocation_id
      AND NEW.proactive_decision_id IS OLD.proactive_decision_id
      AND NEW.canonical_message_id IS OLD.canonical_message_id
      AND NEW.reply_token=OLD.reply_token AND NEW.title=OLD.title AND NEW.body=OLD.body
      AND NEW.content_digest=OLD.content_digest AND NEW.reason_code=OLD.reason_code
      AND NEW.provenance_node_id IS OLD.provenance_node_id
      AND NEW.created_at=OLD.created_at
      OR
      OLD.state IN ('resolved', 'cancelled', 'legacy_unverified') AND NEW.state=OLD.state
      AND NEW.owner_id=OLD.owner_id AND NEW.plan_id IS OLD.plan_id
      AND NEW.session_id=OLD.session_id AND NEW.source_run_id IS OLD.source_run_id
      AND NEW.source_invocation_id IS OLD.source_invocation_id
      AND NEW.proactive_decision_id IS OLD.proactive_decision_id
      AND NEW.canonical_message_id IS OLD.canonical_message_id
      AND NEW.reply_token=OLD.reply_token AND NEW.title=OLD.title AND NEW.body=OLD.body
      AND NEW.content_digest=OLD.content_digest AND NEW.reason_code=OLD.reason_code
      AND NEW.provenance_node_id IS OLD.provenance_node_id
      AND NEW.created_at=OLD.created_at
    )
    BEGIN
      SELECT RAISE(ABORT, 'intervention identity is immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_interventions_update_scope
    AFTER UPDATE OF owner_id, plan_id, session_id, source_run_id,
                    source_invocation_id, canonical_message_id,
                    proactive_decision_id ON interventions
    BEGIN
      SELECT CASE WHEN NOT (
        EXISTS (SELECT 1 FROM sessions WHERE id=NEW.session_id AND owner_id=NEW.owner_id)
        AND (NEW.plan_id IS NULL OR EXISTS (
          SELECT 1 FROM plans WHERE id=NEW.plan_id AND owner_id=NEW.owner_id
        ))
        AND (NEW.source_run_id IS NULL OR EXISTS (
          SELECT 1 FROM agent_runs WHERE id=NEW.source_run_id
            AND owner_id=NEW.owner_id AND plan_id IS NEW.plan_id
        ))
        AND (NEW.proactive_decision_id IS NULL OR EXISTS (
          SELECT 1 FROM proactive_decisions
          WHERE id=NEW.proactive_decision_id AND owner_id=NEW.owner_id
            AND plan_id IS NEW.plan_id
            AND source_run_id IS NEW.source_run_id
        ))
        AND (NEW.source_invocation_id IS NULL OR EXISTS (
          SELECT 1 FROM tool_invocations
          WHERE id=NEW.source_invocation_id AND owner_id=NEW.owner_id
            AND run_id=NEW.source_run_id
        ))
        AND (NEW.canonical_message_id IS NULL OR EXISTS (
          SELECT 1 FROM chat_messages
          WHERE id=NEW.canonical_message_id AND session_id=NEW.session_id
            AND role='assistant' AND content=NEW.body
            AND length(content_hash)=64 AND run_id IS NEW.source_run_id
        ))
      ) THEN RAISE(ABORT, 'intervention scope mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_interventions_no_delete
    BEFORE DELETE ON interventions
    BEGIN
      SELECT RAISE(ABORT, 'interventions are durable facts');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_notifications_intervention_scope
    AFTER INSERT ON notifications
    WHEN NEW.intervention_id IS NOT NULL
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM interventions
        WHERE id=NEW.intervention_id AND owner_id=NEW.owner_id
          AND session_id IS NEW.session_id AND plan_id IS NEW.plan_id
          AND title=NEW.title AND body=NEW.body
          AND reply_token=NEW.reply_token
      ) THEN RAISE(ABORT, 'notification intervention scope mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_notifications_intervention_update_scope
    AFTER UPDATE OF intervention_id, owner_id, session_id, plan_id, title, body,
      reply_token ON notifications
    WHEN NEW.intervention_id IS NOT NULL
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM interventions
        WHERE id=NEW.intervention_id AND owner_id=NEW.owner_id
          AND session_id IS NEW.session_id AND plan_id IS NEW.plan_id
          AND title=NEW.title AND body=NEW.body
          AND reply_token=NEW.reply_token
      ) THEN RAISE(ABORT, 'notification intervention scope mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_notifications_linked_identity
    BEFORE UPDATE ON notifications
    WHEN OLD.intervention_id IS NOT NULL AND (
      NEW.intervention_id IS NOT OLD.intervention_id OR NEW.owner_id<>OLD.owner_id
      OR NEW.session_id IS NOT OLD.session_id OR NEW.plan_id IS NOT OLD.plan_id
      OR NEW.title<>OLD.title OR NEW.body<>OLD.body
      OR NEW.reply_token<>OLD.reply_token
      OR NEW.channel<>OLD.channel OR NEW.delivery_generation<>OLD.delivery_generation
      OR NEW.legacy_unlinked<>OLD.legacy_unlinked
    )
    BEGIN
      SELECT RAISE(ABORT, 'linked notification identity is immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_chat_messages_reply_scope
    AFTER INSERT ON chat_messages
    WHEN NEW.reply_to_intervention_id IS NOT NULL
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM interventions AS intervention
        JOIN sessions AS session ON session.id=NEW.session_id
        WHERE intervention.id=NEW.reply_to_intervention_id
          AND intervention.owner_id=session.owner_id
          AND intervention.session_id=NEW.session_id
      ) THEN RAISE(ABORT, 'message intervention reply scope mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_chat_messages_reply_update_scope
    AFTER UPDATE OF reply_to_intervention_id, session_id ON chat_messages
    WHEN NEW.reply_to_intervention_id IS NOT NULL
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM interventions AS intervention
        JOIN sessions AS session ON session.id=NEW.session_id
        WHERE intervention.id=NEW.reply_to_intervention_id
          AND intervention.owner_id=session.owner_id
          AND intervention.session_id=NEW.session_id
      ) THEN RAISE(ABORT, 'message intervention reply scope mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_chat_messages_reply_identity
    BEFORE UPDATE OF reply_to_intervention_id ON chat_messages
    WHEN OLD.reply_to_intervention_id IS NOT NULL
      AND NEW.reply_to_intervention_id IS NOT OLD.reply_to_intervention_id
    BEGIN
      SELECT RAISE(ABORT, 'message intervention reply target is immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_agent_runs_reply_scope
    AFTER INSERT ON agent_runs
    WHEN NEW.reply_to_intervention_id IS NOT NULL
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM interventions
        WHERE id=NEW.reply_to_intervention_id AND owner_id=NEW.owner_id
          AND session_id IS NEW.session_id AND plan_id IS NEW.plan_id
      ) THEN RAISE(ABORT, 'run intervention reply scope mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_agent_runs_reply_update_scope
    AFTER UPDATE OF reply_to_intervention_id, owner_id, session_id, plan_id ON agent_runs
    WHEN NEW.reply_to_intervention_id IS NOT NULL
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM interventions
        WHERE id=NEW.reply_to_intervention_id AND owner_id=NEW.owner_id
          AND session_id IS NEW.session_id AND plan_id IS NEW.plan_id
      ) THEN RAISE(ABORT, 'run intervention reply scope mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_agent_runs_reply_identity
    BEFORE UPDATE OF reply_to_intervention_id ON agent_runs
    WHEN OLD.reply_to_intervention_id IS NOT NULL
      AND NEW.reply_to_intervention_id IS NOT OLD.reply_to_intervention_id
    BEGIN
      SELECT RAISE(ABORT, 'run intervention reply target is immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_queued_messages_reply_scope
    AFTER INSERT ON queued_messages
    WHEN NEW.reply_to_intervention_id IS NOT NULL
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM interventions
        WHERE id=NEW.reply_to_intervention_id AND owner_id=NEW.owner_id
          AND session_id IS NEW.session_id AND plan_id IS NEW.plan_id
      ) THEN RAISE(ABORT, 'queued intervention reply scope mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_queued_messages_reply_update_scope
    AFTER UPDATE OF reply_to_intervention_id, owner_id, session_id, plan_id ON queued_messages
    WHEN NEW.reply_to_intervention_id IS NOT NULL
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM interventions
        WHERE id=NEW.reply_to_intervention_id AND owner_id=NEW.owner_id
          AND session_id IS NEW.session_id AND plan_id IS NEW.plan_id
      ) THEN RAISE(ABORT, 'queued intervention reply scope mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_queued_messages_reply_identity
    BEFORE UPDATE OF reply_to_intervention_id ON queued_messages
    WHEN OLD.reply_to_intervention_id IS NOT NULL
      AND NEW.reply_to_intervention_id IS NOT OLD.reply_to_intervention_id
    BEGIN
      SELECT RAISE(ABORT, 'queued intervention reply target is immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_inbound_mail_jobs_scope
    AFTER INSERT ON inbound_mail_jobs
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM interventions AS intervention
        WHERE intervention.id=NEW.intervention_id
          AND intervention.owner_id=NEW.owner_id
          AND intervention.session_id=NEW.session_id
          AND intervention.plan_id IS NEW.plan_id
          AND (NEW.source_notification_id IS NULL OR EXISTS (
            SELECT 1 FROM notifications
            WHERE id=NEW.source_notification_id
              AND intervention_id=NEW.intervention_id
          ))
      ) THEN RAISE(ABORT, 'inbound mail intervention scope mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_inbound_mail_jobs_ack_fence
    BEFORE UPDATE ON inbound_mail_jobs
    WHEN (
      NEW.ack_state<>OLD.ack_state OR NEW.ack_attempt<>OLD.ack_attempt
      OR NEW.ack_claim_token IS NOT OLD.ack_claim_token
      OR NEW.ack_claim_expires_at IS NOT OLD.ack_claim_expires_at
      OR NEW.acked_at IS NOT OLD.acked_at
    ) AND NOT (
      OLD.ack_state IN ('pending', 'failed') AND NEW.ack_state='claimed'
      AND NEW.ack_attempt=OLD.ack_attempt+1
      AND NEW.ack_claim_token IS NOT NULL AND NEW.ack_claim_expires_at IS NOT NULL
      AND julianday(NEW.ack_claim_expires_at) > julianday(CURRENT_TIMESTAMP)
      AND NEW.acked_at IS NULL
      OR
      OLD.ack_state='claimed' AND NEW.ack_state='claimed'
      AND julianday(OLD.ack_claim_expires_at) <= julianday(CURRENT_TIMESTAMP)
      AND NEW.ack_attempt=OLD.ack_attempt+1
      AND NEW.ack_claim_token IS NOT NULL
      AND NEW.ack_claim_token<>OLD.ack_claim_token
      AND NEW.ack_claim_expires_at IS NOT NULL
      AND julianday(NEW.ack_claim_expires_at) > julianday(CURRENT_TIMESTAMP)
      AND NEW.acked_at IS NULL
      OR
      OLD.ack_state='claimed' AND NEW.ack_state='acked'
      AND NEW.ack_attempt=OLD.ack_attempt
      AND NEW.ack_claim_token IS NULL AND NEW.ack_claim_expires_at IS NULL
      AND NEW.acked_at IS NOT NULL
      OR
      OLD.ack_state='claimed' AND NEW.ack_state='failed'
      AND NEW.ack_attempt=OLD.ack_attempt
      AND NEW.ack_claim_token IS NULL AND NEW.ack_claim_expires_at IS NULL
      AND NEW.acked_at IS NULL
    )
    BEGIN
      SELECT RAISE(ABORT, 'invalid inbound mail acknowledgement transition');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_inbound_mail_jobs_identity
    BEFORE UPDATE ON inbound_mail_jobs
    WHEN NEW.owner_id<>OLD.owner_id OR NEW.intervention_id<>OLD.intervention_id
      OR NEW.source_notification_id IS NOT OLD.source_notification_id
      OR NEW.session_id<>OLD.session_id OR NEW.plan_id IS NOT OLD.plan_id
      OR NEW.mailbox_key<>OLD.mailbox_key OR NEW.uidvalidity<>OLD.uidvalidity
      OR NEW.uid<>OLD.uid OR NEW.subject<>OLD.subject OR NEW.body<>OLD.body
      OR NEW.payload_digest<>OLD.payload_digest
      OR NEW.execution_mode<>OLD.execution_mode OR NEW.created_at<>OLD.created_at
    BEGIN
      SELECT RAISE(ABORT, 'inbound mail identity is immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_inbound_mail_jobs_update_scope
    AFTER UPDATE OF owner_id, intervention_id, source_notification_id,
                    session_id, plan_id ON inbound_mail_jobs
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM interventions AS intervention
        WHERE intervention.id=NEW.intervention_id
          AND intervention.owner_id=NEW.owner_id
          AND intervention.session_id=NEW.session_id
          AND intervention.plan_id IS NEW.plan_id
          AND (NEW.source_notification_id IS NULL OR EXISTS (
            SELECT 1 FROM notifications
            WHERE id=NEW.source_notification_id
              AND intervention_id=NEW.intervention_id
          ))
      ) THEN RAISE(ABORT, 'inbound mail intervention scope mismatch') END;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_inbound_mail_jobs_no_delete
    BEFORE DELETE ON inbound_mail_jobs
    BEGIN
      SELECT RAISE(ABORT, 'inbound mail jobs are durable receipts');
    END
    """,
)

SCHEMA_TRIGGER_SQL = SCHEMA_TRIGGER_SQL + H5_SCHEMA_TRIGGER_SQL


def install_schema_triggers(connection) -> None:
    """Install the exact trigger set used by migration and metadata fixtures."""

    for statement in SCHEMA_TRIGGER_SQL:
        connection.exec_driver_sql(statement)


@event.listens_for(Base.metadata, "after_create")
def _install_metadata_schema_triggers(_metadata, connection, **_kwargs) -> None:
    install_schema_triggers(connection)
