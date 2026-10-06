CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS organisations (
    workspace_id TEXT NOT NULL,
    id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, id),
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS opportunities (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    duplicate_of TEXT,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (duplicate_of) REFERENCES opportunities(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS source_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    content_type TEXT NOT NULL,
    title TEXT NOT NULL,
    text_content TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    discovered_links_json TEXT NOT NULL,
    etag TEXT,
    last_modified TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE (source_id, content_hash)
);

CREATE TABLE IF NOT EXISTS opportunity_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    UNIQUE (opportunity_id, payload_hash),
    FOREIGN KEY (opportunity_id) REFERENCES opportunities(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    FOREIGN KEY (opportunity_id) REFERENCES opportunities(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS assessments (
    workspace_id TEXT NOT NULL,
    organisation_id TEXT NOT NULL,
    opportunity_id TEXT NOT NULL,
    assessed_at TEXT NOT NULL,
    eligibility_json TEXT NOT NULL,
    fit_json TEXT NOT NULL,
    PRIMARY KEY (workspace_id, organisation_id, opportunity_id, assessed_at),
    FOREIGN KEY (workspace_id, organisation_id)
        REFERENCES organisations(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (opportunity_id) REFERENCES opportunities(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS changes (
    change_id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL,
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    detected_at TEXT NOT NULL,
    source_id TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL,
    FOREIGN KEY (opportunity_id) REFERENCES opportunities(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS notification_queue (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    opportunity_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    workspace_id TEXT,
    organisation_id TEXT,
    payload_json TEXT NOT NULL,
    delivered_at TEXT,
    CHECK ((workspace_id IS NULL) = (organisation_id IS NULL)),
    FOREIGN KEY (opportunity_id) REFERENCES opportunities(id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, organisation_id)
        REFERENCES organisations(workspace_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_opportunities_source ON opportunities(source_id);
CREATE INDEX IF NOT EXISTS idx_opportunities_duplicate ON opportunities(duplicate_of);
CREATE INDEX IF NOT EXISTS idx_source_snapshots_source ON source_snapshots(source_id, last_seen_at);
CREATE INDEX IF NOT EXISTS idx_changes_opportunity_time ON changes(opportunity_id, detected_at);
CREATE INDEX IF NOT EXISTS idx_notifications_workspace ON notification_queue(workspace_id, created_at);
