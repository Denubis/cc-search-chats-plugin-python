# 0001. Classify index failures before acting on them

Status: Accepted
Date: 2026-07-31

## Context

`open_db` in `src/cc_search_chats/storage/index.py` treated a failed integrity
check as corruption, and it responded by deleting the index file and rebuilding
from chat history. The check that fed that decision could not distinguish a
damaged database from an environment that denied writes.

`_check_integrity` ran `PRAGMA quick_check` inside `try/except
sqlite3.DatabaseError`. Because `sqlite3.OperationalError` is a subclass of
`sqlite3.DatabaseError`, the handler also absorbed "attempt to write a readonly
database", "unable to open database file", and "database is locked", none of
which says anything about whether the data is intact.

The failure was demonstrated on 2026-07-31. A Codex session running under a
sandbox that denied writes to `~/.cc-search-chats/` ran a search, saw the index
declared corrupted, and then crashed when the sandbox refused the delete. The
index itself was healthy throughout, passing both `PRAGMA integrity_check` and
the FTS5 `integrity-check` on a byte-identical copy while holding 1,735 sessions
and 579,925 messages.

Reproduction on the production code path, both borders:

| Parent directory | Behaviour |
|------------------|-----------|
| writable | search succeeds, no message, database intact |
| `chmod 500` | corruption message, then `PermissionError` at `index.py:117` |

Under write denial `PRAGMA quick_check` raises `sqlite3.OperationalError:
attempt to write a readonly database`, whose `sqlite_errorname` is
`SQLITE_READONLY_DIRECTORY`. That name says the environment stopped the check
from running, so it reports neither health nor damage. The check never reached a
page, and a damaged database in the same directory would raise the same error,
because the write is refused before any page is read.

The defect was therefore never that the tool deleted the index. It was that the
tool deleted the index without being able to tell why it had failed.

## Decision

Ruled by Brian on 2026-07-31: *"delete should be bounded by ACID. if the db is
corrupt, genuinely corrupt (it happens) we just delete it. It's a cache. that's
just a fail quick with indications about why it's corrupt v all the other errors
and then that's worth my attention"*, refining his earlier ruling that *"a
rebuild sounds good, a delete sounds bad... it just needs to be very clear about
state... No, don't fucking overcomplicate it. Just make the errors clear."*

1. **Classify before acting.** Genuine damage is `SQLITE_CORRUPT`, an extended
   `SQLITE_CORRUPT_*` name, `SQLITE_NOTADB`, or a `quick_check` that completed
   and returned something other than `ok`. Everything else, and in particular the
   `SQLITE_READONLY_*` and `SQLITE_BUSY_*` families, is an environment failure.
   The two get different handling because they are different problems.

2. **Genuine damage deletes the file and fails fast.** The index is a cache
   rebuildable from `~/.claude/projects/`, so nothing is lost by discarding it.
   The command that met the damage fails rather than rebuilding inline, because a
   full corpus rebuild is expensive and a caller expecting a search should not
   silently wait on one.

   The message names the command that rebuilds rather than promising that the
   next run will. Only a project-scoped `search`, `extract`, `list` and `index`
   reindex; `search --all`, `search --everything`, a search from a directory that
   is not a Claude project, and `context` all skip `jit_reindex`, so "the next
   invocation rebuilds" is false for them. An earlier draft of this record said
   exactly that, and naming `cc-search-chats index` is both true and actionable
   where the generalisation was neither.

3. **Damage is reported loudly enough to notice.** Genuine corruption is rare and
   says something about the machine, so the message names what SQLite reported
   rather than describing the deletion alone.

4. **Environment failures never delete anything.** A denied or locked index keeps
   its file, reports the SQLite error name and the original error, and names the
   remedy. For a Codex caller that is adding the index directory to
   `sandbox_workspace_write.writable_roots` in `~/.codex/config.toml`.

5. **A rebuild is a transaction, not a file operation.** `reindex_project` runs
   `BEGIN`, deletes the project's rows, repopulates them and commits, rolling
   back on failure, so a failed rebuild leaves prior contents intact. Atomicity
   comes from the database rather than from filesystem sequencing.

