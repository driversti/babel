CREATE TABLE articles (
    id            bigint PRIMARY KEY,
    title         text        NOT NULL,
    body          text        NOT NULL,
    author_id     bigint,
    author_name   text,
    country       text,
    published_at  timestamptz NOT NULL,
    e_day         int,
    lang          char(3),
    comment_count int         NOT NULL DEFAULT 0,
    fetched_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX articles_published_at_idx ON articles (published_at DESC);
CREATE INDEX articles_country_idx      ON articles (country);
CREATE INDEX articles_author_idx       ON articles (author_id);

CREATE TABLE comments (
    id          bigint PRIMARY KEY,
    article_id  bigint      NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    position    int         NOT NULL,
    depth       smallint    NOT NULL DEFAULT 0,
    author_id   bigint,
    author_name text,
    posted_at   timestamptz,
    body        text
);

CREATE INDEX comments_article_idx ON comments (article_id, position);
CREATE INDEX comments_author_idx  ON comments (author_id);

CREATE TABLE images (
    sha256    bytea PRIMARY KEY,
    mime      text,
    bytes     int         NOT NULL,
    width     int,
    height    int,
    stored_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE article_images (
    article_id bigint      NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    position   int         NOT NULL,
    source_url text        NOT NULL,
    sha256     bytea       REFERENCES images(sha256),
    status     text        NOT NULL,
    checked_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (article_id, position)
);

CREATE INDEX article_images_status_idx ON article_images (status);

CREATE TABLE fetch_log (
    article_id bigint PRIMARY KEY,
    status     text        NOT NULL,
    attempts   smallint    NOT NULL DEFAULT 1,
    last_error text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX fetch_log_status_idx ON fetch_log (status);

CREATE TABLE crawl_cursor (
    name       text PRIMARY KEY,
    next_id    bigint      NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
