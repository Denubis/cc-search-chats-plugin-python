ALTER TABLE cc_search_chats.corpus_revision
    RENAME TO corpus_generation;

ALTER TABLE cc_search_chats.corpus_generation
    RENAME COLUMN revision_id TO corpus_generation;

ALTER TABLE cc_search_chats.corpus_state
    RENAME COLUMN current_revision_id TO current_corpus_generation;

ALTER TABLE cc_search_chats.source_file_current
    RENAME COLUMN updated_revision_id TO updated_corpus_generation;

ALTER TABLE cc_search_chats.refresh_run
    RENAME COLUMN corpus_revision_id TO corpus_generation;

ALTER TABLE cc_search_chats.cutover_validation
    RENAME COLUMN corpus_revision_id TO corpus_generation;

ALTER TABLE cc_search_chats.semantic_revision
    RENAME TO semantic_build;

ALTER TABLE cc_search_chats.semantic_build
    RENAME COLUMN semantic_revision_id TO semantic_build;

ALTER TABLE cc_search_chats.semantic_build
    RENAME COLUMN corpus_revision_id TO corpus_generation;

ALTER TABLE cc_search_chats.semantic_build
    ADD CONSTRAINT semantic_build_identity_owner_key
    UNIQUE (semantic_build, corpus_generation);

ALTER TABLE cc_search_chats.corpus_generation
    ADD COLUMN semantic_build bigint;

UPDATE cc_search_chats.corpus_generation AS generation
SET semantic_build = selected.current_semantic_revision_id
FROM cc_search_chats.corpus_state AS corpus,
     cc_search_chats.semantic_state AS selected,
     cc_search_chats.semantic_build AS build
WHERE corpus.singleton
  AND selected.singleton
  AND generation.corpus_generation = corpus.current_corpus_generation
  AND build.semantic_build = selected.current_semantic_revision_id
  AND build.corpus_generation = generation.corpus_generation
  AND generation.status = 'complete'
  AND generation.completed_at IS NOT NULL
  AND build.status = 'complete'
  AND build.completed_at IS NOT NULL;

ALTER TABLE cc_search_chats.corpus_generation
    ADD CONSTRAINT corpus_generation_selected_semantic_build_fkey
    FOREIGN KEY (semantic_build, corpus_generation)
    REFERENCES cc_search_chats.semantic_build (
        semantic_build, corpus_generation
    )
    DEFERRABLE INITIALLY DEFERRED;

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

DROP TABLE cc_search_chats.semantic_state;

CREATE FUNCTION cc_search_chats.require_coherent_corpus_selection()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.current_corpus_generation IS NULL THEN
        RETURN NEW;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM cc_search_chats.corpus_generation AS generation
        JOIN cc_search_chats.semantic_build AS build
          ON (build.semantic_build, build.corpus_generation) =
             (generation.semantic_build, generation.corpus_generation)
        WHERE generation.corpus_generation = NEW.current_corpus_generation
          AND generation.status = 'complete'
          AND generation.completed_at IS NOT NULL
          AND build.status = 'complete'
          AND build.completed_at IS NOT NULL
    ) THEN
        RAISE EXCEPTION
            'selected corpus generation % has no completed semantic build',
            NEW.current_corpus_generation
            USING ERRCODE = '23514';
    END IF;

    RETURN NEW;
END;
$$;

CREATE CONSTRAINT TRIGGER corpus_state_requires_coherent_selection
AFTER INSERT OR UPDATE ON cc_search_chats.corpus_state
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION cc_search_chats.require_coherent_corpus_selection();
