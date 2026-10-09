BEGIN;

CREATE TABLE app.alembic_version (
    version_num VARCHAR(32) NOT NULL, 
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);

-- Running upgrade  -> 0001_pg_scaffold

CREATE SCHEMA IF NOT EXISTS "app";

CREATE SCHEMA IF NOT EXISTS "search";

CREATE SCHEMA IF NOT EXISTS "checkpoint";

CREATE TABLE app.tenant (
    id UUID NOT NULL, 
    slug VARCHAR(64) NOT NULL, 
    display_name VARCHAR(255) NOT NULL, 
    is_active BOOLEAN DEFAULT true NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    CONSTRAINT pk_tenant PRIMARY KEY (id), 
    CONSTRAINT uq_tenant_slug UNIQUE (slug)
);

CREATE INDEX ix_tenant_slug ON app.tenant (slug);

CREATE TABLE app.knowledge_base (
    id UUID NOT NULL, 
    tenant_id UUID NOT NULL, 
    slug VARCHAR(64) NOT NULL, 
    name VARCHAR(255) NOT NULL, 
    description TEXT, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    CONSTRAINT pk_knowledge_base PRIMARY KEY (id), 
    CONSTRAINT fk_knowledge_base_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES app.tenant (id), 
    CONSTRAINT uq_knowledge_base_tenant_slug UNIQUE (tenant_id, slug)
);

CREATE INDEX ix_knowledge_base_tenant_id ON app.knowledge_base (tenant_id);

CREATE TABLE app.document_record (
    id UUID NOT NULL, 
    tenant_id UUID NOT NULL, 
    kb_id UUID NOT NULL, 
    external_id VARCHAR(255), 
    title VARCHAR(255) NOT NULL, 
    source_uri VARCHAR(1024), 
    ingest_status VARCHAR(32) DEFAULT 'pending' NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    CONSTRAINT pk_document_record PRIMARY KEY (id), 
    CONSTRAINT fk_document_record_kb_id_knowledge_base FOREIGN KEY(kb_id) REFERENCES app.knowledge_base (id), 
    CONSTRAINT fk_document_record_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES app.tenant (id)
);

CREATE INDEX ix_document_record_tenant_id ON app.document_record (tenant_id);

CREATE INDEX ix_document_record_kb_id ON app.document_record (kb_id);

CREATE TABLE app.workflow_definition (
    id UUID NOT NULL, 
    tenant_id UUID NOT NULL, 
    slug VARCHAR(64) NOT NULL, 
    name VARCHAR(255) NOT NULL, 
    description TEXT, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    CONSTRAINT pk_workflow_definition PRIMARY KEY (id), 
    CONSTRAINT fk_workflow_definition_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES app.tenant (id), 
    CONSTRAINT uq_workflow_definition_tenant_slug UNIQUE (tenant_id, slug)
);

CREATE INDEX ix_workflow_definition_tenant_id ON app.workflow_definition (tenant_id);

CREATE TABLE app.workflow_version (
    id UUID NOT NULL, 
    tenant_id UUID NOT NULL, 
    workflow_definition_id UUID NOT NULL, 
    version INTEGER NOT NULL, 
    is_active BOOLEAN DEFAULT false NOT NULL, 
    dsl JSONB DEFAULT '{}'::jsonb NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    CONSTRAINT pk_workflow_version PRIMARY KEY (id), 
    CONSTRAINT fk_workflow_version_workflow_definition_id_workflow_definition FOREIGN KEY(workflow_definition_id) REFERENCES app.workflow_definition (id), 
    CONSTRAINT fk_workflow_version_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES app.tenant (id), 
    CONSTRAINT uq_workflow_version_number UNIQUE (workflow_definition_id, version)
);

CREATE INDEX ix_workflow_version_tenant_id ON app.workflow_version (tenant_id);

CREATE INDEX ix_workflow_version_workflow_definition_id ON app.workflow_version (workflow_definition_id);

CREATE TABLE app.conversation (
    id UUID NOT NULL, 
    tenant_id UUID NOT NULL, 
    external_user_id VARCHAR(255) NOT NULL, 
    title VARCHAR(255), 
    status VARCHAR(32) DEFAULT 'active' NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    CONSTRAINT pk_conversation PRIMARY KEY (id), 
    CONSTRAINT fk_conversation_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES app.tenant (id)
);

CREATE INDEX ix_conversation_tenant_id ON app.conversation (tenant_id);

CREATE INDEX ix_conversation_external_user_id ON app.conversation (external_user_id);

CREATE TABLE app.message_index (
    id UUID NOT NULL, 
    tenant_id UUID NOT NULL, 
    conversation_id UUID NOT NULL, 
    role VARCHAR(32) NOT NULL, 
    content TEXT NOT NULL, 
    token_count INTEGER, 
    is_summary BOOLEAN DEFAULT false NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    CONSTRAINT pk_message_index PRIMARY KEY (id), 
    CONSTRAINT fk_message_index_conversation_id_conversation FOREIGN KEY(conversation_id) REFERENCES app.conversation (id), 
    CONSTRAINT fk_message_index_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES app.tenant (id)
);

CREATE INDEX ix_message_index_tenant_id ON app.message_index (tenant_id);

CREATE INDEX ix_message_index_conversation_id ON app.message_index (conversation_id);

CREATE TABLE app.memory_summary (
    id UUID NOT NULL, 
    tenant_id UUID NOT NULL, 
    conversation_id UUID NOT NULL, 
    summary TEXT NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    CONSTRAINT pk_memory_summary PRIMARY KEY (id), 
    CONSTRAINT fk_memory_summary_conversation_id_conversation FOREIGN KEY(conversation_id) REFERENCES app.conversation (id), 
    CONSTRAINT fk_memory_summary_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES app.tenant (id)
);

CREATE INDEX ix_memory_summary_tenant_id ON app.memory_summary (tenant_id);

CREATE INDEX ix_memory_summary_conversation_id ON app.memory_summary (conversation_id);

CREATE TABLE app.memory_fact (
    id UUID NOT NULL, 
    tenant_id UUID NOT NULL, 
    conversation_id UUID, 
    category VARCHAR(64) NOT NULL, 
    fact TEXT NOT NULL, 
    confidence FLOAT DEFAULT 0.5 NOT NULL, 
    decay_score FLOAT DEFAULT 1.0 NOT NULL, 
    last_accessed_at TIMESTAMP WITH TIME ZONE, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    CONSTRAINT pk_memory_fact PRIMARY KEY (id), 
    CONSTRAINT fk_memory_fact_conversation_id_conversation FOREIGN KEY(conversation_id) REFERENCES app.conversation (id), 
    CONSTRAINT fk_memory_fact_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES app.tenant (id)
);

CREATE INDEX ix_memory_fact_tenant_id ON app.memory_fact (tenant_id);

CREATE INDEX ix_memory_fact_conversation_id ON app.memory_fact (conversation_id);

CREATE TABLE app.chunk_manifest (
    id UUID NOT NULL, 
    tenant_id UUID NOT NULL, 
    kb_id UUID NOT NULL, 
    document_id UUID NOT NULL, 
    chunk_total INTEGER DEFAULT 0 NOT NULL, 
    indexing_state VARCHAR(32) DEFAULT 'pending' NOT NULL, 
    error_message TEXT, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    CONSTRAINT pk_chunk_manifest PRIMARY KEY (id), 
    CONSTRAINT fk_chunk_manifest_document_id_document_record FOREIGN KEY(document_id) REFERENCES app.document_record (id), 
    CONSTRAINT fk_chunk_manifest_kb_id_knowledge_base FOREIGN KEY(kb_id) REFERENCES app.knowledge_base (id), 
    CONSTRAINT fk_chunk_manifest_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES app.tenant (id)
);

CREATE INDEX ix_chunk_manifest_tenant_id ON app.chunk_manifest (tenant_id);

CREATE INDEX ix_chunk_manifest_kb_id ON app.chunk_manifest (kb_id);

CREATE INDEX ix_chunk_manifest_document_id ON app.chunk_manifest (document_id);

