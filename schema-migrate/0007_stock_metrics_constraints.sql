-- Enforce finite sector-relative scores on stock_metrics and document the
-- cross-service sign convention (`> 0` always means better than sector).

UPDATE stock_metrics
SET
    pe_relative_sector = CASE
        WHEN pe_relative_sector IS NOT NULL
             AND (pe_relative_sector = 'NaN'::double precision
                  OR ABS(pe_relative_sector) = 'Infinity'::double precision)
        THEN NULL
        ELSE pe_relative_sector
    END,
    beta_relative_sector = CASE
        WHEN beta_relative_sector IS NOT NULL
             AND (beta_relative_sector = 'NaN'::double precision
                  OR ABS(beta_relative_sector) = 'Infinity'::double precision)
        THEN NULL
        ELSE beta_relative_sector
    END,
    return_relative_sector = CASE
        WHEN return_relative_sector IS NOT NULL
             AND (return_relative_sector = 'NaN'::double precision
                  OR ABS(return_relative_sector) = 'Infinity'::double precision)
        THEN NULL
        ELSE return_relative_sector
    END;

ALTER TABLE stock_metrics
    ADD CONSTRAINT stock_metrics_pe_relative_finite_ck CHECK (
        pe_relative_sector IS NULL
        OR (
            pe_relative_sector <> 'NaN'::double precision
            AND ABS(pe_relative_sector) < 'Infinity'::double precision
        )
    ),
    ADD CONSTRAINT stock_metrics_beta_relative_finite_ck CHECK (
        beta_relative_sector IS NULL
        OR (
            beta_relative_sector <> 'NaN'::double precision
            AND ABS(beta_relative_sector) < 'Infinity'::double precision
        )
    ),
    ADD CONSTRAINT stock_metrics_return_relative_finite_ck CHECK (
        return_relative_sector IS NULL
        OR (
            return_relative_sector <> 'NaN'::double precision
            AND ABS(return_relative_sector) < 'Infinity'::double precision
        )
    );

COMMENT ON COLUMN stock_metrics.pe_relative_sector IS
    'SectorRelativeScore for P/E (inverted: > 0 means undervalued vs sector benchmark).';
COMMENT ON COLUMN stock_metrics.beta_relative_sector IS
    'SectorRelativeScore for Beta (inverted: > 0 means lower volatility than sector benchmark).';
COMMENT ON COLUMN stock_metrics.return_relative_sector IS
    'SectorRelativeScore for 1Y return (> 0 means outperformed sector benchmark).';
