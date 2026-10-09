-- Admit the antigravity provider in every live provider CHECK constraint.
-- Each target table must carry exactly one single-column CHECK on provider,
-- under whatever name PostgreSQL or an operator gave it; it is replaced by
-- <table>_provider_check. The legacy message relation and the quarantine
-- schema are not touched.
DO $$
DECLARE
    target text;
    found text;
    matched integer;
BEGIN
    FOREACH target IN ARRAY ARRAY[
        'message_current', 'source_root_current', 'source_failure_current'
    ] LOOP
        matched := 0;
        FOR found IN
            SELECT constraint_entry.conname
            FROM pg_constraint AS constraint_entry
            JOIN pg_attribute AS attribute
              ON attribute.attrelid = constraint_entry.conrelid
             AND attribute.attnum = ANY (constraint_entry.conkey)
            WHERE constraint_entry.conrelid =
                  format('cc_search_chats.%I', target)::regclass
              AND constraint_entry.contype = 'c'
              AND array_length(constraint_entry.conkey, 1) = 1
              AND attribute.attname = 'provider'
        LOOP
            matched := matched + 1;
            EXECUTE format(
                'ALTER TABLE cc_search_chats.%I DROP CONSTRAINT %I',
                target, found
            );
        END LOOP;
        IF matched = 0 THEN
            RAISE EXCEPTION
                'cc_search_chats.% carries no provider CHECK constraint to replace',
                target;
        END IF;
        EXECUTE format(
            'ALTER TABLE cc_search_chats.%I ADD CONSTRAINT %I '
            'CHECK (provider IN (''claude'', ''codex'', ''antigravity''))',
            target, target || '_provider_check'
        );
    END LOOP;
END
$$;