CREATE TABLE app.run (
    id UUID NOT NULL, 
    tenant_id UUID NOT NULL, 
    workflow_version_id UUID, 
    thread_id VARCHAR(255) NOT NULL, 
    status VARCHAR(32) DEFAULT 'queued' NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    CONSTRAINT pk_run PRIMARY KEY (id), 
    CONSTRAINT fk_run_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES app.tenant (id), 
    CONSTRAINT fk_run_workflow_version_id_workflow_version FOREIGN KEY(workflow_version_id) REFERENCES app.workflow_version (id)
);

CREATE INDEX ix_run_tenant_id ON app.run (tenant_id);

CREATE INDEX ix_run_workflow_version_id ON app.run (workflow_version_id);

CREATE INDEX ix_run_thread_id ON app.run (thread_id);

CREATE TABLE app.run_summary (
    id UUID NOT NULL, 
    tenant_id UUID NOT NULL, 
    run_id UUID NOT NULL, 
    outcome VARCHAR(64), 
    latency_ms INTEGER, 
    model_usage JSONB DEFAULT '{}'::jsonb NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    CONSTRAINT pk_run_summary PRIMARY KEY (id), 
    CONSTRAINT fk_run_summary_run_id_run FOREIGN KEY(run_id) REFERENCES app.run (id), 
    CONSTRAINT fk_run_summary_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES app.tenant (id), 
    CONSTRAINT uq_run_summary_run_id UNIQUE (run_id)
);

CREATE INDEX ix_run_summary_tenant_id ON app.run_summary (tenant_id);

CREATE INDEX ix_run_summary_run_id ON app.run_summary (run_id);

CREATE TABLE app.task (
    id UUID NOT NULL, 
    tenant_id UUID NOT NULL, 
    kind VARCHAR(64) NOT NULL, 
    status VARCHAR(32) DEFAULT 'pending' NOT NULL, 
    payload JSONB DEFAULT '{}'::jsonb NOT NULL, 
    available_at TIMESTAMP WITH TIME ZONE, 
    lease_until TIMESTAMP WITH TIME ZONE, 
    attempts INTEGER DEFAULT 0 NOT NULL, 
    idempotency_key VARCHAR(255) NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    CONSTRAINT pk_task PRIMARY KEY (id), 
    CONSTRAINT fk_task_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES app.tenant (id), 
    CONSTRAINT uq_task_tenant_idempotency_key UNIQUE (tenant_id, idempotency_key)
);

CREATE INDEX ix_task_tenant_id ON app.task (tenant_id);

CREATE INDEX ix_task_status ON app.task (status);

CREATE INDEX ix_task_available_at ON app.task (available_at);

CREATE INDEX ix_task_lease_until ON app.task (lease_until);

CREATE TABLE search.chunk_stable (
    id UUID NOT NULL, 
    tenant_id UUID NOT NULL, 
    kb_id UUID, 
    document_id UUID, 
    parent_chunk_id UUID, 
    layer VARCHAR(16) DEFAULT 'L1' NOT NULL, 
    lang VARCHAR(16) DEFAULT 'simple' NOT NULL, 
    title TEXT, 
    headings_text TEXT, 
    body TEXT NOT NULL, 
    tags_text TEXT, 
    metadata_json JSONB DEFAULT '{}'::jsonb NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    expires_at TIMESTAMP WITH TIME ZONE, 
    search_vector TSVECTOR GENERATED ALWAYS AS (setweight(to_tsvector('simple', coalesce(title, '')), 'A') ||
setweight(to_tsvector('simple', coalesce(headings_text, '')), 'B') ||
setweight(to_tsvector('simple', coalesce(body, '')), 'C') ||
setweight(to_tsvector('simple', coalesce(tags_text, '')), 'D')) STORED NOT NULL, 
    CONSTRAINT pk_chunk_stable PRIMARY KEY (id)
);

CREATE INDEX ix_chunk_stable_search_vector ON search.chunk_stable USING gin (search_vector);

CREATE TABLE search.chunk_hot (
    id UUID NOT NULL, 
    tenant_id UUID NOT NULL, 
    kb_id UUID, 
    document_id UUID, 
    parent_chunk_id UUID, 
    layer VARCHAR(16) DEFAULT 'L1' NOT NULL, 
    lang VARCHAR(16) DEFAULT 'simple' NOT NULL, 
    title TEXT, 
    headings_text TEXT, 
    body TEXT NOT NULL, 
    tags_text TEXT, 
    metadata_json JSONB DEFAULT '{}'::jsonb NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    expires_at TIMESTAMP WITH TIME ZONE, 
    search_vector TSVECTOR GENERATED ALWAYS AS (setweight(to_tsvector('simple', coalesce(title, '')), 'A') ||
setweight(to_tsvector('simple', coalesce(headings_text, '')), 'B') ||
setweight(to_tsvector('simple', coalesce(body, '')), 'C') ||
setweight(to_tsvector('simple', coalesce(tags_text, '')), 'D')) STORED NOT NULL, 
    CONSTRAINT pk_chunk_hot PRIMARY KEY (id)
);

CREATE INDEX ix_chunk_hot_search_vector ON search.chunk_hot USING gin (search_vector);

CREATE TABLE search.memory (
    id UUID NOT NULL, 
    tenant_id UUID NOT NULL, 
    kb_id UUID, 
    document_id UUID, 
    parent_chunk_id UUID, 
    layer VARCHAR(16) DEFAULT 'L1' NOT NULL, 
    lang VARCHAR(16) DEFAULT 'simple' NOT NULL, 
    title TEXT, 
    headings_text TEXT, 
    body TEXT NOT NULL, 
    tags_text TEXT, 
    metadata_json JSONB DEFAULT '{}'::jsonb NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    expires_at TIMESTAMP WITH TIME ZONE, 
    search_vector TSVECTOR GENERATED ALWAYS AS (setweight(to_tsvector('simple', coalesce(title, '')), 'A') ||
setweight(to_tsvector('simple', coalesce(headings_text, '')), 'B') ||
setweight(to_tsvector('simple', coalesce(body, '')), 'C') ||
setweight(to_tsvector('simple', coalesce(tags_text, '')), 'D')) STORED NOT NULL, 
    CONSTRAINT pk_memory PRIMARY KEY (id)
);

CREATE INDEX ix_memory_search_vector ON search.memory USING gin (search_vector);

CREATE INDEX ix_chunk_stable_scope ON search.chunk_stable (tenant_id, kb_id, layer, updated_at);

CREATE INDEX ix_chunk_hot_scope ON search.chunk_hot (tenant_id, kb_id, layer, updated_at);

CREATE INDEX ix_memory_scope ON search.memory (tenant_id, updated_at);

INSERT INTO app.alembic_version (version_num) VALUES ('0001_pg_scaffold') RETURNING app.alembic_version.version_num;

-- Running upgrade 0001_pg_scaffold -> 0002_tenant_policy_fields

ALTER TABLE app.tenant ADD COLUMN plan_tier VARCHAR(32) DEFAULT 'standard' NOT NULL;

ALTER TABLE app.tenant ADD COLUMN locale VARCHAR(16) DEFAULT 'zh-CN' NOT NULL;

ALTER TABLE app.tenant ADD COLUMN timezone VARCHAR(64) DEFAULT 'Asia/Shanghai' NOT NULL;

ALTER TABLE app.tenant ADD COLUMN security_level VARCHAR(16) DEFAULT 'internal' NOT NULL;

UPDATE app.alembic_version SET version_num='0002_tenant_policy_fields' WHERE app.alembic_version.version_num = '0001_pg_scaffold';

-- Running upgrade 0002_tenant_policy_fields -> 0003_user_and_tenant_extras

CREATE TABLE app."user" (
    id UUID NOT NULL, 
    tenant_id UUID NOT NULL, 
    external_id VARCHAR(255) NOT NULL, 
    display_name VARCHAR(255) NOT NULL, 
    email VARCHAR(255), 
    roles JSONB DEFAULT '[]'::jsonb NOT NULL, 
    is_active BOOLEAN DEFAULT true NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    CONSTRAINT pk_user PRIMARY KEY (id), 
    CONSTRAINT fk_user_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES app.tenant (id), 
    CONSTRAINT uq_user_tenant_external UNIQUE (tenant_id, external_id)
);

