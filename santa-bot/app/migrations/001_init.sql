-- Initial schema (SPEC §3). Timestamps are UTC ISO-8601 text with milliseconds,
-- dates are 'YYYY-MM-DD', flags are 0/1 integers.

CREATE TABLE users (
    user_id             INTEGER PRIMARY KEY,           -- the MAX user id
    max_name            TEXT,                          -- stored only after consent
    username            TEXT,
    first_seen_at       TEXT    NOT NULL,
    first_source        TEXT    NOT NULL DEFAULT 'direct',  -- j:CODE | n:CODE | s:src | direct
    consent_at          TEXT,
    consent_version     TEXT,
    dm_ok               INTEGER NOT NULL DEFAULT 1,
    blocked             INTEGER NOT NULL DEFAULT 0,
    games_created_today INTEGER NOT NULL DEFAULT 0,
    games_created_day   TEXT                           -- Moscow date of the counter above
);

CREATE TABLE games (
    id                     INTEGER PRIMARY KEY,
    code                   TEXT    NOT NULL UNIQUE,
    title                  TEXT    NOT NULL,
    organizer_id           INTEGER NOT NULL REFERENCES users (user_id),
    organizer_participates INTEGER NOT NULL DEFAULT 1,
    budget_text            TEXT    NOT NULL,
    exchange_date          TEXT,
    status                 TEXT    NOT NULL DEFAULT 'collecting'
                           CHECK (status IN ('collecting', 'drawn', 'finished', 'cancelled')),
    tier                   TEXT    NOT NULL DEFAULT 'free' CHECK (tier IN ('free', 'S', 'M', 'L')),
    participant_limit      INTEGER NOT NULL,
    anon_chat              INTEGER NOT NULL DEFAULT 1,
    reminder_on            INTEGER NOT NULL DEFAULT 1,
    group_chat_id          INTEGER,
    group_card_mid         TEXT,
    source_game_id         INTEGER REFERENCES games (id) ON DELETE SET NULL,
    source                 TEXT    NOT NULL DEFAULT 'direct',  -- ref | participant | s:src | direct
    created_at             TEXT    NOT NULL,
    drawn_at               TEXT,
    finished_at            TEXT,
    cancelled_at           TEXT,
    reveal_done            INTEGER NOT NULL DEFAULT 0,
    last_join_notice_at    TEXT,
    last_waiting_notice_at TEXT,
    last_wish_reminder_at  TEXT,
    org_nudge_sent         INTEGER NOT NULL DEFAULT 0,
    pre_exchange_sent      INTEGER NOT NULL DEFAULT 0,
    redraw_count           INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX games_organizer ON games (organizer_id, created_at);
CREATE INDEX games_status_date ON games (status, exchange_date);
CREATE INDEX games_source_game ON games (source_game_id);
CREATE INDEX games_group_chat ON games (group_chat_id) WHERE group_chat_id IS NOT NULL;

CREATE TABLE user_state (
    user_id    INTEGER PRIMARY KEY REFERENCES users (user_id) ON DELETE CASCADE,
    kind       TEXT    NOT NULL CHECK (kind IN (
                   'resume', 'title', 'budget_custom', 'date_custom', 'wishes', 'display_name',
                   'code', 'relay_to_receiver', 'relay_to_santa', 'reply_relay', 'admin_input')),
    game_id    INTEGER REFERENCES games (id) ON DELETE CASCADE,
    data       TEXT    NOT NULL DEFAULT '{}',
    expires_at TEXT    NOT NULL
);

CREATE TABLE participants (
    id           INTEGER PRIMARY KEY,
    game_id      INTEGER NOT NULL REFERENCES games (id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    display_name TEXT    NOT NULL,
    wishes       TEXT,
    status       TEXT    NOT NULL CHECK (status IN ('active', 'waiting', 'left', 'removed')),
    joined_at    TEXT    NOT NULL,
    via          TEXT    NOT NULL CHECK (via IN ('link', 'code', 'group', 'ref', 'organizer')),
    gift_ready   INTEGER NOT NULL DEFAULT 0,
    result_dm_ok INTEGER,
    UNIQUE (game_id, user_id)
);
CREATE INDEX participants_user ON participants (user_id);
CREATE INDEX participants_game_status ON participants (game_id, status, joined_at);

CREATE TABLE exclusions (
    game_id INTEGER NOT NULL REFERENCES games (id) ON DELETE CASCADE,
    user_a  INTEGER NOT NULL,
    user_b  INTEGER NOT NULL,
    PRIMARY KEY (game_id, user_a, user_b),
    CHECK (user_a < user_b)
);

CREATE TABLE assignments (
    game_id     INTEGER NOT NULL REFERENCES games (id) ON DELETE CASCADE,
    giver_id    INTEGER NOT NULL,
    receiver_id INTEGER NOT NULL,
    PRIMARY KEY (game_id, giver_id),
    UNIQUE (game_id, receiver_id),
    CHECK (giver_id <> receiver_id)
);

-- Payments outlive their game (accounting): game_id becomes NULL when the game is purged.
CREATE TABLE payments (
    inv_id     INTEGER PRIMARY KEY AUTOINCREMENT,      -- the Robokassa InvId
    game_id    INTEGER REFERENCES games (id) ON DELETE SET NULL,
    payer_id   INTEGER,
    tier       TEXT    NOT NULL CHECK (tier IN ('S', 'M', 'L')),
    amount_rub INTEGER NOT NULL CHECK (amount_rub >= 0),
    status     TEXT    NOT NULL CHECK (status IN ('created', 'paid', 'refunded', 'granted')),
    provider   TEXT    NOT NULL CHECK (provider IN ('robokassa', 'manual')),
    created_at TEXT    NOT NULL,
    paid_at    TEXT,
    raw        TEXT
);
CREATE INDEX payments_game ON payments (game_id, status);
CREATE INDEX payments_paid_at ON payments (paid_at) WHERE paid_at IS NOT NULL;

CREATE TABLE relay_messages (
    id         INTEGER PRIMARY KEY,
    game_id    INTEGER NOT NULL REFERENCES games (id) ON DELETE CASCADE,
    from_id    INTEGER NOT NULL,
    to_id      INTEGER NOT NULL,
    direction  TEXT    NOT NULL CHECK (direction IN ('to_receiver', 'to_santa')),
    text       TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);
CREATE INDEX relay_sender_day ON relay_messages (from_id, game_id, created_at);
CREATE INDEX relay_created ON relay_messages (created_at);

CREATE TABLE reports (
    id          INTEGER PRIMARY KEY,
    relay_id    INTEGER REFERENCES relay_messages (id) ON DELETE SET NULL,
    game_id     INTEGER REFERENCES games (id) ON DELETE SET NULL,
    reporter_id INTEGER NOT NULL,
    reported_id INTEGER NOT NULL,
    text        TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    resolved    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX reports_open ON reports (resolved, created_at);

CREATE TABLE events (
    id      INTEGER PRIMARY KEY,
    ts      TEXT    NOT NULL,
    type    TEXT    NOT NULL,
    user_id INTEGER,
    game_id INTEGER,
    props   TEXT    NOT NULL DEFAULT '{}'
);
CREATE INDEX events_type_ts ON events (type, ts);
CREATE INDEX events_ts ON events (ts);

CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- purpose/game_id let delivery hooks track draw results (see app/outbox.py).
CREATE TABLE outbox (
    id              INTEGER PRIMARY KEY,
    kind            TEXT    NOT NULL CHECK (kind IN ('send', 'edit')),
    target_type     TEXT    NOT NULL CHECK (target_type IN ('user', 'chat')),
    target_id       INTEGER NOT NULL,
    message_id      TEXT,
    body            TEXT    NOT NULL,
    disable_preview INTEGER NOT NULL DEFAULT 0,
    not_before      TEXT    NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    status          TEXT    NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'dead')),
    last_error      TEXT,
    created_at      TEXT    NOT NULL,
    dedupe_key      TEXT    UNIQUE,
    purpose         TEXT,
    game_id         INTEGER
);
CREATE INDEX outbox_due ON outbox (status, not_before, id);
CREATE INDEX outbox_purpose ON outbox (purpose, game_id) WHERE purpose IS NOT NULL;

CREATE TABLE processed_updates (
    key TEXT PRIMARY KEY,
    ts  TEXT NOT NULL
);
CREATE INDEX processed_updates_ts ON processed_updates (ts);
