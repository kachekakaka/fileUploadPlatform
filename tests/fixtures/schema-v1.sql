-- Frozen Core schema from 89946424fa4476c4f517a35a8e910e48ead91bf2; synthetic test only.

CREATE TABLE schema_version (
	id VARCHAR NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id)
)

;

CREATE TABLE accounts (
	id VARCHAR NOT NULL,
	platform VARCHAR NOT NULL,
	reference VARCHAR NOT NULL,
	display_name VARCHAR NOT NULL,
	status VARCHAR NOT NULL,
	concurrency_limit INTEGER NOT NULL,
	PRIMARY KEY (id),
	CHECK (concurrency_limit > 0),
	UNIQUE (reference)
)

;

CREATE TABLE source_roots (
	id VARCHAR NOT NULL,
	directory VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (directory)
)

;

CREATE TABLE upload_works (
	id VARCHAR NOT NULL,
	root_id VARCHAR NOT NULL,
	manifest JSON NOT NULL,
	created_at FLOAT NOT NULL,
	updated_at FLOAT NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(root_id) REFERENCES source_roots (id)
)

;

CREATE TABLE upload_units (
	id VARCHAR NOT NULL,
	work_id VARCHAR NOT NULL,
	logical_id VARCHAR NOT NULL,
	mode VARCHAR NOT NULL,
	snapshot JSON NOT NULL,
	PRIMARY KEY (id),
	CHECK (mode IN ('multipart','independent')),
	FOREIGN KEY(work_id) REFERENCES upload_works (id)
)

;

CREATE TABLE assets (
	id VARCHAR NOT NULL,
	work_id VARCHAR NOT NULL,
	root_id VARCHAR NOT NULL,
	logical_id VARCHAR NOT NULL,
	relative_path VARCHAR NOT NULL,
	role VARCHAR NOT NULL,
	size INTEGER NOT NULL,
	mtime_ns INTEGER NOT NULL,
	PRIMARY KEY (id),
	CHECK (size >= 0),
	FOREIGN KEY(work_id) REFERENCES upload_works (id),
	FOREIGN KEY(root_id) REFERENCES source_roots (id)
)

;

CREATE TABLE upload_unit_assets (
	id VARCHAR NOT NULL,
	unit_id VARCHAR NOT NULL,
	asset_id VARCHAR NOT NULL,
	position INTEGER NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (unit_id, position),
	FOREIGN KEY(unit_id) REFERENCES upload_units (id),
	FOREIGN KEY(asset_id) REFERENCES assets (id)
)

;

CREATE TABLE upload_targets (
	id VARCHAR NOT NULL,
	unit_id VARCHAR NOT NULL,
	account_id VARCHAR NOT NULL,
	platform VARCHAR NOT NULL,
	enabled BOOLEAN NOT NULL,
	options JSON NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(unit_id) REFERENCES upload_units (id),
	FOREIGN KEY(account_id) REFERENCES accounts (id)
)

;

CREATE TABLE submission_ledger (
	id VARCHAR NOT NULL,
	work_id VARCHAR NOT NULL,
	unit_logical_id VARCHAR NOT NULL,
	mode VARCHAR NOT NULL,
	item_key VARCHAR NOT NULL,
	platform VARCHAR NOT NULL,
	account_id VARCHAR NOT NULL,
	current_task_id VARCHAR,
	round INTEGER NOT NULL,
	PRIMARY KEY (id),
	CHECK (round >= 0),
	UNIQUE (work_id, unit_logical_id, mode, item_key, platform, account_id),
	FOREIGN KEY(work_id) REFERENCES upload_works (id),
	FOREIGN KEY(account_id) REFERENCES accounts (id),
	FOREIGN KEY(current_task_id) REFERENCES tasks (id)
)

;