CREATE INDEX ix_user_tenant_id ON app."user" (tenant_id);

CREATE TABLE app.tenant_domain (
    id UUID NOT NULL, 
    tenant_id UUID NOT NULL, 
    domain VARCHAR(255) NOT NULL, 
    is_verified BOOLEAN DEFAULT false NOT NULL, 
    is_primary BOOLEAN DEFAULT false NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    CONSTRAINT pk_tenant_domain PRIMARY KEY (id), 
    CONSTRAINT fk_tenant_domain_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES app.tenant (id), 
    CONSTRAINT uq_tenant_domain_domain UNIQUE (domain)
);

CREATE INDEX ix_tenant_domain_tenant_id ON app.tenant_domain (tenant_id);

CREATE TABLE app.tenant_brand (
    id UUID NOT NULL, 
    tenant_id UUID NOT NULL, 
    brand_name VARCHAR(255) NOT NULL, 
    logo_url VARCHAR(1024), 
    primary_color VARCHAR(7), 
    css_variables JSONB DEFAULT '{}'::jsonb NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    CONSTRAINT pk_tenant_brand PRIMARY KEY (id), 
    CONSTRAINT fk_tenant_brand_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES app.tenant (id)
);

CREATE INDEX ix_tenant_brand_tenant_id ON app.tenant_brand (tenant_id);

CREATE OR REPLACE FUNCTION app.tenant_filter_check()
    RETURNS boolean AS $$
    DECLARE
        current_tid text;
    BEGIN
        current_tid := current_setting('app.current_tenant_id', true);
        IF current_tid IS NULL OR current_tid = '' THEN
            RETURN true;
        END IF;
        RETURN tenant_id = current_tid::uuid;
    EXCEPTION WHEN OTHERS THEN
        RETURN true;
    END;
    $$ LANGUAGE plpgsql STABLE SECURITY DEFINER;;

ALTER TABLE app."knowledge_base" ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_knowledge_base ON app."knowledge_base" FOR ALL USING (app.tenant_filter_check()) WITH CHECK (app.tenant_filter_check());

ALTER TABLE app."document_record" ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_document_record ON app."document_record" FOR ALL USING (app.tenant_filter_check()) WITH CHECK (app.tenant_filter_check());

ALTER TABLE app."chunk_manifest" ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_chunk_manifest ON app."chunk_manifest" FOR ALL USING (app.tenant_filter_check()) WITH CHECK (app.tenant_filter_check());

ALTER TABLE app."workflow_definition" ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_workflow_definition ON app."workflow_definition" FOR ALL USING (app.tenant_filter_check()) WITH CHECK (app.tenant_filter_check());

ALTER TABLE app."workflow_version" ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_workflow_version ON app."workflow_version" FOR ALL USING (app.tenant_filter_check()) WITH CHECK (app.tenant_filter_check());

ALTER TABLE app."conversation" ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_conversation ON app."conversation" FOR ALL USING (app.tenant_filter_check()) WITH CHECK (app.tenant_filter_check());

ALTER TABLE app."message_index" ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_message_index ON app."message_index" FOR ALL USING (app.tenant_filter_check()) WITH CHECK (app.tenant_filter_check());

ALTER TABLE app."memory_summary" ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_memory_summary ON app."memory_summary" FOR ALL USING (app.tenant_filter_check()) WITH CHECK (app.tenant_filter_check());

ALTER TABLE app."memory_fact" ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_memory_fact ON app."memory_fact" FOR ALL USING (app.tenant_filter_check()) WITH CHECK (app.tenant_filter_check());

ALTER TABLE app."run" ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_run ON app."run" FOR ALL USING (app.tenant_filter_check()) WITH CHECK (app.tenant_filter_check());

ALTER TABLE app."run_summary" ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_run_summary ON app."run_summary" FOR ALL USING (app.tenant_filter_check()) WITH CHECK (app.tenant_filter_check());

ALTER TABLE app."task" ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_task ON app."task" FOR ALL USING (app.tenant_filter_check()) WITH CHECK (app.tenant_filter_check());

ALTER TABLE app."user" ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_user ON app."user" FOR ALL USING (app.tenant_filter_check()) WITH CHECK (app.tenant_filter_check());

ALTER TABLE app."tenant_domain" ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_tenant_domain ON app."tenant_domain" FOR ALL USING (app.tenant_filter_check()) WITH CHECK (app.tenant_filter_check());

ALTER TABLE app."tenant_brand" ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_tenant_brand ON app."tenant_brand" FOR ALL USING (app.tenant_filter_check()) WITH CHECK (app.tenant_filter_check());

UPDATE app.alembic_version SET version_num='0003_user_and_tenant_extras' WHERE app.alembic_version.version_num = '0002_tenant_policy_fields';

-- Running upgrade 0003_user_and_tenant_extras -> 0004_model_gateway

CREATE TABLE app.provider (
    id UUID NOT NULL, 
    slug VARCHAR(64) NOT NULL, 
    provider_type VARCHAR(32) NOT NULL, 
    display_name VARCHAR(255) NOT NULL, 
    base_url VARCHAR(512), 
    is_active BOOLEAN DEFAULT true NOT NULL, 
    config JSONB DEFAULT '{}'::jsonb NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    CONSTRAINT pk_provider PRIMARY KEY (id), 
    CONSTRAINT uq_provider_slug UNIQUE (slug)
);

CREATE TABLE app.key_pool (
    id UUID NOT NULL, 
    provider_id UUID NOT NULL, 
    label VARCHAR(255) NOT NULL, 
    api_key_encrypted VARCHAR(512) NOT NULL, 
    weight INTEGER DEFAULT 1 NOT NULL, 
    is_active BOOLEAN DEFAULT true NOT NULL, 
    last_health_check_at TIMESTAMP WITH TIME ZONE, 
    consecutive_failures INTEGER DEFAULT 0 NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    CONSTRAINT pk_key_pool PRIMARY KEY (id), 
    CONSTRAINT fk_key_pool_provider_id_provider FOREIGN KEY(provider_id) REFERENCES app.provider (id)
);

CREATE INDEX ix_key_pool_provider_id ON app.key_pool (provider_id);

CREATE TABLE app.model_profile (
    id UUID NOT NULL, 
    tenant_id UUID NOT NULL, 
    name VARCHAR(255) NOT NULL, 
    use_case VARCHAR(32) NOT NULL, 
    provider_id UUID NOT NULL, 
    model_name VARCHAR(255) NOT NULL, 
    parameters JSONB DEFAULT '{}'::jsonb NOT NULL, 
    fallback_profile_id UUID, 
    is_active BOOLEAN DEFAULT true NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    CONSTRAINT pk_model_profile PRIMARY KEY (id), 
    CONSTRAINT fk_model_profile_provider_id_provider FOREIGN KEY(provider_id) REFERENCES app.provider (id), 
    CONSTRAINT fk_model_profile_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES app.tenant (id)
);

CREATE INDEX ix_model_profile_tenant_id ON app.model_profile (tenant_id);

CREATE INDEX ix_model_profile_provider_id ON app.model_profile (provider_id);

CREATE TABLE app.tenant_model_binding (
    id UUID NOT NULL, 
    tenant_id UUID NOT NULL, 
    use_case VARCHAR(32) NOT NULL, 
    profile_id UUID NOT NULL, 
    is_default BOOLEAN DEFAULT false NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    CONSTRAINT pk_tenant_model_binding PRIMARY KEY (id), 
    CONSTRAINT fk_tenant_model_binding_profile_id_model_profile FOREIGN KEY(profile_id) REFERENCES app.model_profile (id), 
    CONSTRAINT fk_tenant_model_binding_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES app.tenant (id), 
    CONSTRAINT uq_tenant_binding_use_case UNIQUE (tenant_id, use_case)
);

CREATE INDEX ix_tenant_model_binding_tenant_id ON app.tenant_model_binding (tenant_id);

CREATE INDEX ix_tenant_model_binding_profile_id ON app.tenant_model_binding (profile_id);

