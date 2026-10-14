-- 背景工作系統（TASK-20261013-001、docs/adr/041）：工作、進度事件、outbox、產物、資源租約。
-- 回復：0001_jobs.down.sql（只刪這幾張表，不碰 uploads、紀錄與知識庫的資料）。

CREATE TABLE jobs (
  id               TEXT PRIMARY KEY,
  kind             TEXT NOT NULL CHECK (kind IN ('cad', 'schedule')),
  queue            TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'queued'
                   CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
  owner_id         TEXT NOT NULL,           -- 提交的帳號 id（不存 JWT）
  scope            TEXT NOT NULL,           -- 互斥範圍：schedule:plant、cad:<part|upload>
  params           JSONB NOT NULL DEFAULT '{}'::jsonb,
  resource         JSONB NOT NULL DEFAULT '{}'::jsonb,  -- 來源資源（授權依據）
  input_version    TEXT,                    -- 排程：提交當下的輸入版本
  input_versions   JSONB,                   -- 排程：工單／途程／機台／行事曆各自的版本
  idempotency_key  TEXT,
  request_hash     TEXT,
  request_id       TEXT,
  attempt          INTEGER NOT NULL DEFAULT 0,
  max_attempts     INTEGER NOT NULL DEFAULT 3,
  lease_owner      TEXT,
  lease_expires_at TIMESTAMPTZ,
  heartbeat_at     TIMESTAMPTZ,
  cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
  stop_requested   BOOLEAN NOT NULL DEFAULT FALSE,
  committing_attempt INTEGER,               -- 已進入寫回的執行嘗試（之後不能再取消）
  progress         JSONB,
  result           JSONB,
  error            JSONB,
  event_seq        INTEGER NOT NULL DEFAULT 0,
  dispatched_at    TIMESTAMPTZ,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at       TIMESTAMPTZ,
  finished_at      TIMESTAMPTZ
);
-- 相同帳號／操作／key 只會有一個工作（Idempotency-Key）
CREATE UNIQUE INDEX jobs_idempotency_uq ON jobs (owner_id, kind, idempotency_key)
  WHERE idempotency_key IS NOT NULL;
-- 同一個排程範圍同時只能有一個排隊中或執行中的工作
CREATE UNIQUE INDEX jobs_one_active_schedule_uq ON jobs (scope)
  WHERE kind = 'schedule' AND status IN ('queued', 'running');
CREATE INDEX jobs_status_lease_idx ON jobs (status, lease_expires_at);
CREATE INDEX jobs_owner_created_idx ON jobs (owner_id, created_at DESC);
CREATE INDEX jobs_finished_idx ON jobs (finished_at) WHERE finished_at IS NOT NULL;

-- 進度事件：同一個工作的 seq 連續遞增（Last-Event-ID 續接用）
CREATE TABLE job_events (
  job_id  TEXT NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
  seq     INTEGER NOT NULL,
  type    TEXT NOT NULL,
  data    JSONB NOT NULL DEFAULT '{}'::jsonb,
  at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (job_id, seq)
);

-- outbox：建立工作與投遞訊息同一個交易，dispatcher 負責可重試地投遞到 Redis
CREATE TABLE job_outbox (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  job_id            TEXT NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
  queue             TEXT NOT NULL,
  reason            TEXT NOT NULL DEFAULT 'created',
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  available_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  dispatched_at     TIMESTAMPTZ,
  dispatch_attempts INTEGER NOT NULL DEFAULT 0,
  last_error        TEXT
);
CREATE INDEX job_outbox_pending_idx ON job_outbox (available_at) WHERE dispatched_at IS NULL;

-- 受保護產物（檔案在 data/cad/<job_id>/，這裡只記白名單檔名、大小與雜湊）
CREATE TABLE job_artifacts (
  job_id       TEXT NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
  name         TEXT NOT NULL,
  content_type TEXT NOT NULL,
  size_bytes   BIGINT NOT NULL,
  sha256       TEXT NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (job_id, name)
);

-- 資源租約：排程範圍（schedule:plant）與 GPU 名額（gpu:0…）跨 worker 行程互斥；
-- fencing 每次換人遞增，過期的舊 worker 拿著舊的 attempt 不能寫回
CREATE TABLE resource_leases (
  resource   TEXT PRIMARY KEY,
  holder     TEXT NOT NULL,
  attempt    INTEGER NOT NULL DEFAULT 0,
  fencing    BIGINT NOT NULL DEFAULT 1,
  expires_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
