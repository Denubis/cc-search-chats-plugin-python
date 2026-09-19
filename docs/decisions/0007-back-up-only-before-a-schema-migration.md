# 0007. Back up the database only before a schema migration

Status: Accepted
Date: 2026-09-20

## Context

The preserving-upgrade runbook required a `pg_dump` before every CLI upgrade.
During the 2.3.4 upgrade that step failed: this laptop runs PostgreSQL 17 on
port 5432 and PostgreSQL 18 on port 5433, Debian's `pg_wrapper` cannot read a
`service=` connection, so it selected the version 17 client, and an older
`pg_dump` refuses a newer server. The upgrade itself carried no migration.

PostgreSQL is a rebuildable projection of read-only native logs. A backup
therefore protects rebuild time, not content. An ordinary upgrade cannot force
a rebuild: the new CLI uses the same tables, a failed index leaves the previous
coherent selection current, and rollback is reinstalling the previous commit.
A schema migration can: it alters tables in place and may strand the previous
CLI.

The governing records are:

- Brian, 2026-09-20: *"um, why are we even running pg_dump? The fuck is that
  for?"* (native record `attachment`/`queued_command`, session
  `9d65e5ae-8c1f-4135-883c-c1530e296182`; sent mid-turn, so it has no indexed
  locator).
- Supervisor, 2026-09-20: proposed requiring the dump only when a migration is
  pending, with a version-safe `pg_dump` command. Brian answered: *"yes, skip
  empty ceremony"*
  (`ccchat:v1:claude:9d65e5ae-8c1f-4135-883c-c1530e296182:uuid:64acb6a4-25d3-47ee-ac3c-8d370ff9cca6`).

## Decision

A preserving upgrade backs up the database only when the newly installed CLI
reports a pending migration: `index --status --json` exits 6 with
`maintenance_required` and `pending_versions`. The backup then precedes
`index --migrate`. An upgrade without a pending migration takes no backup.

The backup command selects `pg_dump` from the server's own major version and
stops when the client is older than the server or the dump fails.

## Consequences

`docs/runbooks/laptop-deployment.md` owns the procedure. Routine protection
against disk loss remains an operator responsibility and is not an upgrade
step.