ALTER TABLE app."model_profile" ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_model_profile ON app."model_profile" FOR ALL USING (app.tenant_filter_check()) WITH CHECK (app.tenant_filter_check());

ALTER TABLE app."tenant_model_binding" ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_tenant_model_binding ON app."tenant_model_binding" FOR ALL USING (app.tenant_filter_check()) WITH CHECK (app.tenant_filter_check());

UPDATE app.alembic_version SET version_num='0004_model_gateway' WHERE app.alembic_version.version_num = '0003_user_and_tenant_extras';

-- Running upgrade 0004_model_gateway -> 0005_page_anchored_eval

ALTER TABLE search.chunk_stable ADD COLUMN page_no INTEGER;

ALTER TABLE search.chunk_stable ADD COLUMN page_count INTEGER;

ALTER TABLE search.chunk_stable ADD COLUMN page_offset_start INTEGER;

ALTER TABLE search.chunk_stable ADD COLUMN page_offset_end INTEGER;

ALTER TABLE search.chunk_stable ADD COLUMN element_type VARCHAR(16) DEFAULT 'text' NOT NULL;

ALTER TABLE search.chunk_stable ADD COLUMN start_char_idx INTEGER;

ALTER TABLE search.chunk_stable ADD COLUMN end_char_idx INTEGER;

ALTER TABLE search.chunk_stable ADD COLUMN is_parent BOOLEAN DEFAULT false NOT NULL;

ALTER TABLE search.chunk_stable ADD COLUMN brand_id UUID;

ALTER TABLE search.chunk_stable ADD COLUMN source_type VARCHAR(32) DEFAULT 'upload' NOT NULL;

ALTER TABLE search.chunk_stable ADD COLUMN security_level VARCHAR(16) DEFAULT 'internal' NOT NULL;

ALTER TABLE search.chunk_stable ADD COLUMN page_image_ref VARCHAR(1024);

ALTER TABLE search.chunk_hot ADD COLUMN page_no INTEGER;

ALTER TABLE search.chunk_hot ADD COLUMN page_count INTEGER;

ALTER TABLE search.chunk_hot ADD COLUMN page_offset_start INTEGER;

ALTER TABLE search.chunk_hot ADD COLUMN page_offset_end INTEGER;

ALTER TABLE search.chunk_hot ADD COLUMN element_type VARCHAR(16) DEFAULT 'text' NOT NULL;

ALTER TABLE search.chunk_hot ADD COLUMN start_char_idx INTEGER;

ALTER TABLE search.chunk_hot ADD COLUMN end_char_idx INTEGER;

ALTER TABLE search.chunk_hot ADD COLUMN is_parent BOOLEAN DEFAULT false NOT NULL;

ALTER TABLE search.chunk_hot ADD COLUMN brand_id UUID;

ALTER TABLE search.chunk_hot ADD COLUMN source_type VARCHAR(32) DEFAULT 'upload' NOT NULL;

ALTER TABLE search.chunk_hot ADD COLUMN security_level VARCHAR(16) DEFAULT 'internal' NOT NULL;

ALTER TABLE search.chunk_hot ADD COLUMN page_image_ref VARCHAR(1024);

ALTER TABLE search.memory ADD COLUMN page_no INTEGER;

ALTER TABLE search.memory ADD COLUMN page_count INTEGER;

ALTER TABLE search.memory ADD COLUMN page_offset_start INTEGER;

ALTER TABLE search.memory ADD COLUMN page_offset_end INTEGER;

ALTER TABLE search.memory ADD COLUMN element_type VARCHAR(16) DEFAULT 'text' NOT NULL;

ALTER TABLE search.memory ADD COLUMN start_char_idx INTEGER;

ALTER TABLE search.memory ADD COLUMN end_char_idx INTEGER;

ALTER TABLE search.memory ADD COLUMN is_parent BOOLEAN DEFAULT false NOT NULL;

ALTER TABLE search.memory ADD COLUMN brand_id UUID;

ALTER TABLE search.memory ADD COLUMN source_type VARCHAR(32) DEFAULT 'upload' NOT NULL;

ALTER TABLE search.memory ADD COLUMN security_level VARCHAR(16) DEFAULT 'internal' NOT NULL;

ALTER TABLE search.memory ADD COLUMN page_image_ref VARCHAR(1024);

ALTER TABLE search.memory ADD COLUMN user_id UUID;

CREATE INDEX ix_chunk_stable_page ON search.chunk_stable (tenant_id, document_id, page_no);

CREATE INDEX ix_chunk_hot_page ON search.chunk_hot (tenant_id, document_id, page_no);

CREATE INDEX ix_search_memory_user ON search.memory (tenant_id, user_id);

CREATE INDEX ix_chunk_stable_page_no ON search.chunk_stable (page_no);

CREATE INDEX ix_chunk_stable_is_parent ON search.chunk_stable (is_parent);

CREATE INDEX ix_chunk_hot_page_no ON search.chunk_hot (page_no);

CREATE INDEX ix_chunk_hot_is_parent ON search.chunk_hot (is_parent);

CREATE INDEX ix_memory_page_no ON search.memory (page_no);

CREATE INDEX ix_memory_is_parent ON search.memory (is_parent);

ALTER TABLE app.chunk_manifest ADD COLUMN parent_unit VARCHAR(16) DEFAULT 'token_window' NOT NULL;

ALTER TABLE app.chunk_manifest ADD COLUMN page_count INTEGER;

ALTER TABLE app.chunk_manifest ADD COLUMN parser_version VARCHAR(64);

ALTER TABLE app.chunk_manifest ADD COLUMN chunker_version VARCHAR(64);

ALTER TABLE app.chunk_manifest ADD COLUMN embedding_profile VARCHAR(64);

CREATE TABLE app.retriever_profile_version (
    id UUID NOT NULL, 
    tenant_id UUID NOT NULL, 
    profile_id UUID NOT NULL, 
    version INTEGER DEFAULT 1 NOT NULL, 
    query_strategy VARCHAR(32) DEFAULT 'direct' NOT NULL, 
    recall_channels JSONB DEFAULT '[]'::jsonb NOT NULL, 
    join_mode VARCHAR(32) DEFAULT 'reciprocal_rank_fusion' NOT NULL, 
    channel_budgets JSONB DEFAULT '{}'::jsonb NOT NULL, 
    fusion_config JSONB DEFAULT '{}'::jsonb NOT NULL, 
    crag_thresholds JSONB DEFAULT '{}'::jsonb NOT NULL, 
    rerank_model_profile VARCHAR(128), 
    citation_verify_semantic BOOLEAN DEFAULT false NOT NULL, 
    top_k_recall INTEGER DEFAULT 50 NOT NULL, 
    top_k_rerank INTEGER DEFAULT 20 NOT NULL, 
    top_k_generate INTEGER DEFAULT 6 NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    CONSTRAINT pk_retriever_profile_version PRIMARY KEY (id), 
    CONSTRAINT fk_rpv_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES app.tenant (id), 
    CONSTRAINT uq_retriever_profile_version UNIQUE (tenant_id, profile_id, version)
);

CREATE INDEX ix_rpv_tenant_id ON app.retriever_profile_version (tenant_id);

CREATE INDEX ix_rpv_profile_id ON app.retriever_profile_version (profile_id);

CREATE TABLE app.eval_case (
    id UUID NOT NULL, 
    tenant_id UUID NOT NULL, 
    kb_id UUID, 
    question TEXT NOT NULL, 
    conversation_turns JSONB DEFAULT '[]'::jsonb NOT NULL, 
    gold_evidence JSONB DEFAULT '[]'::jsonb NOT NULL, 
    expected_answer TEXT, 
    answerable BOOLEAN DEFAULT true NOT NULL, 
    supported_claims JSONB DEFAULT '[]'::jsonb NOT NULL, 
    review_state VARCHAR(16) DEFAULT 'pending' NOT NULL, 
    labeler VARCHAR(255), 
    split VARCHAR(16) DEFAULT 'dev' NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    CONSTRAINT pk_eval_case PRIMARY KEY (id), 
    CONSTRAINT fk_eval_case_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES app.tenant (id)
);

