-- EPC (Energy Performance Certificate) domestic data — MHCLG England & Wales, OGL v3.0.
-- Adds floor area / £-per-m² / energy rating to the sold-price data by matching certificates
-- to transactions on address (PPD has no UPRN, so the join is address-based, not UPRN-to-UPRN).
--
-- Auto-runs on a FRESH volume after 01_schema.sql and 02_agent_readonly.sql. On the LIVE db
-- it must be applied by hand (the init dir only runs on first init of an empty data volume):
--   docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
--     -f /docker-entrypoint-initdb.d/03_epc_schema.sql
-- then re-apply 02_agent_readonly.sql for the new GRANTs. The first build of transaction_epc
-- scans all market_transactions once (a few minutes) and yields 0 rows until EPC data is loaded.
-- All objects are idempotent (IF NOT EXISTS / OR REPLACE) so re-applying is safe.

-- ---------------------------------------------------------------------------
-- 1. Base table: one row per certificate (LMK_KEY). All certs kept (a property
--    re-lodges over time); dedupe to one-per-property happens in epc_property.
--    norm_* match keys are computed once at ingest (see ingest/epc_ingest.py).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS epc_certificates (
    lmk_key                    TEXT PRIMARY KEY,          -- unique certificate id
    uprn                       BIGINT,                    -- nullable; for EPC dedupe + future bridges
    uprn_source                TEXT,
    building_reference_number  TEXT,

    address1                   TEXT,                      -- raw, as lodged (kept for audit)
    address2                   TEXT,
    address3                   TEXT,
    postcode                   TEXT,

    current_energy_rating      CHAR(1),                   -- A..G
    current_energy_efficiency  SMALLINT,                  -- 1..100 SAP points; nullable
    property_type              TEXT,                      -- House/Bungalow/Flat/Maisonette/Park home
    built_form                 TEXT,                      -- Detached/Semi-Detached/Mid-Terrace/...
    transaction_type           TEXT,                      -- marketed sale / new dwelling / rental ...
    tenure                     TEXT,                      -- Owner-occupied / Rented (private|social) ...

    total_floor_area           REAL,                      -- m²; nullable; outliers clamped downstream
    number_habitable_rooms     REAL,                      -- coarse size proxy (NOT bedrooms); nullable
    number_heated_rooms        REAL,

    inspection_date            DATE,
    lodgement_date             DATE,
    lodgement_datetime         TIMESTAMP,

    local_authority            TEXT,
    constituency               TEXT,
    county                     TEXT,

    -- normalised match keys (filled at ingest; mirror the PPD-side SQL in transaction_epc)
    norm_postcode              TEXT,                      -- 'CR0 2EF' -> 'CR02EF'
    paon_number                TEXT,                      -- leading number of address1, e.g. '14', '14A'
    paon_name                  TEXT,                      -- house name (upper, punctuation-stripped) when no number
    saon_token                 TEXT,                      -- flat/unit integer token when present
    norm_street                TEXT,                      -- thoroughfare upper, punctuation-stripped, abbrev-folded

    ingested_at                TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_epc_norm_postcode  ON epc_certificates(norm_postcode);
CREATE INDEX IF NOT EXISTS idx_epc_pc_paonnum     ON epc_certificates(norm_postcode, paon_number);
CREATE INDEX IF NOT EXISTS idx_epc_pc_paonname    ON epc_certificates(norm_postcode, paon_name);
CREATE INDEX IF NOT EXISTS idx_epc_uprn           ON epc_certificates(uprn) WHERE uprn IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_epc_lodgement      ON epc_certificates(lodgement_date);

COMMENT ON TABLE epc_certificates IS
  'MHCLG domestic EPC certificates (England & Wales, OGL v3.0). One row per LMK_KEY; a property has many.';

-- ---------------------------------------------------------------------------
-- 2. epc_property: one row per physical property (latest certificate).
--    Dedupe key prefers UPRN (authoritative) else the normalised address tuple.
--    Floor area is essentially static, so the latest lodgement reflects current state.
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS epc_property AS
WITH keyed AS (
    SELECT *,
           COALESCE(NULLIF(uprn::text, ''),
                    norm_postcode || '|' ||
                    COALESCE(NULLIF(paon_number, ''), NULLIF(paon_name, ''), '') || '|' ||
                    COALESCE(saon_token, '')) AS property_key
    FROM epc_certificates
),
ranked AS (
    SELECT *,
           ROW_NUMBER() OVER (
               PARTITION BY property_key
               ORDER BY lodgement_date DESC NULLS LAST,
                        lodgement_datetime DESC NULLS LAST,
                        lmk_key DESC
           ) AS rn
    FROM keyed
)
SELECT property_key,
       lmk_key                AS latest_lmk_key,
       uprn,
       norm_postcode, paon_number, paon_name, saon_token, norm_street, postcode,
       property_type, built_form, tenure,
       current_energy_rating, current_energy_efficiency,
       total_floor_area, number_habitable_rooms,
       lodgement_date         AS latest_lodgement_date
FROM ranked
WHERE rn = 1;

CREATE UNIQUE INDEX IF NOT EXISTS idx_epc_property_key      ON epc_property(property_key);
CREATE INDEX        IF NOT EXISTS idx_epc_property_pc_num   ON epc_property(norm_postcode, paon_number);
CREATE INDEX        IF NOT EXISTS idx_epc_property_pc_name  ON epc_property(norm_postcode, paon_name);

-- ---------------------------------------------------------------------------
-- 3. transaction_epc: best EPC property per market transaction (≤1 each).
--    Tiered address match; the LATERAL ... LIMIT 1 picks the best tier, then the
--    cert closest to the sale date, preferring one with a floor area. PPD side is
--    normalised here in SQL with the SAME rules used at EPC ingest.
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS transaction_epc AS
WITH ppd AS (
    SELECT transaction_id, date, price,
           UPPER(REPLACE(postcode, ' ', ''))                                        AS norm_postcode,
           NULLIF((regexp_match(UPPER(COALESCE(paon, '')), '^([0-9]+[A-Z]?)'))[1], '') AS paon_number,
           CASE WHEN COALESCE(paon, '') ~ '^[0-9]' THEN NULL
                ELSE NULLIF(regexp_replace(UPPER(COALESCE(paon, '')), '[^A-Z0-9 ]', '', 'g'), '') END AS paon_name,
           NULLIF((regexp_match(UPPER(COALESCE(saon, '')), '([0-9]+)'))[1], '')      AS saon_token,
           NULLIF(regexp_replace(UPPER(COALESCE(street, '')), '[^A-Z0-9 ]', '', 'g'), '') AS norm_street
    FROM market_transactions
)
SELECT p.transaction_id,
       e.latest_lmk_key                              AS lmk_key,
       e.total_floor_area,
       e.number_habitable_rooms                      AS habitable_rooms,
       e.current_energy_rating,
       CASE WHEN e.total_floor_area > 0
            THEN ROUND(p.price / e.total_floor_area)::int END AS price_per_sqm,
       e.method                                      AS match_method,
       e.confidence                                  AS match_confidence,
       e.property_type                               AS epc_property_type,  -- House/Bungalow/Flat/Maisonette
       e.built_form,                                                        -- Detached/Semi/Mid-Terrace/...
       e.current_energy_efficiency                                          -- SAP score 1..100 (numeric)
FROM ppd p
CROSS JOIN LATERAL (
    SELECT cand.latest_lmk_key, cand.total_floor_area, cand.number_habitable_rooms,
           cand.current_energy_rating, cand.method, cand.confidence,
           cand.property_type, cand.built_form, cand.current_energy_efficiency
    FROM (
        SELECT ep.*, 'postcode_paon_num_saon' AS method, 0.95 AS confidence, 1 AS prio
          FROM epc_property ep
         WHERE ep.norm_postcode = p.norm_postcode
           AND p.paon_number IS NOT NULL AND ep.paon_number = p.paon_number
           AND p.saon_token  IS NOT NULL AND ep.saon_token  = p.saon_token
        UNION ALL
        SELECT ep.*, 'postcode_paon_num', 0.90, 2
          FROM epc_property ep
         WHERE ep.norm_postcode = p.norm_postcode
           AND p.paon_number IS NOT NULL AND ep.paon_number = p.paon_number
           AND COALESCE(ep.saon_token, '') = '' AND COALESCE(p.saon_token, '') = ''
        UNION ALL
        SELECT ep.*, 'postcode_paon_name', 0.80, 3
          FROM epc_property ep
         WHERE ep.norm_postcode = p.norm_postcode
           AND p.paon_name IS NOT NULL AND ep.paon_name = p.paon_name
        UNION ALL
        SELECT ep.*, 'postcode_street_paon_num', 0.85, 4
          FROM epc_property ep
         WHERE ep.norm_postcode = p.norm_postcode
           AND p.norm_street IS NOT NULL AND ep.norm_street = p.norm_street
           AND p.paon_number IS NOT NULL AND ep.paon_number = p.paon_number
    ) cand
    ORDER BY cand.prio,
             ABS(cand.latest_lodgement_date - p.date),
             (cand.total_floor_area IS NULL),
             cand.latest_lmk_key
    LIMIT 1
) e;

CREATE UNIQUE INDEX IF NOT EXISTS idx_transaction_epc_txn ON transaction_epc(transaction_id);

-- ---------------------------------------------------------------------------
-- 4. epc_match_coverage: the honesty denominator (per year). Surfaced via
--    get_data_coverage + /ingest/coverage so a £/m² figure always carries its match %.
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS epc_match_coverage AS
SELECT date_trunc('year', mt.date)::date              AS year,
       count(*)                                        AS total_txns,
       count(te.transaction_id)                        AS matched_txns,
       round(100.0 * count(te.transaction_id) / NULLIF(count(*), 0), 1) AS pct_matched,
       count(te.price_per_sqm)                         AS with_floor_area
FROM market_transactions mt
LEFT JOIN transaction_epc te USING (transaction_id)
GROUP BY 1;

CREATE UNIQUE INDEX IF NOT EXISTS idx_epc_coverage_year ON epc_match_coverage(year);

-- ---------------------------------------------------------------------------
-- 5. Enriched access view: market_transactions + EPC attributes (NULL where unmatched).
--    Analytics opt in by reading this instead of market_transactions; existing queries
--    are unaffected. The join is on a unique key, so it stays cheap.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW market_transactions_epc AS
SELECT mt.*,
       te.lmk_key,
       te.total_floor_area,
       te.habitable_rooms,
       te.current_energy_rating,
       te.price_per_sqm,
       te.match_method,
       te.match_confidence,
       te.epc_property_type,          -- EPC's own type (House/Bungalow/Flat/Maisonette); distinct from PPD property_type
       te.built_form,                 -- Detached/Semi-Detached/Mid-Terrace/End-Terrace/...
       te.current_energy_efficiency   -- numeric SAP score (1..100); current_energy_rating is the A..G band
FROM market_transactions mt
LEFT JOIN transaction_epc te ON te.transaction_id = mt.transaction_id;

-- ---------------------------------------------------------------------------
-- 6. Refresh helper. STRICT ORDER: epc_property -> transaction_epc -> coverage.
--    Plain REFRESH on first build; callers may switch to CONCURRENTLY afterwards
--    (every matview here has a UNIQUE index). Does NOT touch monthly_price_stats.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION refresh_epc_stats()
RETURNS void AS $$
BEGIN
    REFRESH MATERIALIZED VIEW epc_property;
    REFRESH MATERIALIZED VIEW transaction_epc;
    REFRESH MATERIALIZED VIEW epc_match_coverage;
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------------------
-- 7. agent_ro grants. Done HERE (not in 02_agent_readonly.sql) because on a fresh
--    volume 02 runs before this file, so these objects don't exist there yet. The
--    agent_ro role itself was created by 02, which always runs first. Keep the refresh
--    function ungranted so the read-only role can never trigger an expensive rebuild.
-- ---------------------------------------------------------------------------
GRANT SELECT ON epc_certificates, epc_property, transaction_epc,
                market_transactions_epc, epc_match_coverage TO agent_ro;
REVOKE EXECUTE ON FUNCTION refresh_epc_stats() FROM agent_ro;
