# 2026-11-21 — Fix the semantic-search read-lock stall on a work branch

Status: Accepted
Date: 2026-11-21

**Question answered:** The cause of yesterday's chat-search stall is now reproduced in the disposable test database. A model-ranked search opens its database transaction first, takes the shared one-at-a-time read lock, and only then waits for the embedding model to load. When the model load stalled (the graphics card was full because an index run was using it), that search kept the lock for about 25 minutes, and a second one then kept it for another 17. Every other search, list and extract queued behind them. The proposed fix has two parts: fetch the query embedding before the transaction opens, so a search never holds the lock while waiting on the model; and apply the documented 30-second limit to the wait for the lock itself, which the code currently leaves unlimited. The one-at-a-time read rule stays, and model-ranked search keeps having no overall deadline. If you say yes, a worker implements both parts test-first on a work branch with local commits only, a fresh reviewer checks it, and I bring it to you to try before anything is installed. If you say no, nothing changes and the stall can recur whenever a model load is slow. Should I go ahead and have this two-part fix to the chat-search stall implemented on a work branch?

**Human answer (verbatim):** "yes"

**Decision:** Implement two changes test-first on the work branch
`fix/semantic-search-read-lock`, with local commits only: obtain the query
embedding before the search transaction opens, and set the 30-second lock
timeout before a no-deadline read waits for the read queue. The read queue and
the no-deadline semantic contract (ADR 0004) are unchanged. Pushing, merging
and installing are not authorized by this answer.

**Original-message resolver:** Claude session
`972b077b-bea8-4868-a1e2-d3a90db2811a`, native source
`/home/brian/.claude/projects/-home-brian-people-Brian-cc-search-chats-plugin-python/972b077b-bea8-4868-a1e2-d3a90db2811a.jsonl`,
line 518, record uuid `35af2100-b8bd-4d50-88b4-df722065a535`, timestamp
`2026-11-21T04:29:08.289Z`.

## Earlier investigation authorization

**Question answered:** Should I have a worker reproduce yesterday's chat-search stall and bring you a proposed fix?

**Human answer (verbatim):** "yes"

**Original-message resolver:** the same native source, line 517, record uuid
`994f8487-16c6-49b5-9da7-884c066b6399`, timestamp `2026-11-21T04:29:08.288Z`.

## Later correction: keep only the embedding-before-lock change

**Question answered:** I need to correct something I told you about the chat-search fix you approved. I described the second part, a 30-second limit on how long a search waits its turn in the one-at-a-time read queue, as closing a gap the code had left by accident. That was wrong. The August change that introduced the queue says waiting commands deliberately sleep until their turn "instead of retry loops", and an existing test checked exactly that; the worker had to alter that test to make the new limit pass. Both parts are now built and committed on the work branch, not yet reviewed or installed. The first part, getting the embedding before taking the lock, removes the cause of the stall by itself. With the second part also in, a command that waits more than 30 seconds for its turn fails with a generic internal-failure error and exit code 1, and agents would then retry it. Without it, commands wait their turn as originally designed, however long that takes. I recommend removing the second part and restoring the original test. If you say yes, the worker removes it before review; if you say no, both parts go to review as built. Should I remove the 30-second limit on waiting for the read queue and keep only the embedding-before-lock change?

**Human answer (verbatim):** "yes? um... what? This is the agent choosing to wait in line?"

**Decision:** Remove the lock-timeout-before-acquire change and restore
`test_exact_resolution_waits_in_the_database_read_queue` to its original form.
Only the embedding-before-transaction change remains. This supersedes the
second part of the decision above. The blocking read-queue wait introduced in
commit `a23129a` is unchanged.

**Original-message resolver:** the same native source, line 678, record uuid
`e61d2a3e-da60-42d4-8a71-7ab4629c21e2`, timestamp `2026-11-21T05:57:23.909Z`.

## Merge, push and install authorization

**Question answered:** The fix for the chat-search stall is finished on a local work branch as version 2.5.4 and has not been pushed, merged or installed. A fresh reviewer found no proven problems. I also tried it against your real search database: with the installed version 2.5.3, a plain search failed with a deadline error while a model-ranked search was loading its model; running the fixed code the same way, two plain searches each answered in about one second while the model was still loading, and the model-ranked search then returned normal results. The tool's installation procedure installs from GitHub, so putting the fix into everyday use means three steps: merging the work branch into the main branch, pushing that to GitHub, and reinstalling the chat-search command on this computer from the pushed commit. If you say yes, I do those three steps following the project's installation runbook and then run its smoke test. If you say no, the fix stays on the local branch and the installed tool keeps the stall. May I merge the chat-search fix into main, push it to GitHub, and install version 2.5.4 on this computer?

**Human answer (verbatim):** "yes"

**Decision:** Merge `fix/semantic-search-read-lock` into `main`, push `main`
to GitHub, and install 2.5.4 on this laptop by the preserving-upgrade route in
`docs/runbooks/laptop-deployment.md`, then run its installed acceptance smoke
test. Deleting the work branch is not authorized by this answer.

**Original-message resolver:** the same native source, line 937, record uuid
`b86a7e66-1d5b-4ee9-be25-d21b03addcd1`, timestamp `2026-11-21T09:33:53.685Z`.