CREATE INDEX ix_eval_case_tenant_id ON app.eval_case (tenant_id);

CREATE INDEX ix_eval_case_kb_id ON app.eval_case (kb_id);

CREATE INDEX ix_eval_case_split ON app.eval_case (tenant_id, split);

CREATE TABLE app.eval_run (
    id UUID NOT NULL, 
    tenant_id UUID NOT NULL, 
    bundle_id VARCHAR(128) NOT NULL, 
    bundle JSONB DEFAULT '{}'::jsonb NOT NULL, 
    metrics JSONB DEFAULT '{}'::jsonb NOT NULL, 
    case_total INTEGER DEFAULT 0 NOT NULL, 
    split VARCHAR(16) DEFAULT 'holdout' NOT NULL, 
    status VARCHAR(32) DEFAULT 'completed' NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    CONSTRAINT pk_eval_run PRIMARY KEY (id), 
    CONSTRAINT fk_eval_run_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES app.tenant (id)
);

CREATE INDEX ix_eval_run_tenant_id ON app.eval_run (tenant_id);

CREATE INDEX ix_eval_run_bundle_id ON app.eval_run (bundle_id);

CREATE TABLE app.grader_validation (
    id UUID NOT NULL, 
    tenant_id UUID NOT NULL, 
    rails_prompt_version VARCHAR(64) NOT NULL, 
    sample_size INTEGER DEFAULT 0 NOT NULL, 
    agreement FLOAT DEFAULT 0 NOT NULL, 
    confusion JSONB DEFAULT '{}'::jsonb NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    CONSTRAINT pk_grader_validation PRIMARY KEY (id), 
    CONSTRAINT fk_grader_validation_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES app.tenant (id)
);

CREATE INDEX ix_gv_tenant_id ON app.grader_validation (tenant_id);

CREATE INDEX ix_gv_version ON app.grader_validation (rails_prompt_version);

CREATE TABLE app.reranker_artifact (
    id UUID NOT NULL, 
    tenant_id UUID NOT NULL, 
    version INTEGER DEFAULT 1 NOT NULL, 
    method VARCHAR(32) DEFAULT 'sft' NOT NULL, 
    scope_key VARCHAR(255) NOT NULL, 
    artifact_ref VARCHAR(1024) NOT NULL, 
    source_bundle JSONB DEFAULT '{}'::jsonb NOT NULL, 
    eval_metrics JSONB DEFAULT '{}'::jsonb NOT NULL, 
    is_promoted BOOLEAN DEFAULT false NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    CONSTRAINT pk_reranker_artifact PRIMARY KEY (id), 
    CONSTRAINT fk_reranker_artifact_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES app.tenant (id), 
    CONSTRAINT uq_reranker_artifact_tenant_version UNIQUE (tenant_id, version)
);

CREATE INDEX ix_reranker_artifact_tenant_id ON app.reranker_artifact (tenant_id);

CREATE INDEX ix_reranker_artifact_is_promoted ON app.reranker_artifact (is_promoted);

ALTER TABLE app.model_profile ADD COLUMN artifact_ref VARCHAR(1024);

ALTER TABLE app."retriever_profile_version" ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_retriever_profile_version ON app."retriever_profile_version" FOR ALL USING (app.tenant_filter_check()) WITH CHECK (app.tenant_filter_check());

ALTER TABLE app."eval_case" ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_eval_case ON app."eval_case" FOR ALL USING (app.tenant_filter_check()) WITH CHECK (app.tenant_filter_check());

ALTER TABLE app."eval_run" ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_eval_run ON app."eval_run" FOR ALL USING (app.tenant_filter_check()) WITH CHECK (app.tenant_filter_check());

ALTER TABLE app."grader_validation" ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_grader_validation ON app."grader_validation" FOR ALL USING (app.tenant_filter_check()) WITH CHECK (app.tenant_filter_check());

ALTER TABLE app."reranker_artifact" ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_reranker_artifact ON app."reranker_artifact" FOR ALL USING (app.tenant_filter_check()) WITH CHECK (app.tenant_filter_check());

UPDATE app.alembic_version SET version_num='0005_page_anchored_eval' WHERE app.alembic_version.version_num = '0004_model_gateway';

-- Running upgrade 0005_page_anchored_eval -> 0006_retriever_profile_gate

ALTER TABLE app.retriever_profile_version ADD COLUMN name VARCHAR(128);

ALTER TABLE app.retriever_profile_version ADD COLUMN crag_enabled BOOLEAN DEFAULT true NOT NULL;

UPDATE app."retriever_profile_version" SET "crag_enabled" = true WHERE "crag_enabled" IS NULL;

UPDATE app.alembic_version SET version_num='0006_retriever_profile_gate' WHERE app.alembic_version.version_num = '0005_page_anchored_eval';

-- Running upgrade 0006_retriever_profile_gate -> 0007_memory_bitemporal

ALTER TABLE app.memory_fact ADD COLUMN user_id UUID;

ALTER TABLE app.memory_fact ADD COLUMN subject_key VARCHAR(128) DEFAULT '' NOT NULL;

ALTER TABLE app.memory_fact ADD COLUMN valid_from TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL;

ALTER TABLE app.memory_fact ADD COLUMN valid_until TIMESTAMP WITH TIME ZONE;

ALTER TABLE app.memory_fact ADD COLUMN ingested_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL;

ALTER TABLE app.memory_fact ADD COLUMN superseded_by UUID;

ALTER TABLE app.memory_fact ADD COLUMN mention_count INTEGER DEFAULT 1 NOT NULL;

ALTER TABLE app.memory_summary ADD COLUMN turn_range_start INTEGER DEFAULT 0 NOT NULL;

ALTER TABLE app.memory_summary ADD COLUMN turn_range_end INTEGER DEFAULT 0 NOT NULL;

ALTER TABLE app.memory_summary ADD COLUMN summary_version VARCHAR(64);

CREATE INDEX ix_memory_fact_active ON app.memory_fact (tenant_id, user_id, valid_until);

CREATE INDEX ix_memory_fact_decay ON app.memory_fact (tenant_id, decay_score);

CREATE INDEX ix_memory_fact_subject ON app.memory_fact (tenant_id, user_id, subject_key);

CREATE TABLE app.memory_profile_version (
    id UUID NOT NULL, 
    tenant_id UUID NOT NULL, 
    version INTEGER DEFAULT 1 NOT NULL, 
    window_n INTEGER NOT NULL, 
    strategy VARCHAR(32) DEFAULT 'rolling' NOT NULL, 
    summary_prompt_version VARCHAR(64), 
    eval_run_id UUID, 
    notes TEXT, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    CONSTRAINT pk_memory_profile_version PRIMARY KEY (id), 
    CONSTRAINT fk_memory_profile_version_tenant_id FOREIGN KEY(tenant_id) REFERENCES app.tenant (id), 
    CONSTRAINT uq_memory_profile_version UNIQUE (tenant_id, version)
);

CREATE INDEX ix_memory_profile_version_tenant_id ON app.memory_profile_version (tenant_id);

ALTER TABLE app."memory_profile_version" ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_memory_profile_version ON app."memory_profile_version" FOR ALL USING (app.tenant_filter_check()) WITH CHECK (app.tenant_filter_check());

UPDATE app.alembic_version SET version_num='0007_memory_bitemporal' WHERE app.alembic_version.version_num = '0006_retriever_profile_gate';

-- Running upgrade 0007_memory_bitemporal -> 0008_chat_feedback

ALTER TABLE app.message_index ADD COLUMN run_id UUID;

ALTER TABLE app.message_index ADD COLUMN citations JSONB DEFAULT '[]'::jsonb NOT NULL;

CREATE INDEX ix_message_index_run_id ON app.message_index (run_id);

