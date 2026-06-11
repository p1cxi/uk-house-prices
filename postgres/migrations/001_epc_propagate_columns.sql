-- Migration 001 — propagate EPC property_type / built_form / SAP score into transaction_epc + view.
--
-- transaction_epc is a MATERIALIZED VIEW, so adding columns means DROP + recreate (the
-- `CREATE ... IF NOT EXISTS` in 03_epc_schema.sql will NOT alter an existing matview on the live DB).
-- epc_match_coverage and market_transactions_epc depend on it, so they are dropped + recreated too.
-- Wrapped in ONE transaction: the swap is atomic — on any error it rolls back and the OLD (working)
-- objects are left intact. Definitions MIRROR 03_epc_schema.sql §3–§5 — keep them in sync. On a fresh
-- volume 03 already has these columns, so this migration is only for already-initialised databases.
--
-- Apply (the transaction_epc rebuild re-runs the LATERAL address match over all market_transactions —
-- several minutes; EPC-backed tools block until COMMIT, so run it as a coordinated maintenance step):
--   docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
--     -v ON_ERROR_STOP=1 -f /app/postgres/migrations/001_epc_propagate_columns.sql
\set ON_ERROR_STOP on

BEGIN;

DROP VIEW IF EXISTS market_transactions_epc;
DROP MATERIALIZED VIEW IF EXISTS epc_match_coverage;
DROP MATERIALIZED VIEW IF EXISTS transaction_epc;

-- §3 transaction_epc — now carrying epc_property_type / built_form / current_energy_efficiency (SAP).
CREATE MATERIALIZED VIEW transaction_epc AS
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
       e.property_type                               AS epc_property_type,
       e.built_form,
       e.current_energy_efficiency
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

CREATE UNIQUE INDEX idx_transaction_epc_txn ON transaction_epc(transaction_id);

-- §4 epc_match_coverage — unchanged definition (does not select the new columns).
CREATE MATERIALIZED VIEW epc_match_coverage AS
SELECT date_trunc('year', mt.date)::date              AS year,
       count(*)                                        AS total_txns,
       count(te.transaction_id)                        AS matched_txns,
       round(100.0 * count(te.transaction_id) / NULLIF(count(*), 0), 1) AS pct_matched,
       count(te.price_per_sqm)                         AS with_floor_area
FROM market_transactions mt
LEFT JOIN transaction_epc te USING (transaction_id)
GROUP BY 1;

CREATE UNIQUE INDEX idx_epc_coverage_year ON epc_match_coverage(year);

-- §5 enriched view — now exposing epc_property_type / built_form / current_energy_efficiency.
CREATE VIEW market_transactions_epc AS
SELECT mt.*,
       te.lmk_key,
       te.total_floor_area,
       te.habitable_rooms,
       te.current_energy_rating,
       te.price_per_sqm,
       te.match_method,
       te.match_confidence,
       te.epc_property_type,
       te.built_form,
       te.current_energy_efficiency
FROM market_transactions mt
LEFT JOIN transaction_epc te ON te.transaction_id = mt.transaction_id;

-- Re-grant: DROP removed the grants; agent_ro must keep read access to the recreated objects.
GRANT SELECT ON transaction_epc, epc_match_coverage, market_transactions_epc TO agent_ro;

COMMIT;
