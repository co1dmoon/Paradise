-- Ad autopilot (PROMO_SPEC §4). Money is gross (VAT included) integer kopecks; days are
-- Moscow dates 'YYYY-MM-DD'; timestamps are UTC ISO text like everywhere else.
-- No personal data here except admin ids (created_by, decided_by): kept for the season.

-- Campaigns the autopilot manages; src is the attribution source: yd<campaignId> or vk<adPlanId>.
CREATE TABLE promo_campaigns (
    src                    TEXT    PRIMARY KEY,
    platform               TEXT    NOT NULL CHECK (platform IN ('yd', 'vk')),
    external_id            TEXT    NOT NULL,
    name                   TEXT    NOT NULL,
    state                  TEXT    NOT NULL DEFAULT 'other' CHECK (state IN ('active', 'paused', 'other')),
    budget_kind            TEXT    CHECK (budget_kind IN ('week', 'day')),
    budget_kop             INTEGER,              -- last known budget the autopilot may change, gross
    previous_budget_kop    INTEGER,              -- the budget(s) before the change on last_budget_change_day
    limit_kop              INTEGER,              -- a limit on total spend (VK: whole campaign, Direct: period)
    limit_start            TEXT,                 -- the limit's period (Direct); empty: the whole campaign
    limit_end              TEXT,
    pays_per_conversion    INTEGER NOT NULL DEFAULT 0,
    registered_at          TEXT    NOT NULL,
    enabled                INTEGER NOT NULL DEFAULT 1,
    last_budget_change_day TEXT,
    paused_by              TEXT    CHECK (paused_by IN ('autopilot', 'admin', 'cap', 'season', 'preseason')),
    changed_at             TEXT,                 -- the bot's last change of state or budget (older fetches lose)
    spend_checked_at       TEXT,                 -- when the spend was last fetched from the platform
    UNIQUE (platform, external_id)
);

-- Daily spend per campaign; platforms revise recent days, so fetched days are replaced.
CREATE TABLE promo_spend (
    src        TEXT    NOT NULL,
    day        TEXT    NOT NULL,
    cost_kop   INTEGER NOT NULL,
    clicks     INTEGER NOT NULL,
    fetched_at TEXT    NOT NULL,
    PRIMARY KEY (src, day)
);

-- Everything the autopilot did, would do (test mode) or proposes, and every admin action.
CREATE TABLE promo_actions (
    id         INTEGER PRIMARY KEY,
    ts         TEXT    NOT NULL,
    src        TEXT    NOT NULL,                  -- '*' for suspend_all
    action     TEXT    NOT NULL CHECK (action IN ('pause', 'resume', 'set_budget', 'suspend_all')),
    params     TEXT    NOT NULL DEFAULT '{}',
    reason     TEXT    NOT NULL DEFAULT '',
    mode       TEXT    NOT NULL CHECK (mode IN ('dry', 'auto', 'admin')),
    status     TEXT    NOT NULL CHECK (status IN ('proposed', 'applied', 'failed', 'declined', 'expired')),
    error      TEXT,
    decided_by INTEGER
);
CREATE INDEX promo_actions_status ON promo_actions (status, ts);

-- Manual tracking links (/link): src is p<slug>.
CREATE TABLE promo_links (
    src        TEXT    PRIMARY KEY,
    title      TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    created_by INTEGER NOT NULL
);

-- Posts of the season calendar handed to the outbox for the owner's channel, skipped because
-- they were more than a day overdue, or refused by MAX (failed; /channel send tries again).
CREATE TABLE promo_posts_sent (
    post_id   TEXT    PRIMARY KEY,
    sent_at   TEXT    NOT NULL,
    mid       TEXT,
    status    TEXT    NOT NULL DEFAULT 'sent' CHECK (status IN ('sent', 'skipped', 'failed')),
    outbox_id INTEGER                          -- the outbox row of the last attempt
);

-- The VK Ads OAuth token pair (at most 5 tokens per client and user: reuse, refresh, never hoard).
CREATE TABLE promo_tokens (
    platform      TEXT PRIMARY KEY,
    access_token  TEXT NOT NULL,
    refresh_token TEXT,
    expires_at    TEXT NOT NULL
);

-- Channels the bot was added to (bot_added with is_channel), so /channel can show their ids.
CREATE TABLE promo_channels (
    chat_id  INTEGER PRIMARY KEY,
    added_at TEXT    NOT NULL
);

-- Attribution reads users by their first source.
CREATE INDEX users_first_source ON users (first_source);
