CREATE TABLE IF NOT EXISTS audiobooks (
    id            BIGINT PRIMARY KEY,                  -- iTunes collectionId
    title         TEXT NOT NULL,
    author        TEXT,
    description   TEXT,                                -- plain text, HTML already stripped
    genre         TEXT,
    release_date  DATE,
    cover_url     TEXT,                                -- cover image at iTunes
    palette       JSONB,                               -- up to 5 "#rrggbb" colors, most dominant first
    content_hash  TEXT NOT NULL,                       -- fingerprint of the cleaned source fields
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()   -- when our copy last changed
);

-- Genre filter on the list endpoint and stats grouped by genre.
CREATE INDEX IF NOT EXISTS audiobooks_genre_idx ON audiobooks (genre);

-- Stats grouped by release year, and release-date filtering or sorting.
CREATE INDEX IF NOT EXISTS audiobooks_release_date_idx ON audiobooks (release_date);