6. **State is reported only when it can be verified.** Where no trustworthy read
   of the stored timestamp is available, the diagnostic says the timestamp is
   unavailable and why, rather than reporting a figure it cannot verify. A
   write-denied WAL database opens under `mode=ro` when readable `-wal` and
   `-shm` sidecars are present, but sidecars are absent in the ordinary case
   because a clean close removes them, and the only connection that opens then is
   `immutable=1`, which omits committed WAL frames.

7. **The index stays in WAL.** Read-only opens would work under
   `journal_mode=DELETE`, and that was rejected. WAL lets several agents search
   while indexing writes, whereas DELETE takes an exclusive lock and would hand a
   concurrent search `SQLITE_BUSY` during an `index --all` over the full corpus.
   Sandboxed callers are granted write access to the index directory instead.

8. **Indexing owns its transaction.** `index_session` and `reindex_project`
   refuse a connection that already has a transaction open, rather than nesting
   through savepoints. No caller wraps indexing in an outer transaction, so the
   simpler rule holds, and it prevents the helpers from committing or rolling
   back work they do not own.

9. **An unreadable source file is skipped, not fatal.** The transaction protects
   the database from a partial write and does not extend to source availability,
   so a session file that cannot be opened is reported and skipped in
   `jit_reindex`, `index_all_projects` and `reindex_project` alike.

   An earlier implementation removed the shared `OSError` guard so that a rebuild
   would be atomic, which reached the two incremental sweeps as well. Measured
   against a fixture holding one readable and one unreadable session file, the
   prior code warned, skipped and completed the sweep, while the change aborted
   it with an unhandled `PermissionError`. Because `jit_reindex` runs on every
   search, list and extract, that turned a rare destructive failure into a common
   crashing one.

10. **A failed deletion is never reported as a deletion.** Where damage is
    proven but the unlink then fails, typically because the directory is
    unwritable, the command reports both causes, states that the damaged file
    remains, gives the permission remedy, and exits non-zero. It does not
    announce a deletion that did not happen.

    This intersection is the original incident. The prior code printed
    "Rebuilding from chat history..." before attempting the unlink, so it
    announced an outcome that never occurred and then died with an unhandled
    `PermissionError`. Announcing an action before it succeeds is the same defect
    as misreporting the reason for a failure, so the ordering is load-bearing:
    attempt first, then report what actually happened.

11. **Deletion is best-effort rather than guarded.** Between classifying damage
    and unlinking, another process could delete and rebuild the file, so the
    unlink can remove a healthy replacement. That is accepted. The index is a
    cache whose source of truth is `~/.claude/projects/`, so losing the race
    costs a rebuild on the next invocation rather than any data, and every
    available guard is itself a check followed by a use and therefore racy. A
    file already absent counts as success; a file whose removal is refused does
    not, and falls under item 10.

12. **Damage is handled wherever it is classified, and only on the database that
    raised it.** Deletion is not limited to damage found by the opening
    `quick_check`, because `quick_check` is deliberately cheaper than
    `integrity_check` and does not perform every check, so damage a specific
    query hits can be damage the opening check passed over. Limiting deletion to
    open time would leave such a database reported forever and never
    automatically cleared. One helper owns "damage was classified, therefore
    delete, then report", and every call site uses it.

    That helper is bound to the identity of the database that raised the error,
    and never to a path resolved from an ambient default. **An error's
    classification says what kind of problem it is. It never says which database
    has it.** `search --everything` builds a throwaway in-memory index, so a
    corruption raised there is discarded with that database and reported as
    having left the persistent index unmodified.

    The binding is the load-bearing half. An earlier draft of this item required
    handling damage "wherever it is classified" without it, which would have let
    a corruption in the in-memory index delete the healthy persistent one. That
    is this record's own defect one level up: the original code acted on the index
    because it could not tell *why* a call failed, and the draft would have acted
    on it because it could not tell *which* database failed.

## Consequences

An environmental failure no longer destroys an index, and a caller learns what
denied it and how to fix it. Genuine damage still clears the cache, which is the
cheap and correct response to a rebuildable artefact, and it now says what SQLite
actually reported so that recurring corruption is visible rather than absorbed.

The distinction that matters is three-way rather than two-way: damaged, healthy,
or could not tell. The third state is the one the original code lacked, and
having it is what makes deleting on the first state safe.
