# Pending work

Working root: `/home/brian/people/Brian/cc-search-chats-plugin-python`.
Completed items move to `worklog.md`; do not tick them here.

## Outcome 5: consumer truth and release
- [ ] Update `README.md`, `skills/search-chat/SKILL.md`,
      `commands/search-chat.md`, `CLAUDE.md` CLI Contract,
      `docs/architecture/database.md`, `docs/runbooks/laptop-deployment.md`.
- [ ] Execute every added command example; record output.
- [ ] Bounded documentation inspection with a planted positive control;
      record findings and fixes.
- [ ] Amend `docs/design-plans/2026-08-10-cross-vendor-semantic-search.md`
      lines 55–56 and 96–98.
- [ ] Version `2.4.0` in `pyproject.toml`, both plugin manifests,
      marketplace, `tests/test_plugin_packaging.py`, `uv.lock`; CHANGELOG
      `**Added:**` entry.
- [ ] Run the five authority resolves with the installed CLI; flip the design
      status only if all resolve, otherwise record and ask the human the one
      question in the plan.
- [ ] Full gate set incl. `pre-commit validate-config` and every `--help`.
- [ ] Final checkpoint commit (release commit).

## After execution (not authorised by this plan)
- [ ] Human UAT over the finished surface (plan.md, Finished-work UAT) after
      the separately authorised backup, `index --migrate` and production index.
- [ ] Normalise checkpoints; rerun gates; direct-delivery request to `main`.
