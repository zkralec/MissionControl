-- Create notification dedupe storage table for outbound channels.

CREATE TABLE IF NOT EXISTS notifications_sent (
    id BIGSERIAL PRIMARY KEY,
    channel TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_notifications_sent_channel_dedupe_key
    ON notifications_sent(channel, dedupe_key);

CREATE INDEX IF NOT EXISTS ix_notifications_sent_expires_at
    ON notifications_sent(expires_at);
