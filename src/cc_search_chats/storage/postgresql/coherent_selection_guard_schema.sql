UPDATE cc_search_chats.corpus_state AS state
SET current_corpus_generation = NULL
WHERE state.current_corpus_generation IS NOT NULL
  AND NOT EXISTS (
      SELECT 1
      FROM cc_search_chats.corpus_generation AS generation
      JOIN cc_search_chats.semantic_build AS build
        ON (build.semantic_build, build.corpus_generation) =
           (generation.semantic_build, generation.corpus_generation)
      WHERE generation.corpus_generation = state.current_corpus_generation
        AND generation.status = 'complete'
        AND generation.completed_at IS NOT NULL
        AND build.status = 'complete'
        AND build.completed_at IS NOT NULL
  );

CREATE FUNCTION cc_search_chats.require_coherent_selection_after_pair_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    selected_generation bigint;
BEGIN
    -- Publication already holds this row lock at its deferred checks, so it cannot self-block.
    SELECT state.current_corpus_generation
    INTO selected_generation
    FROM cc_search_chats.corpus_state AS state
    WHERE state.singleton
    FOR SHARE;

    IF selected_generation IS NULL THEN
        RETURN NEW;
    END IF;

    IF TG_TABLE_NAME = 'corpus_generation' THEN
        IF NEW.corpus_generation IS DISTINCT FROM selected_generation THEN
            RETURN NEW;
        END IF;
    ELSIF TG_TABLE_NAME = 'semantic_build' THEN
        IF NOT EXISTS (
            SELECT 1
            FROM cc_search_chats.corpus_generation AS generation
            WHERE generation.corpus_generation = selected_generation
              AND (generation.semantic_build, generation.corpus_generation) =
                  (NEW.semantic_build, NEW.corpus_generation)
        ) THEN
            RETURN NEW;
        END IF;
    ELSE
        RAISE EXCEPTION 'unsupported coherence trigger relation %', TG_TABLE_NAME;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM cc_search_chats.corpus_generation AS generation
        JOIN cc_search_chats.semantic_build AS build
          ON (build.semantic_build, build.corpus_generation) =
             (generation.semantic_build, generation.corpus_generation)
        WHERE generation.corpus_generation = selected_generation
          AND generation.status = 'complete'
          AND generation.completed_at IS NOT NULL
          AND build.status = 'complete'
          AND build.completed_at IS NOT NULL
    ) THEN
        RAISE EXCEPTION
            'incoherent selection after update of %: selected corpus generation % has no completed semantic build',
            TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME,
            selected_generation
            USING ERRCODE = '23514';
    END IF;

    RETURN NEW;
END;
$$;

CREATE CONSTRAINT TRIGGER corpus_generation_preserves_coherent_selection
AFTER UPDATE OF status, completed_at, semantic_build
ON cc_search_chats.corpus_generation
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION cc_search_chats.require_coherent_selection_after_pair_update();

CREATE CONSTRAINT TRIGGER semantic_build_preserves_coherent_selection
AFTER UPDATE OF status, completed_at
ON cc_search_chats.semantic_build
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION cc_search_chats.require_coherent_selection_after_pair_update();
