BEGIN TRANSACTION;

-- Items dismissed in the web UI; hidden items still count as seen (dedupe)
ALTER TABLE items ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0;

UPDATE parameters
SET value = '1.0.5.6'
WHERE key = 'version';

COMMIT;
