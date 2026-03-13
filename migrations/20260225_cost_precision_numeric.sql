-- Convert cost columns from floating-point to exact numeric precision.
-- This migration preserves existing values rounded to 8 decimal places.

ALTER TABLE runs
    ALTER COLUMN cost_usd TYPE NUMERIC(12, 8)
    USING ROUND(cost_usd::numeric, 8);

ALTER TABLE tasks
    ALTER COLUMN cost_usd TYPE NUMERIC(12, 8)
    USING ROUND(cost_usd::numeric, 8);
