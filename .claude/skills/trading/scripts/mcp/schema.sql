CREATE TABLE IF NOT EXISTS filings (
    id            INTEGER PRIMARY KEY,
    symbol        VARCHAR NOT NULL,
    date          DATE NOT NULL,
    mda_content   TEXT,
    risk_content  TEXT,
    document_type VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS news (
    id         INTEGER PRIMARY KEY,
    symbol     VARCHAR NOT NULL,
    date       TIMESTAMP NOT NULL,
    title      TEXT,
    url        TEXT,
    highlights TEXT
);

CREATE TABLE IF NOT EXISTS prices (
    id        INTEGER PRIMARY KEY,
    symbol    VARCHAR NOT NULL,
    date      DATE NOT NULL,
    open      DOUBLE,
    high      DOUBLE,
    low       DOUBLE,
    close     DOUBLE,
    adj_close DOUBLE,
    volume    BIGINT
);
