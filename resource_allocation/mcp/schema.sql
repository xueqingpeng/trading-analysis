CREATE TABLE IF NOT EXISTS company_states (
    company_id               VARCHAR NOT NULL,
    date                     DATE NOT NULL,
    cash_runway_months       DOUBLE,
    net_debt_to_ebitda       DOUBLE,
    revenue_growth_pct       DOUBLE,
    gross_margin_pct         DOUBLE,
    transformation_pressure  VARCHAR,
    capacity_constraint      VARCHAR,
    board_priority           TEXT,
    PRIMARY KEY (company_id, date)
);

CREATE TABLE IF NOT EXISTS business_units (
    company_id            VARCHAR NOT NULL,
    date                  DATE NOT NULL,
    unit_id               VARCHAR NOT NULL,
    current_capex_share   DOUBLE,
    roi_trend             VARCHAR,
    growth_outlook        VARCHAR,
    execution_risk        VARCHAR,
    absorptive_capacity   VARCHAR,
    strategic_role        VARCHAR,
    PRIMARY KEY (company_id, date, unit_id)
);

CREATE TABLE IF NOT EXISTS reallocation_constraints (
    company_id                 VARCHAR NOT NULL,
    date                       DATE NOT NULL,
    reallocatable_share_cap    DOUBLE,
    transfer_rules_json        TEXT,
    PRIMARY KEY (company_id, date)
);

CREATE TABLE IF NOT EXISTS unit_constraints (
    company_id     VARCHAR NOT NULL,
    date           DATE NOT NULL,
    unit_id        VARCHAR NOT NULL,
    floor_share    DOUBLE,
    ceiling_share  DOUBLE,
    locked_share   DOUBLE,
    PRIMARY KEY (company_id, date, unit_id)
);

CREATE TABLE IF NOT EXISTS role_briefs (
    company_id            VARCHAR NOT NULL,
    date                  DATE NOT NULL,
    role                  VARCHAR NOT NULL,
    priority              TEXT,
    watch_for_json        TEXT,
    must_optimize_json    TEXT,
    veto_if_json          TEXT,
    PRIMARY KEY (company_id, date, role)
);

CREATE TABLE IF NOT EXISTS benchmark_context (
    company_id                    VARCHAR NOT NULL,
    date                          DATE NOT NULL,
    mckinsey_reference            TEXT,
    boldness_threshold            DOUBLE,
    scenario_note                 TEXT,
    boldness_desirable_if_json    TEXT,
    boldness_undesirable_if_json  TEXT,
    PRIMARY KEY (company_id, date)
);

CREATE TABLE IF NOT EXISTS decision_history (
    company_id             VARCHAR NOT NULL,
    date                   DATE NOT NULL,
    allocation_plan_json   TEXT,
    decision_type          VARCHAR,
    reallocation_share     DOUBLE,
    ceo_rationale          TEXT,
    PRIMARY KEY (company_id, date)
);

CREATE TABLE IF NOT EXISTS hidden_outcome_targets (
    company_id                      VARCHAR NOT NULL,
    date                            DATE NOT NULL,
    preferred_min_share             DOUBLE,
    preferred_max_share             DOUBLE,
    preferred_receiving_units_json  TEXT,
    protected_units_json            TEXT,
    overloaded_if_funded_json       TEXT,
    downside_if_too_conservative    TEXT,
    downside_if_too_aggressive      TEXT,
    PRIMARY KEY (company_id, date)
);
