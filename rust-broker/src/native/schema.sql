-- Schema-2 DDL mechanically extracted from the existing initialiser.
CREATE TABLE cortex_detail (
    memory_id BLOB PRIMARY KEY REFERENCES cortex_memory(memory_id) ON DELETE CASCADE,
    detail_blob BLOB NOT NULL,
    checksum_sha256 BLOB NOT NULL
);
CREATE TABLE cortex_memory (
    memory_pk INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id BLOB UNIQUE NOT NULL CHECK(length(memory_id)=16),
    memory_type INTEGER NOT NULL,
    scope TEXT NOT NULL,
    payload_blob BLOB NOT NULL,
    payload_raw_bytes INTEGER NOT NULL,
    payload_stored_bytes INTEGER NOT NULL,
    importance INTEGER NOT NULL CHECK(importance BETWEEN 0 AND 255),
    confidence INTEGER NOT NULL CHECK(confidence BETWEEN 0 AND 255),
    sensitivity INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT,
    status INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0,
    supersedes_id BLOB,
    checksum_sha256 BLOB NOT NULL CHECK(length(checksum_sha256)=32), content_fingerprint BLOB, detail_sha256 BLOB, observed_at TEXT, valid_from TEXT, valid_to TEXT, source_hash TEXT, confidence_reason TEXT NOT NULL DEFAULT '', claim_id TEXT, prior_version_id BLOB, stage_id BLOB, record_checksum BLOB,
    FOREIGN KEY(supersedes_id) REFERENCES cortex_memory(memory_id)
);
CREATE TABLE cortex_term (
    memory_pk INTEGER NOT NULL,
    term_hash BLOB NOT NULL CHECK(length(term_hash)=8),
    PRIMARY KEY(memory_pk, term_hash),
    FOREIGN KEY(memory_pk) REFERENCES cortex_memory(memory_pk) ON DELETE CASCADE
) WITHOUT ROWID;
CREATE TABLE cortex_tombstone (
    memory_id BLOB PRIMARY KEY, scope TEXT NOT NULL, removed_at TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE cortex_verbatim (
    archive_id BLOB PRIMARY KEY CHECK(length(archive_id)=16),
    scope TEXT NOT NULL,
    media_type TEXT NOT NULL,
    original_blob BLOB NOT NULL,
    original_bytes INTEGER NOT NULL,
    stored_bytes INTEGER NOT NULL,
    content_sha256 BLOB NOT NULL CHECK(length(content_sha256)=32),
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    retention TEXT NOT NULL,
    expires_at TEXT,
    pinned INTEGER NOT NULL DEFAULT 1,
    status INTEGER NOT NULL DEFAULT 0,
    linked_memory_id BLOB, record_checksum BLOB,
    FOREIGN KEY(linked_memory_id) REFERENCES cortex_memory(memory_id)
);
CREATE TABLE hippocampus_stage (
    stage_id BLOB PRIMARY KEY CHECK(length(stage_id)=16),
    created_at TEXT NOT NULL,
    expires_at TEXT,
    scope TEXT NOT NULL,
    source TEXT NOT NULL,
    raw_blob BLOB NOT NULL,
    raw_bytes INTEGER NOT NULL,
    stored_bytes INTEGER NOT NULL,
    checksum_sha256 BLOB NOT NULL CHECK(length(checksum_sha256)=32),
    status INTEGER NOT NULL DEFAULT 0
, record_checksum BLOB);
CREATE TABLE vault_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), vault_id TEXT NOT NULL, revision INTEGER NOT NULL
);
CREATE INDEX cortex_content ON cortex_memory(scope,content_fingerprint,status);
CREATE INDEX native_memory_active_claim ON cortex_memory(scope,claim_id,memory_id) WHERE status=0;
CREATE INDEX native_memory_supersedes ON cortex_memory(supersedes_id);
CREATE INDEX native_verbatim_link ON cortex_verbatim(linked_memory_id);
CREATE INDEX cortex_expiry ON cortex_memory(expires_at);
CREATE INDEX cortex_scope_status ON cortex_memory(scope, status, memory_type);
CREATE INDEX cortex_term_hash ON cortex_term(term_hash);
CREATE INDEX verbatim_expiry ON cortex_verbatim(expires_at);
CREATE INDEX verbatim_scope_status ON cortex_verbatim(scope, status);
CREATE TRIGGER revision_cortex_detail_DELETE AFTER DELETE ON cortex_detail BEGIN UPDATE vault_state SET revision=revision+1 WHERE singleton=1; END;
CREATE TRIGGER revision_cortex_detail_INSERT AFTER INSERT ON cortex_detail BEGIN UPDATE vault_state SET revision=revision+1 WHERE singleton=1; END;
CREATE TRIGGER revision_cortex_detail_UPDATE AFTER UPDATE ON cortex_detail BEGIN UPDATE vault_state SET revision=revision+1 WHERE singleton=1; END;
CREATE TRIGGER revision_cortex_memory_DELETE AFTER DELETE ON cortex_memory BEGIN UPDATE vault_state SET revision=revision+1 WHERE singleton=1; END;
CREATE TRIGGER revision_cortex_memory_INSERT AFTER INSERT ON cortex_memory BEGIN UPDATE vault_state SET revision=revision+1 WHERE singleton=1; END;
CREATE TRIGGER revision_cortex_memory_UPDATE AFTER UPDATE ON cortex_memory BEGIN UPDATE vault_state SET revision=revision+1 WHERE singleton=1; END;
CREATE TRIGGER revision_cortex_tombstone_DELETE AFTER DELETE ON cortex_tombstone BEGIN UPDATE vault_state SET revision=revision+1 WHERE singleton=1; END;
CREATE TRIGGER revision_cortex_tombstone_INSERT AFTER INSERT ON cortex_tombstone BEGIN UPDATE vault_state SET revision=revision+1 WHERE singleton=1; END;
CREATE TRIGGER revision_cortex_tombstone_UPDATE AFTER UPDATE ON cortex_tombstone BEGIN UPDATE vault_state SET revision=revision+1 WHERE singleton=1; END;
CREATE TRIGGER revision_cortex_verbatim_DELETE AFTER DELETE ON cortex_verbatim BEGIN UPDATE vault_state SET revision=revision+1 WHERE singleton=1; END;
CREATE TRIGGER revision_cortex_verbatim_INSERT AFTER INSERT ON cortex_verbatim BEGIN UPDATE vault_state SET revision=revision+1 WHERE singleton=1; END;
CREATE TRIGGER revision_cortex_verbatim_UPDATE AFTER UPDATE ON cortex_verbatim BEGIN UPDATE vault_state SET revision=revision+1 WHERE singleton=1; END;
CREATE TRIGGER revision_hippocampus_stage_DELETE AFTER DELETE ON hippocampus_stage BEGIN UPDATE vault_state SET revision=revision+1 WHERE singleton=1; END;
CREATE TRIGGER revision_hippocampus_stage_INSERT AFTER INSERT ON hippocampus_stage BEGIN UPDATE vault_state SET revision=revision+1 WHERE singleton=1; END;
CREATE TRIGGER revision_hippocampus_stage_UPDATE AFTER UPDATE ON hippocampus_stage BEGIN UPDATE vault_state SET revision=revision+1 WHERE singleton=1; END;
