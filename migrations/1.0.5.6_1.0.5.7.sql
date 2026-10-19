BEGIN TRANSACTION;

-- Condition (new, very good, ...) comes from the search payload; category is read from the item page
ALTER TABLE items ADD COLUMN condition TEXT;
ALTER TABLE items ADD COLUMN category TEXT;

UPDATE parameters
SET value = '1.0.5.7'
WHERE key = 'version';

COMMIT;