CREATE TABLE tasks (
	id VARCHAR NOT NULL,
	work_id VARCHAR NOT NULL,
	unit_id VARCHAR NOT NULL,
	target_id VARCHAR NOT NULL,
	ledger_id VARCHAR NOT NULL,
	account_id VARCHAR NOT NULL,
	previous_task_id VARCHAR,
	submission_round INTEGER NOT NULL,
	status VARCHAR NOT NULL,
	version INTEGER NOT NULL,
	snapshot JSON NOT NULL,
	execution_type VARCHAR NOT NULL,
	scheduled_at FLOAT NOT NULL,
	max_lateness_seconds INTEGER NOT NULL,
	retry_policy JSON NOT NULL,
	attempt_count INTEGER NOT NULL,
	next_attempt_at FLOAT NOT NULL,
	current_attempt_id VARCHAR,
	fence INTEGER NOT NULL,
	cancel_requested_at FLOAT,
	created_at FLOAT NOT NULL,
	updated_at FLOAT NOT NULL,
	completed_at FLOAT,
	PRIMARY KEY (id),
	UNIQUE (ledger_id, submission_round),
	CHECK (submission_round > 0 AND version > 0 AND attempt_count >= 0 AND fence >= 0 AND max_lateness_seconds >= 0),
	CHECK (execution_type IN ('immediate','scheduled')),
	CHECK (status IN ('QUEUED','CLAIMED','VALIDATING','RUNNING','RETRY_WAIT','RECOVERY_PENDING','UNCERTAIN','SUCCEEDED','FAILED','CANCELLED','EXPIRED')),
	FOREIGN KEY(work_id) REFERENCES upload_works (id),
	FOREIGN KEY(unit_id) REFERENCES upload_units (id),
	FOREIGN KEY(target_id) REFERENCES upload_targets (id),
	FOREIGN KEY(ledger_id) REFERENCES submission_ledger (id),
	FOREIGN KEY(account_id) REFERENCES accounts (id),
	FOREIGN KEY(previous_task_id) REFERENCES tasks (id),
	FOREIGN KEY(current_attempt_id) REFERENCES attempts (id)
)

;

CREATE TABLE task_assets (
	id VARCHAR NOT NULL,
	task_id VARCHAR NOT NULL,
	asset_id VARCHAR NOT NULL,
	position INTEGER NOT NULL,
	role VARCHAR NOT NULL,
	video_id VARCHAR,
	PRIMARY KEY (id),
	UNIQUE (task_id, position),
	FOREIGN KEY(task_id) REFERENCES tasks (id),
	FOREIGN KEY(asset_id) REFERENCES assets (id)
)

;

CREATE TABLE workers (
	id VARCHAR NOT NULL,
	boot_id VARCHAR NOT NULL,
	pid INTEGER NOT NULL,
	heartbeat_at FLOAT NOT NULL,
	status VARCHAR NOT NULL,
	PRIMARY KEY (id)
)

;

CREATE TABLE attempts (
	id VARCHAR NOT NULL,
	task_id VARCHAR NOT NULL,
	attempt_number INTEGER NOT NULL,
	worker_id VARCHAR NOT NULL,
	status VARCHAR NOT NULL,
	phase VARCHAR NOT NULL,
	execution_id VARCHAR NOT NULL,
	fence INTEGER NOT NULL,
	execution JSON NOT NULL,
	permission_at FLOAT,
	exit_confirmed BOOLEAN NOT NULL,
	host_sequence INTEGER NOT NULL,
	result JSON,
	error VARCHAR,
	recovery_owner VARCHAR,
	recovery_generation INTEGER NOT NULL,
	recovery_expires FLOAT,
	started_at FLOAT NOT NULL,
	heartbeat_at FLOAT NOT NULL,
	ended_at FLOAT,
	PRIMARY KEY (id),
	UNIQUE (task_id, attempt_number),
	CHECK (attempt_number > 0 AND fence > 0 AND host_sequence >= 0 AND recovery_generation >= 0),
	CHECK (status IN ('CREATED','STARTING','RUNNING','RETRYABLE_FAILURE','PERMANENT_FAILURE','SUCCEEDED','UNCERTAIN','CANCELLED')),
	FOREIGN KEY(task_id) REFERENCES tasks (id),
	FOREIGN KEY(worker_id) REFERENCES workers (id),
	UNIQUE (execution_id)
)

;

CREATE TABLE fencing_counters (
	id VARCHAR NOT NULL,
	scope VARCHAR NOT NULL,
	slot INTEGER NOT NULL,
	generation INTEGER NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (scope, slot),
	CHECK (generation > 0)
)

;

CREATE TABLE leases (
	id VARCHAR NOT NULL,
	scope VARCHAR NOT NULL,
	slot INTEGER NOT NULL,
	attempt_id VARCHAR NOT NULL,
	generation INTEGER NOT NULL,
	expires_at FLOAT NOT NULL,
	released_at FLOAT,
	PRIMARY KEY (id),
	CHECK (slot >= 0 AND generation > 0),
	FOREIGN KEY(attempt_id) REFERENCES attempts (id)
)

;

CREATE TABLE events (
	id VARCHAR NOT NULL,
	aggregate_id VARCHAR NOT NULL,
	sequence INTEGER NOT NULL,
	task_id VARCHAR,
	attempt_id VARCHAR,
	type VARCHAR NOT NULL,
	payload JSON NOT NULL,
	created_at FLOAT NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (aggregate_id, sequence),
	CHECK (sequence > 0),
	FOREIGN KEY(task_id) REFERENCES tasks (id),
	FOREIGN KEY(attempt_id) REFERENCES attempts (id)
)

