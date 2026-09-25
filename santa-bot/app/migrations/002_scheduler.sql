-- Scheduler bookkeeping (SPEC §10).

-- Each periodic job's last run, so a restart neither repeats nor skips a daily job.
CREATE TABLE job_runs (
    name        TEXT PRIMARY KEY,
    last_run_at TEXT NOT NULL
);

-- When the last game of this user was purged by the data-retention job: users with
-- no games for 365 days are deleted (§10), which the purged game rows can no longer show.
ALTER TABLE users ADD COLUMN last_game_at TEXT;