CREATE TABLE app.feedback_entry (
    id UUID NOT NULL, 
    tenant_id UUID NOT NULL, 
    conversation_id UUID NOT NULL, 
    message_id UUID NOT NULL, 
    user_id UUID, 
    run_id UUID, 
    rating VARCHAR(16) NOT NULL, 
    comment TEXT, 
    answer_snapshot TEXT, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
    CONSTRAINT pk_feedback_entry PRIMARY KEY (id), 
    CONSTRAINT fk_feedback_entry_tenant_id FOREIGN KEY(tenant_id) REFERENCES app.tenant (id), 
    CONSTRAINT fk_feedback_entry_conversation_id FOREIGN KEY(conversation_id) REFERENCES app.conversation (id), 
    CONSTRAINT fk_feedback_entry_message_id FOREIGN KEY(message_id) REFERENCES app.message_index (id), 
    CONSTRAINT uq_feedback_entry_message_user UNIQUE (message_id, user_id)
);

CREATE INDEX ix_feedback_entry_tenant_id ON app.feedback_entry (tenant_id);

CREATE INDEX ix_feedback_entry_rating ON app.feedback_entry (tenant_id, rating);

ALTER TABLE app."feedback_entry" ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_feedback_entry ON app."feedback_entry" FOR ALL USING (app.tenant_filter_check()) WITH CHECK (app.tenant_filter_check());

UPDATE app.alembic_version SET version_num='0008_chat_feedback' WHERE app.alembic_version.version_num = '0007_memory_bitemporal';

-- Running upgrade 0008_chat_feedback -> 0009_fail_closed_rls

CREATE FUNCTION app.tenant_filter_check(row_tenant uuid)
        RETURNS boolean LANGUAGE plpgsql STABLE SECURITY INVOKER
        SET search_path = pg_catalog AS $$
        DECLARE current_tid text;
        BEGIN
            current_tid := current_setting('app.current_tenant_id', true);
            IF current_tid IS NULL OR current_tid = '' THEN RETURN false; END IF;
            RETURN COALESCE(row_tenant = current_tid::uuid, false);
        EXCEPTION WHEN invalid_text_representation THEN RETURN false;
        END;
        $$;

ALTER TABLE app."knowledge_base" ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_knowledge_base ON app."knowledge_base";

CREATE POLICY tenant_isolation_knowledge_base ON app."knowledge_base" FOR ALL USING (app.tenant_filter_check(tenant_id)) WITH CHECK (app.tenant_filter_check(tenant_id));

ALTER TABLE app."document_record" ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_document_record ON app."document_record";

CREATE POLICY tenant_isolation_document_record ON app."document_record" FOR ALL USING (app.tenant_filter_check(tenant_id)) WITH CHECK (app.tenant_filter_check(tenant_id));

ALTER TABLE app."chunk_manifest" ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_chunk_manifest ON app."chunk_manifest";

CREATE POLICY tenant_isolation_chunk_manifest ON app."chunk_manifest" FOR ALL USING (app.tenant_filter_check(tenant_id)) WITH CHECK (app.tenant_filter_check(tenant_id));

ALTER TABLE app."workflow_definition" ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_workflow_definition ON app."workflow_definition";

CREATE POLICY tenant_isolation_workflow_definition ON app."workflow_definition" FOR ALL USING (app.tenant_filter_check(tenant_id)) WITH CHECK (app.tenant_filter_check(tenant_id));

ALTER TABLE app."workflow_version" ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_workflow_version ON app."workflow_version";

CREATE POLICY tenant_isolation_workflow_version ON app."workflow_version" FOR ALL USING (app.tenant_filter_check(tenant_id)) WITH CHECK (app.tenant_filter_check(tenant_id));

ALTER TABLE app."conversation" ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_conversation ON app."conversation";

CREATE POLICY tenant_isolation_conversation ON app."conversation" FOR ALL USING (app.tenant_filter_check(tenant_id)) WITH CHECK (app.tenant_filter_check(tenant_id));

ALTER TABLE app."message_index" ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_message_index ON app."message_index";

CREATE POLICY tenant_isolation_message_index ON app."message_index" FOR ALL USING (app.tenant_filter_check(tenant_id)) WITH CHECK (app.tenant_filter_check(tenant_id));

ALTER TABLE app."memory_summary" ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_memory_summary ON app."memory_summary";

CREATE POLICY tenant_isolation_memory_summary ON app."memory_summary" FOR ALL USING (app.tenant_filter_check(tenant_id)) WITH CHECK (app.tenant_filter_check(tenant_id));

ALTER TABLE app."memory_fact" ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_memory_fact ON app."memory_fact";

CREATE POLICY tenant_isolation_memory_fact ON app."memory_fact" FOR ALL USING (app.tenant_filter_check(tenant_id)) WITH CHECK (app.tenant_filter_check(tenant_id));

ALTER TABLE app."run" ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_run ON app."run";

CREATE POLICY tenant_isolation_run ON app."run" FOR ALL USING (app.tenant_filter_check(tenant_id)) WITH CHECK (app.tenant_filter_check(tenant_id));

ALTER TABLE app."run_summary" ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_run_summary ON app."run_summary";

CREATE POLICY tenant_isolation_run_summary ON app."run_summary" FOR ALL USING (app.tenant_filter_check(tenant_id)) WITH CHECK (app.tenant_filter_check(tenant_id));

ALTER TABLE app."task" ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_task ON app."task";

CREATE POLICY tenant_isolation_task ON app."task" FOR ALL USING (app.tenant_filter_check(tenant_id)) WITH CHECK (app.tenant_filter_check(tenant_id));

ALTER TABLE app."user" ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_user ON app."user";

CREATE POLICY tenant_isolation_user ON app."user" FOR ALL USING (app.tenant_filter_check(tenant_id)) WITH CHECK (app.tenant_filter_check(tenant_id));

ALTER TABLE app."tenant_domain" ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_tenant_domain ON app."tenant_domain";

CREATE POLICY tenant_isolation_tenant_domain ON app."tenant_domain" FOR ALL USING (app.tenant_filter_check(tenant_id)) WITH CHECK (app.tenant_filter_check(tenant_id));

ALTER TABLE app."tenant_brand" ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_tenant_brand ON app."tenant_brand";

CREATE POLICY tenant_isolation_tenant_brand ON app."tenant_brand" FOR ALL USING (app.tenant_filter_check(tenant_id)) WITH CHECK (app.tenant_filter_check(tenant_id));

ALTER TABLE app."model_profile" ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_model_profile ON app."model_profile";

CREATE POLICY tenant_isolation_model_profile ON app."model_profile" FOR ALL USING (app.tenant_filter_check(tenant_id)) WITH CHECK (app.tenant_filter_check(tenant_id));

ALTER TABLE app."tenant_model_binding" ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_tenant_model_binding ON app."tenant_model_binding";

CREATE POLICY tenant_isolation_tenant_model_binding ON app."tenant_model_binding" FOR ALL USING (app.tenant_filter_check(tenant_id)) WITH CHECK (app.tenant_filter_check(tenant_id));

ALTER TABLE app."retriever_profile_version" ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_retriever_profile_version ON app."retriever_profile_version";

CREATE POLICY tenant_isolation_retriever_profile_version ON app."retriever_profile_version" FOR ALL USING (app.tenant_filter_check(tenant_id)) WITH CHECK (app.tenant_filter_check(tenant_id));

ALTER TABLE app."eval_case" ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_eval_case ON app."eval_case";

CREATE POLICY tenant_isolation_eval_case ON app."eval_case" FOR ALL USING (app.tenant_filter_check(tenant_id)) WITH CHECK (app.tenant_filter_check(tenant_id));

ALTER TABLE app."eval_run" ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_eval_run ON app."eval_run";

CREATE POLICY tenant_isolation_eval_run ON app."eval_run" FOR ALL USING (app.tenant_filter_check(tenant_id)) WITH CHECK (app.tenant_filter_check(tenant_id));

ALTER TABLE app."grader_validation" ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_grader_validation ON app."grader_validation";

CREATE POLICY tenant_isolation_grader_validation ON app."grader_validation" FOR ALL USING (app.tenant_filter_check(tenant_id)) WITH CHECK (app.tenant_filter_check(tenant_id));

ALTER TABLE app."reranker_artifact" ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_reranker_artifact ON app."reranker_artifact";

CREATE POLICY tenant_isolation_reranker_artifact ON app."reranker_artifact" FOR ALL USING (app.tenant_filter_check(tenant_id)) WITH CHECK (app.tenant_filter_check(tenant_id));

