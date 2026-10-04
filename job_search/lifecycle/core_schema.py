"""Application obligations and immutable, reviewed lifecycle detail history."""

SCHEMA = """
CREATE TABLE lifecycle_tasks (
 task_id TEXT PRIMARY KEY,
 application_id TEXT NOT NULL REFERENCES applications(application_id),
 kind TEXT NOT NULL CHECK(kind IN ('reply','send_availability','complete_assessment','attend_interview','send_document','offer_decision','follow_up')),
 owner TEXT NOT NULL CHECK(owner IN ('applicant','employer','unknown')),
 status TEXT NOT NULL CHECK(status IN ('open','completed','cancelled','superseded')),
 note TEXT NOT NULL, due_at TEXT, snoozed_until TEXT, source_time TEXT NOT NULL,
 evidence_id TEXT REFERENCES mail_evidence(evidence_id),
 completed_evidence_id TEXT REFERENCES mail_evidence(evidence_id),
 supersedes_task_id TEXT REFERENCES lifecycle_tasks(task_id),
 action_id TEXT REFERENCES action_proposals(action_id),
 policy_version TEXT NOT NULL, revision_no INTEGER NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX lifecycle_tasks_application ON lifecycle_tasks(application_id,status,due_at);
CREATE TABLE lifecycle_task_revisions (
 revision_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES lifecycle_tasks(task_id),
 revision_no INTEGER NOT NULL, operation TEXT NOT NULL, state_json TEXT NOT NULL,
 actor_kind TEXT NOT NULL, source_kind TEXT NOT NULL, source_ref TEXT NOT NULL,
 occurred_at TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(task_id,revision_no)
);
CREATE TABLE lifecycle_details (
 detail_id TEXT PRIMARY KEY, application_id TEXT NOT NULL REFERENCES applications(application_id),
 kind TEXT NOT NULL CHECK(kind IN ('assessment','offer')), status TEXT NOT NULL,
 details_json TEXT NOT NULL, evidence_id TEXT REFERENCES mail_evidence(evidence_id),
 source_time TEXT NOT NULL, task_id TEXT REFERENCES lifecycle_tasks(task_id),
 revision_no INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE lifecycle_detail_revisions (
 revision_id TEXT PRIMARY KEY, detail_id TEXT NOT NULL REFERENCES lifecycle_details(detail_id),
 revision_no INTEGER NOT NULL, state_json TEXT NOT NULL,
 actor_kind TEXT NOT NULL, source_kind TEXT NOT NULL, source_ref TEXT NOT NULL,
 occurred_at TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(detail_id,revision_no)
);
CREATE TABLE lifecycle_correction_proposals (
 proposal_id TEXT PRIMARY KEY, application_id TEXT NOT NULL REFERENCES applications(application_id),
 kind TEXT NOT NULL, payload_json TEXT NOT NULL, evidence_id TEXT REFERENCES mail_evidence(evidence_id),
 status TEXT NOT NULL CHECK(status IN ('pending','accepted','rejected')),
 actor_kind TEXT NOT NULL, source_kind TEXT NOT NULL, source_ref TEXT NOT NULL,
 created_at TEXT NOT NULL, decided_at TEXT, base_state_json TEXT NOT NULL
);
CREATE TABLE lifecycle_correction_decisions (
 decision_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL UNIQUE REFERENCES lifecycle_correction_proposals(proposal_id),
 decision TEXT NOT NULL, actor_kind TEXT NOT NULL, reason TEXT NOT NULL, result_json TEXT NOT NULL,
 decided_at TEXT NOT NULL
);
CREATE TRIGGER lifecycle_correction_input_immutable BEFORE UPDATE ON lifecycle_correction_proposals
WHEN OLD.proposal_id IS NOT NEW.proposal_id OR OLD.application_id IS NOT NEW.application_id
 OR OLD.kind IS NOT NEW.kind OR OLD.payload_json IS NOT NEW.payload_json
 OR OLD.evidence_id IS NOT NEW.evidence_id OR OLD.actor_kind IS NOT NEW.actor_kind
 OR OLD.source_kind IS NOT NEW.source_kind OR OLD.source_ref IS NOT NEW.source_ref
 OR OLD.created_at IS NOT NEW.created_at OR OLD.base_state_json IS NOT NEW.base_state_json
BEGIN SELECT RAISE(ABORT,'correction proposal input is immutable'); END;
"""
for _table in ("lifecycle_task_revisions", "lifecycle_detail_revisions", "lifecycle_correction_decisions"):
    for _operation in ("UPDATE", "DELETE"):
        SCHEMA += f"CREATE TRIGGER {_table}_no_{_operation.lower()} BEFORE {_operation} ON {_table} BEGIN SELECT RAISE(ABORT,'lifecycle history is append-only'); END;\n"

SCHEMA += """
CREATE TABLE lifecycle_follow_up_policies (
 application_id TEXT PRIMARY KEY REFERENCES applications(application_id),
 after_days INTEGER CHECK(after_days BETWEEN 1 AND 90),
 policy_version INTEGER NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE lifecycle_follow_up_policy_revisions (
 revision_id TEXT PRIMARY KEY, application_id TEXT NOT NULL REFERENCES applications(application_id),
 after_days INTEGER, policy_version INTEGER NOT NULL, actor_kind TEXT NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(application_id,policy_version)
);
CREATE TRIGGER lifecycle_follow_up_policy_no_update BEFORE UPDATE ON lifecycle_follow_up_policy_revisions
BEGIN SELECT RAISE(ABORT,'follow-up policy history is append-only'); END;
CREATE TRIGGER lifecycle_follow_up_policy_no_delete BEFORE DELETE ON lifecycle_follow_up_policy_revisions
BEGIN SELECT RAISE(ABORT,'follow-up policy history is append-only'); END;
CREATE TABLE lifecycle_event_tasks (
 event_id TEXT PRIMARY KEY REFERENCES application_events(event_id),
 task_id TEXT NOT NULL REFERENCES lifecycle_tasks(task_id)
);
CREATE TABLE lifecycle_follow_up_observations (
 observation_id TEXT NOT NULL, policy_version INTEGER NOT NULL,
 task_id TEXT NOT NULL REFERENCES lifecycle_tasks(task_id), PRIMARY KEY(observation_id,policy_version)
);
"""