;

CREATE TABLE outbox_events (
	id VARCHAR NOT NULL,
	event_id VARCHAR NOT NULL,
	published_at FLOAT,
	PRIMARY KEY (id),
	UNIQUE (event_id),
	FOREIGN KEY(event_id) REFERENCES events (id)
)

;

CREATE TABLE receipts (
	id VARCHAR NOT NULL,
	task_id VARCHAR NOT NULL,
	attempt_id VARCHAR NOT NULL,
	source VARCHAR NOT NULL,
	fake BOOLEAN NOT NULL,
	evidence JSON NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (task_id),
	FOREIGN KEY(task_id) REFERENCES tasks (id),
	FOREIGN KEY(attempt_id) REFERENCES attempts (id)
)

;

CREATE TABLE resolutions (
	id VARCHAR NOT NULL,
	task_id VARCHAR NOT NULL,
	attempt_id VARCHAR NOT NULL,
	conclusion VARCHAR NOT NULL,
	reason VARCHAR NOT NULL,
	evidence_refs JSON NOT NULL,
	source VARCHAR NOT NULL,
	created_at FLOAT NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(task_id) REFERENCES tasks (id),
	FOREIGN KEY(attempt_id) REFERENCES attempts (id)
)

;

CREATE TABLE idempotency_keys (
	id VARCHAR NOT NULL,
	scope VARCHAR NOT NULL,
	"key" VARCHAR NOT NULL,
	request VARCHAR NOT NULL,
	http_status INTEGER NOT NULL,
	response JSON NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (scope, "key")
)

;

CREATE TABLE schedules (
	id VARCHAR NOT NULL,
	version INTEGER NOT NULL,
	enabled BOOLEAN NOT NULL,
	config JSON NOT NULL,
	next_fire_at FLOAT,
	created_at FLOAT NOT NULL,
	PRIMARY KEY (id),
	CHECK (version > 0)
)

;

CREATE TABLE schedule_versions (
	id VARCHAR NOT NULL,
	schedule_id VARCHAR NOT NULL,
	version INTEGER NOT NULL,
	config JSON NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (schedule_id, version),
	FOREIGN KEY(schedule_id) REFERENCES schedules (id)
)

;

CREATE TABLE schedule_runs (
	id VARCHAR NOT NULL,
	schedule_id VARCHAR NOT NULL,
	version INTEGER NOT NULL,
	planned_at FLOAT NOT NULL,
	started_at FLOAT NOT NULL,
	first_missed_at FLOAT NOT NULL,
	merged_count INTEGER NOT NULL,
	config JSON NOT NULL,
	status VARCHAR NOT NULL,
	candidates JSON,
	owner VARCHAR,
	generation INTEGER NOT NULL,
	expires_at FLOAT,
	error VARCHAR,
	PRIMARY KEY (id),
	UNIQUE (schedule_id, version, planned_at),
	CHECK (status IN ('PENDING','DISPATCHING','COMPLETED','COMPLETED_WITH_ERRORS','SKIPPED')),
	FOREIGN KEY(schedule_id) REFERENCES schedules (id)
)

;

CREATE TABLE schedule_run_works (
	id VARCHAR NOT NULL,
	run_id VARCHAR NOT NULL,
	directory VARCHAR NOT NULL,
	work_id VARCHAR,
	status VARCHAR NOT NULL,
	error JSON,
	PRIMARY KEY (id),
	UNIQUE (run_id, directory),
	FOREIGN KEY(run_id) REFERENCES schedule_runs (id),
	FOREIGN KEY(work_id) REFERENCES upload_works (id)
)

;

CREATE TABLE schedule_run_tasks (
	id VARCHAR NOT NULL,
	run_id VARCHAR NOT NULL,
	run_work_id VARCHAR NOT NULL,
	task_id VARCHAR NOT NULL,
	created BOOLEAN NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (run_work_id, task_id),
	FOREIGN KEY(run_id) REFERENCES schedule_runs (id),
	FOREIGN KEY(run_work_id) REFERENCES schedule_run_works (id),
	FOREIGN KEY(task_id) REFERENCES tasks (id)
)

;
CREATE UNIQUE INDEX one_active_attempt ON attempts (task_id) WHERE status IN ('CREATED', 'STARTING', 'RUNNING');
CREATE UNIQUE INDEX one_occupied_slot ON leases (scope, slot) WHERE released_at IS NULL;