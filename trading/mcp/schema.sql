CREATE TABLE IF NOT EXISTS filings (
    ticker      VARCHAR NOT NULL,
    filing_date VARCHAR NOT NULL,
    form_type   VARCHAR NOT NULL,
    content     TEXT,
    PRIMARY KEY (ticker, filing_date, form_type)
);

CREATE TABLE IF NOT EXISTS news (
    ticker  VARCHAR NOT NULL,
    date    VARCHAR NOT NULL,
    item_id INTEGER NOT NULL,
    content TEXT,
    PRIMARY KEY (ticker, date, item_id)
);

CREATE TABLE IF NOT EXISTS prices (
    ticker   VARCHAR NOT NULL,
    date     VARCHAR NOT NULL,
    price    DOUBLE,
    momentum VARCHAR,
    PRIMARY KEY (ticker, date)
);
