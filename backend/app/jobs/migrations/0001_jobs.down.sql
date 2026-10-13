-- 回復 0001：只刪背景工作系統的資料表（先備份：pg_dump -t jobs -t job_events -t job_outbox -t job_artifacts -t resource_leases）
DROP TABLE IF EXISTS job_artifacts;
DROP TABLE IF EXISTS job_outbox;
DROP TABLE IF EXISTS job_events;
DROP TABLE IF EXISTS resource_leases;
DROP TABLE IF EXISTS jobs;
