CREATE TABLE processor_executions (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 execution_id UUID NOT NULL UNIQUE, request JSONB NOT NULL,
 status VARCHAR(16) NOT NULL CHECK(status IN ('RUNNING','SUCCEEDED','FAILED')),
 result JSONB,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE processor_outbox (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 event_id UUID NOT NULL UNIQUE, execution_id UUID NOT NULL UNIQUE REFERENCES processor_executions(execution_id),
 payload JSONB NOT NULL, trace_parent VARCHAR(55), published_at TIMESTAMPTZ,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX processor_pending ON processor_outbox(id) WHERE published_at IS NULL;