ALTER TABLE app."memory_profile_version" ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_memory_profile_version ON app."memory_profile_version";

CREATE POLICY tenant_isolation_memory_profile_version ON app."memory_profile_version" FOR ALL USING (app.tenant_filter_check(tenant_id)) WITH CHECK (app.tenant_filter_check(tenant_id));

ALTER TABLE app."feedback_entry" ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_feedback_entry ON app."feedback_entry";

CREATE POLICY tenant_isolation_feedback_entry ON app."feedback_entry" FOR ALL USING (app.tenant_filter_check(tenant_id)) WITH CHECK (app.tenant_filter_check(tenant_id));

ALTER TABLE search."chunk_stable" ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_chunk_stable ON search."chunk_stable";

CREATE POLICY tenant_isolation_chunk_stable ON search."chunk_stable" FOR ALL USING (app.tenant_filter_check(tenant_id)) WITH CHECK (app.tenant_filter_check(tenant_id));

ALTER TABLE search."chunk_hot" ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_chunk_hot ON search."chunk_hot";

CREATE POLICY tenant_isolation_chunk_hot ON search."chunk_hot" FOR ALL USING (app.tenant_filter_check(tenant_id)) WITH CHECK (app.tenant_filter_check(tenant_id));

ALTER TABLE search."memory" ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_memory ON search."memory";

CREATE POLICY tenant_isolation_memory ON search."memory" FOR ALL USING (app.tenant_filter_check(tenant_id)) WITH CHECK (app.tenant_filter_check(tenant_id));

DROP FUNCTION app.tenant_filter_check();

UPDATE app.alembic_version SET version_num='0009_fail_closed_rls' WHERE app.alembic_version.version_num = '0008_chat_feedback';

-- Running upgrade 0009_fail_closed_rls -> 0010_immutable_evidence_lineage

ALTER TABLE app.chunk_manifest ALTER COLUMN parser_version TYPE VARCHAR(128);

ALTER TABLE app.document_record ADD CONSTRAINT uq_document_owner_id UNIQUE (tenant_id, kb_id, id);

ALTER TABLE app.document_record ADD COLUMN active_index_id UUID;

CREATE TABLE app.document_version (
    id UUID NOT NULL, 
    tenant_id UUID NOT NULL, 
    kb_id UUID NOT NULL, 
    document_id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    source_digest VARCHAR(64) NOT NULL, 
    source_ref VARCHAR(1024) NOT NULL, 
    media_type VARCHAR(128) NOT NULL, 
    CONSTRAINT pk_document_version PRIMARY KEY (id), 
    CONSTRAINT uq_document_version_owner_id UNIQUE (tenant_id, kb_id, document_id, id), 
    CONSTRAINT uq_document_version_digest UNIQUE (tenant_id, document_id, source_digest), 
    CONSTRAINT fk_document_version_owner FOREIGN KEY(tenant_id, kb_id, document_id) REFERENCES app.document_record (tenant_id, kb_id, id), 
    CONSTRAINT fk_document_version_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES app.tenant (id)
);

CREATE INDEX ix_document_version_tenant_id ON app.document_version (tenant_id);

CREATE TABLE app.document_representation (
    id UUID NOT NULL, 
    tenant_id UUID NOT NULL, 
    kb_id UUID NOT NULL, 
    document_id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    document_version_id UUID NOT NULL, 
    representation_digest VARCHAR(64) NOT NULL, 
    representation_ref VARCHAR(1024) NOT NULL, 
    parser_version VARCHAR(128) NOT NULL, 
    render_version VARCHAR(128) NOT NULL, 
    parent_unit VARCHAR(16) NOT NULL, 
    char_count INTEGER NOT NULL, 
    page_count INTEGER, 
    CONSTRAINT pk_document_representation PRIMARY KEY (id), 
    CONSTRAINT uq_representation_owner_id UNIQUE (tenant_id, kb_id, document_id, document_version_id, id), 
    CONSTRAINT uq_representation_content_profile UNIQUE (tenant_id, document_version_id, representation_digest, parser_version, render_version), 
    CONSTRAINT fk_representation_version_owner FOREIGN KEY(tenant_id, kb_id, document_id, document_version_id) REFERENCES app.document_version (tenant_id, kb_id, document_id, id), 
    CONSTRAINT fk_document_representation_tenant_id_tenant FOREIGN KEY(tenant_id) REFERENCES app.tenant (id)
);

CREATE INDEX ix_document_representation_tenant_id ON app.document_representation (tenant_id);

ALTER TABLE app.chunk_manifest ADD COLUMN document_version_id UUID;

ALTER TABLE app.chunk_manifest ADD COLUMN representation_id UUID;

ALTER TABLE app.chunk_manifest ADD COLUMN chunker_config_digest VARCHAR(64);

ALTER TABLE app.chunk_manifest ADD COLUMN embedding_version VARCHAR(128);

ALTER TABLE app.chunk_manifest ADD COLUMN plan_ref VARCHAR(1024);

ALTER TABLE app.chunk_manifest ADD COLUMN plan_digest VARCHAR(64);

ALTER TABLE app.chunk_manifest ADD COLUMN legacy_indexing_state VARCHAR(32);

UPDATE app.chunk_manifest SET legacy_indexing_state=indexing_state, indexing_state='unversioned' WHERE document_version_id IS NULL;

ALTER TABLE app.chunk_manifest ADD CONSTRAINT uq_manifest_owner_id UNIQUE (tenant_id, kb_id, document_id, id);

ALTER TABLE app.chunk_manifest ADD CONSTRAINT uq_manifest_lineage_id UNIQUE (tenant_id, kb_id, document_id, document_version_id, representation_id, id);

ALTER TABLE app.chunk_manifest ADD CONSTRAINT fk_manifest_representation_owner FOREIGN KEY(tenant_id, kb_id, document_id, document_version_id, representation_id) REFERENCES app.document_representation (tenant_id, kb_id, document_id, document_version_id, id);

ALTER TABLE app.document_record ADD CONSTRAINT fk_document_active_index_owner FOREIGN KEY(tenant_id, kb_id, id, active_index_id) REFERENCES app.chunk_manifest (tenant_id, kb_id, document_id, id);

ALTER TABLE search.chunk_stable ADD COLUMN document_version_id UUID;

ALTER TABLE search.chunk_stable ADD COLUMN representation_id UUID;

ALTER TABLE search.chunk_stable ADD COLUMN index_id UUID;

ALTER TABLE search.chunk_stable ADD CONSTRAINT fk_stable_manifest_lineage FOREIGN KEY(tenant_id, kb_id, document_id, document_version_id, representation_id, index_id) REFERENCES app.chunk_manifest (tenant_id, kb_id, document_id, document_version_id, representation_id, id);

ALTER TABLE search.chunk_hot ADD COLUMN document_version_id UUID;

ALTER TABLE search.chunk_hot ADD COLUMN representation_id UUID;

ALTER TABLE search.chunk_hot ADD COLUMN index_id UUID;

ALTER TABLE search.chunk_hot ADD CONSTRAINT fk_hot_manifest_lineage FOREIGN KEY(tenant_id, kb_id, document_id, document_version_id, representation_id, index_id) REFERENCES app.chunk_manifest (tenant_id, kb_id, document_id, document_version_id, representation_id, id);

ALTER TABLE app."document_version" ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_document_version ON app."document_version" FOR ALL USING (app.tenant_filter_check(tenant_id)) WITH CHECK (app.tenant_filter_check(tenant_id));

ALTER TABLE app."document_representation" ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_document_representation ON app."document_representation" FOR ALL USING (app.tenant_filter_check(tenant_id)) WITH CHECK (app.tenant_filter_check(tenant_id));

CREATE FUNCTION app.reject_source_mutation() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
            IF (to_jsonb(NEW)-'updated_at') IS DISTINCT FROM (to_jsonb(OLD)-'updated_at') THEN
                RAISE EXCEPTION 'immutable source identity cannot be modified'
                    USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END; $$;

CREATE TRIGGER immutable_source BEFORE UPDATE ON app."document_version" FOR EACH ROW EXECUTE FUNCTION app.reject_source_mutation();

CREATE TRIGGER immutable_source BEFORE UPDATE ON app."document_representation" FOR EACH ROW EXECUTE FUNCTION app.reject_source_mutation();

UPDATE app.alembic_version SET version_num='0010_immutable_evidence_lineage' WHERE app.alembic_version.version_num = '0009_fail_closed_rls';

-- Running upgrade 0010_immutable_evidence_lineage -> 0011_index_reconciliation

ALTER TABLE app.chunk_manifest ADD COLUMN pg_digest VARCHAR(64);

ALTER TABLE app.chunk_manifest ADD COLUMN vector_digest VARCHAR(64);

ALTER TABLE app.chunk_manifest ADD COLUMN reconciled_at TIMESTAMP WITH TIME ZONE;

UPDATE app.chunk_manifest SET indexing_state='failed', error_message='Index requires verified two-store readback' WHERE indexing_state='indexed' AND (pg_digest IS NULL OR vector_digest IS NULL OR reconciled_at IS NULL OR pg_digest IS DISTINCT FROM vector_digest);

UPDATE app.document_record SET active_index_id=NULL, ingest_status='failed' WHERE active_index_id IN (SELECT id FROM app.chunk_manifest WHERE indexing_state='failed' AND reconciled_at IS NULL);

ALTER TABLE app.chunk_manifest ADD CONSTRAINT ck_chunk_manifest_ck_manifest_readback_publication CHECK (indexing_state <> 'indexed' OR (pg_digest IS NOT NULL AND vector_digest IS NOT NULL AND vector_digest = pg_digest AND reconciled_at IS NOT NULL));

CREATE FUNCTION app.reject_manifest_rewrite() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
            IF ROW(NEW.id, NEW.tenant_id, NEW.kb_id, NEW.document_id, NEW.document_version_id,
                   NEW.representation_id, NEW.chunker_version, NEW.chunker_config_digest,
                   NEW.embedding_profile, NEW.embedding_version, NEW.plan_ref, NEW.plan_digest,
                   NEW.parent_unit, NEW.page_count, NEW.parser_version, NEW.chunk_total)
            IS DISTINCT FROM
               ROW(OLD.id, OLD.tenant_id, OLD.kb_id, OLD.document_id, OLD.document_version_id,
                   OLD.representation_id, OLD.chunker_version, OLD.chunker_config_digest,
                   OLD.embedding_profile, OLD.embedding_version, OLD.plan_ref, OLD.plan_digest,
                   OLD.parent_unit, OLD.page_count, OLD.parser_version, OLD.chunk_total) THEN
                RAISE EXCEPTION 'immutable index plan cannot be changed' USING ERRCODE='23514';
            END IF;
            IF NEW.indexing_state='indexed' AND
               (NEW.pg_digest IS NULL OR NEW.vector_digest IS NULL OR NEW.reconciled_at IS NULL
                OR NEW.pg_digest IS DISTINCT FROM NEW.vector_digest) THEN
                RAISE EXCEPTION 'index publication requires readback facts' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END; $$;

CREATE TRIGGER immutable_manifest BEFORE UPDATE ON app.chunk_manifest
        FOR EACH ROW EXECUTE FUNCTION app.reject_manifest_rewrite();

CREATE FUNCTION search.reject_chunk_rewrite() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
            IF (to_jsonb(NEW)-'updated_at'-'expires_at'-'search_vector') IS DISTINCT FROM
               (to_jsonb(OLD)-'updated_at'-'expires_at'-'search_vector') THEN
                RAISE EXCEPTION 'immutable evidence chunk cannot be changed' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END; $$;

CREATE TRIGGER immutable_chunk BEFORE UPDATE ON search."chunk_stable" FOR EACH ROW EXECUTE FUNCTION search.reject_chunk_rewrite();

CREATE TRIGGER immutable_chunk BEFORE UPDATE ON search."chunk_hot" FOR EACH ROW EXECUTE FUNCTION search.reject_chunk_rewrite();

CREATE FUNCTION search.guard_published_chunk_set() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$
        DECLARE owner_index uuid; owner_tenant uuid; index_state text; replay_exists boolean;
        BEGIN
            IF TG_OP='DELETE' THEN
                owner_index := OLD.index_id; owner_tenant := OLD.tenant_id;
            ELSE
                owner_index := NEW.index_id; owner_tenant := NEW.tenant_id;
            END IF;
            IF owner_index IS NULL THEN
                IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
            END IF;
            SELECT indexing_state INTO index_state FROM app.chunk_manifest
                WHERE id=owner_index AND tenant_id=owner_tenant FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'index owner is unavailable' USING ERRCODE='23514';
            END IF;
            IF index_state='indexed' THEN
                IF TG_OP='DELETE' THEN
                    RAISE EXCEPTION 'published evidence cannot be deleted'
                        USING ERRCODE='23514';
                END IF;
                EXECUTE format(
                    'SELECT EXISTS(SELECT 1 FROM search.%%I WHERE id=$1 AND tenant_id=$2)',
                    TG_TABLE_NAME) INTO replay_exists USING NEW.id, NEW.tenant_id;
                IF NOT replay_exists THEN
                    RAISE EXCEPTION 'published index cannot gain new evidence'
                        USING ERRCODE='23514';
                END IF;
            END IF;
            IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
        END; $$;

CREATE TRIGGER published_chunk_set BEFORE INSERT OR DELETE ON search."chunk_stable" FOR EACH ROW EXECUTE FUNCTION search.guard_published_chunk_set();

CREATE TRIGGER published_chunk_set BEFORE INSERT OR DELETE ON search."chunk_hot" FOR EACH ROW EXECUTE FUNCTION search.guard_published_chunk_set();

UPDATE app.alembic_version SET version_num='0011_index_reconciliation' WHERE app.alembic_version.version_num = '0010_immutable_evidence_lineage';

-- Running upgrade 0011_index_reconciliation -> 0012_task_execution_lifecycle

ALTER TABLE app.task ADD COLUMN timeout_seconds INTEGER DEFAULT 300 NOT NULL;

ALTER TABLE app.task ADD COLUMN deadline_at TIMESTAMP WITH TIME ZONE;

ALTER TABLE app.task ADD COLUMN cancel_requested_at TIMESTAMP WITH TIME ZONE;

ALTER TABLE app.task ADD COLUMN finished_at TIMESTAMP WITH TIME ZONE;

ALTER TABLE app.task ADD COLUMN error_code VARCHAR(64);

ALTER TABLE app.task ADD CONSTRAINT ck_task_ck_task_timeout_budget CHECK (timeout_seconds BETWEEN 1 AND 3600);

ALTER TABLE app.task ADD CONSTRAINT ck_task_ck_task_attempts_nonnegative CHECK (attempts >= 0);

ALTER TABLE app.task ADD CONSTRAINT ck_task_ck_task_lifecycle_state CHECK (status IN ('pending','retryable','leased','completed','failed','cancelled','timed_out'));

CREATE FUNCTION app.reject_task_envelope_rewrite() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
            IF ROW(NEW.id,NEW.tenant_id,NEW.kind,NEW.payload,
                   NEW.idempotency_key,NEW.timeout_seconds)
            IS DISTINCT FROM
               ROW(OLD.id,OLD.tenant_id,OLD.kind,OLD.payload,OLD.idempotency_key,OLD.timeout_seconds)
            THEN RAISE EXCEPTION 'immutable task envelope cannot be changed' USING ERRCODE='23514';
            END IF;
            IF OLD.deadline_at IS NOT NULL AND NEW.deadline_at IS DISTINCT FROM OLD.deadline_at
            THEN RAISE EXCEPTION 'task total deadline cannot be changed' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END; $$;

CREATE TRIGGER immutable_task_envelope BEFORE UPDATE ON app.task FOR EACH ROW EXECUTE FUNCTION app.reject_task_envelope_rewrite();

UPDATE app.alembic_version SET version_num='0012_task_execution_lifecycle' WHERE app.alembic_version.version_num = '0011_index_reconciliation';

COMMIT;

