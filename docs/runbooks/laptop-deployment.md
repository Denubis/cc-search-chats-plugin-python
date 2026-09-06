# Laptop deployment

Last verified: 2026-09-06

This is the canonical installation and upgrade procedure for one operator-owned
Linux laptop using the repository's current infrastructure: a global `uv` tool,
PostgreSQL 18 with pgvector, the pinned local semantic model, Claude Code and
Codex plugins, a Codex host-routing rule, and a systemd user timer.

Choose exactly one route before changing state:

- **Clean install or rebuild** treats the PostgreSQL search projection as
  disposable and rebuilds it from read-only native Claude Code and Codex logs.
  Use it for a new laptop or a projection that is not worth preserving.
- **Preserving upgrade** retains the database, selected coherent corpus, and a
  recovery copy while moving the installed CLI and consumers to an exact newer
  commit.

The execution order is intentionally different:

- clean/rebuild: common release boundary → CLI proof → Codex rule → plugins →
  packaged units → clean database/rebuild → installed acceptance;
- preserving upgrade: common release boundary → record and back up the existing
  deployment → quiesce timer activation → CLI proof → database migration/build
  → rule/plugins/units → installed acceptance.

Do not execute every section top-to-bottom during an upgrade: its pre-change
evidence and backup must precede installation of the new CLI.

Neither route edits native chat logs. Database reset, migration, installation,
plugin changes, timer changes, production UAT, and legacy pruning are distinct
actions. Do not infer authority for one from another. Legacy pruning remains in
the [PostgreSQL maintenance runbook](postgresql-index-maintenance.md).

## Common release boundary

Run these commands from this repository. Installation is allowed only from an
accepted commit on clean `main` when local `main` and freshly fetched
`origin/main` name the same SHA. An empty `git status --short` output is part of
the gate; ignored local files are excluded by the package build allowlist rather
than by that status check.

```fish
git fetch origin main
git switch main
git status --short
set worktree_status (git status --porcelain --untracked-files=all)
test (count $worktree_status) -eq 0; or exit 1
set release_sha (git rev-parse HEAD)
test "$release_sha" = (git rev-parse main); or exit 1
test "$release_sha" = (git rev-parse origin/main); or exit 1
```

When the target changes CLI, skill, rule, or output behavior, first assign the
new release version in `pyproject.toml`, `uv.lock`, both plugin manifests, and
the Claude marketplace entry, and add its changelog section. The packaging test
requires those versions and the README release label to agree. Reusing the old
plugin version can make an updater truthfully report that nothing changed.

Then run the project gates on that exact tree before installation:

```fish
uv run --frozen pytest -q -m 'not postgresql'
uv run --frozen pytest -q -m postgresql
uv run --frozen ruff check src tests scripts
uv run --frozen ruff format --check src tests scripts
uv run --frozen ty check src tests scripts
uv run --frozen complexipy --failed --plain
uv run --frozen vulture
git diff --check
```

Do not redirect or replace the configured uv, Hugging Face, Torch, or model
caches. The semantic snapshot must already exist at the configured location;
runtime commands are offline and never download it.

## Install and prove the CLI

Install the semantic-capable CLI non-editably from the exact accepted SHA. The
same command installs a new tool or replaces the requirement recorded for an
existing one. `--force` also makes a same-version reinstall observable.

```fish
uv tool install --force "cc-search-chats[semantic] @ git+https://github.com/Denubis/cc-search-chats-plugin-python@$release_sha"
set tool_root (uv tool dir)/cc-search-chats
set tool_python "$tool_root/bin/python"
test (command -v cc-search-chats) = "$HOME/.local/bin/cc-search-chats"; or exit 1
```

Verify the VCS commit recorded by the installed distribution and prove that the
import resolves inside the global uv-tool environment. The package version alone
is not sufficient because different commits may share it.

```fish
env CC_SEARCH_EXPECTED_SHA="$release_sha" CC_SEARCH_EXPECTED_TOOL_ROOT="$tool_root" "$tool_python" -c 'import importlib.metadata as m,json,os; from pathlib import Path; import cc_search_chats; d=m.distribution("cc-search-chats"); direct=json.loads(d.read_text("direct_url.json")); actual=direct["vcs_info"]["commit_id"]; root=Path(os.environ["CC_SEARCH_EXPECTED_TOOL_ROOT"]).resolve(); module=Path(cc_search_chats.__file__).resolve(); assert actual==os.environ["CC_SEARCH_EXPECTED_SHA"], (actual,os.environ["CC_SEARCH_EXPECTED_SHA"]); assert module.is_relative_to(root), (module,root); print("commit="+actual); print("module="+str(module))'
cc-search-chats --version
cc-search-chats --help
```

Verify the semantic dependencies, host CUDA visibility, and exact local model
revision without loading the model:

```fish
nvidia-smi
"$tool_python" -c 'import torch,transformers; assert torch.cuda.is_available(); print("torch="+torch.__version__); print("transformers="+transformers.__version__)'
"$tool_python" -c 'from cc_search_chats.semantic.model import MODEL_REVISION,local_model_revision; actual=local_model_revision(); assert actual==MODEL_REVISION,(actual,MODEL_REVISION); print(actual)'
```

These are host checks. A sandbox failure establishes only what that sandboxed
process could access.

## Install the Codex host route

The plugin does not install a Codex execution-policy rule. Install the tracked
rule separately and verify both its bytes and its decision when combined with
every other configured rule. This catches a competing `prompt` or `forbidden`
rule instead of validating the new file in isolation.

```fish
python3 scripts/install_codex_rule.py
cmp --silent rules/cc-search-chats.rules ~/.codex/rules/cc-search-chats.rules; or exit 1
set rule_args
for rule_path in ~/.codex/rules/*.rules
    set -a rule_args --rules "$rule_path"
end
codex execpolicy check $rule_args cc-search-chats search deployment-control --literal --json
codex execpolicy check $rule_args cc-search-chats index --status --json
codex execpolicy check $rule_args cc-search-chats index --json
```

All three results must report `decision: allow`. The rule intentionally covers
read-only searches and index mutations because the operator has authorized this
CLI to use the host PostgreSQL service, systemd user manager, model cache, and
GPU. Restart Codex after changing the rule; an open session may retain its
startup policy.

## Install the plugins

The plugins provide agent instructions and invoke the independently installed
CLI. For a new Claude Code installation:

```fish
claude plugin marketplace add Denubis/cc-search-chats-plugin-python
claude plugin install cc-search-chats@cc-search-chats-marketplace
```

For the current laptop's repository-local Codex marketplace:

```fish
codex plugin marketplace add "$PWD"
codex plugin add cc-search-chats@cc-search-chats-marketplace --json
```

On a preserving upgrade, refresh the existing consumers after their manifests
carry the same version as the CLI:

```fish
claude plugin marketplace update cc-search-chats-marketplace
claude plugin update cc-search-chats@cc-search-chats-marketplace
codex plugin add cc-search-chats@cc-search-chats-marketplace --json
```

Start new Claude Code and Codex sessions after plugin or rule changes. Do not use
an old session as evidence that the new plugin or policy is active.

## Install scheduled maintenance

The packaged service assumes uv exposes the CLI at
`$HOME/.local/bin/cc-search-chats`; the earlier executable-path assertion blocks
deployment if this laptop uses another tool binary directory. Copy the units
from the exact accepted repository tree and load them without enabling the timer
yet:

```fish
install -Dm644 src/cc_search_chats/systemd/cc-search-chats-index.service "$HOME/.config/systemd/user/cc-search-chats-index.service"
install -Dm644 src/cc_search_chats/systemd/cc-search-chats-index.timer "$HOME/.config/systemd/user/cc-search-chats-index.timer"
systemctl --user daemon-reload
```

Optional source-root configuration belongs in
`$HOME/.config/cc-search-chats/index.env`. Keep libpq credentials in `.pgpass`
and retain the operator's existing cache environment; the service deliberately
supplies neither credentials nor cache overrides.

## Clean install or rebuild

This route may destroy an existing PostgreSQL search projection. If that
projection might be needed, stop and use **Preserving upgrade**. Native session
logs are the rebuild authority and remain read-only.

### Provision a new database

As a PostgreSQL administrator, create the role and database only when they do
not already exist:

```sql
CREATE ROLE cc_search_chats_owner LOGIN;
GRANT SET ON PARAMETER temp_file_limit TO cc_search_chats_owner;
\password cc_search_chats_owner
CREATE DATABASE cc_search_chats OWNER cc_search_chats_owner;
\connect cc_search_chats
CREATE EXTENSION vector;
```

For an explicitly disposable existing projection, first disable the timer and
confirm that no index service or other host is using the database. Then, as a
PostgreSQL administrator, replace only this application's database:

```fish
systemctl --user disable --now cc-search-chats-index.timer
systemctl --user is-active cc-search-chats-index.service
```

`is-active` must report `inactive`. Do not stop a live index process merely to
force the reset; determine who owns it and let it finish or fail visibly.

```sql
\connect postgres
DROP DATABASE cc_search_chats WITH (FORCE);
CREATE DATABASE cc_search_chats OWNER cc_search_chats_owner;
\connect cc_search_chats
CREATE EXTENSION vector;
```

Configure the libpq service:

```ini
[cc_search_chats]
host=127.0.0.1
port=5432
dbname=cc_search_chats
user=cc_search_chats_owner
```

Put the password in `~/.pgpass`, never in the systemd unit, and restrict its
permissions:

```text
127.0.0.1:5432:cc_search_chats:cc_search_chats_owner:YOUR_PASSWORD
```

```fish
chmod 600 ~/.pgpass
pg_isready -d 'service=cc_search_chats'
```

Large deployments must provision default and temporary tablespaces on the
operator-managed external mount before migration. Follow the
[storage preflight](postgresql-index-maintenance.md#2-back-up-and-preflight-storage)
without inventing a database, tablespace, cache, model, or root-filesystem
fallback.

### Migrate and rebuild

Retain deployment evidence outside `/tmp`:

```fish
set deployment_evidence "$HOME/.local/state/cc-search-chats/deployments/$release_sha"
install -d -m 700 "$deployment_evidence"
cc-search-chats index --migrate --json >"$deployment_evidence/index-migrate.stdout.json" 2>"$deployment_evidence/index-migrate.stderr.ndjson"
cc-search-chats index --status --json >"$deployment_evidence/pre-index-status.json" 2>"$deployment_evidence/pre-index-status.ndjson"
```

If the configured native session roots do not exist yet, stop here with the
explicit state **installed, awaiting native sources**. Do not create invented
source roots, enable the timer, or claim that an empty search proves the
deployment. Once the native clients have created their session directories,
run the rebuild on the host:

```fish
cc-search-chats index --json >"$deployment_evidence/index.stdout.json" 2>"$deployment_evidence/index.stderr.ndjson"
cc-search-chats index --status --json >"$deployment_evidence/post-index-status.json" 2>"$deployment_evidence/post-index-status.ndjson"
```

A completed rebuild requires positive resolved-root and discovered-file counts,
a positive `refresh.corpus_generation`, a positive
`semantic.semantic_build`, matching semantic/corpus generations,
`semantic.fresh: true`, and `completed == total`. Source diagnostics remain
failures or explicit evidence limits; do not repair native logs by hand.

## Preserving upgrade

This route never drops the database. Before changing the CLI, record the current
installed commit, database identity, schema state, selected corpus/build, timer
state, and a recoverable database backup. Complete the common release boundary
for the target SHA first.

```fish
set previous_tool_root (uv tool dir)/cc-search-chats
"$previous_tool_root/bin/python" -c 'import importlib.metadata as m,json; d=m.distribution("cc-search-chats"); print(json.loads(d.read_text("direct_url.json"))["vcs_info"]["commit_id"])'
systemctl --user is-enabled cc-search-chats-index.timer
systemctl --user list-timers --all --no-pager cc-search-chats-index.timer
cc-search-chats index --status --json
set upgrade_evidence "$HOME/.local/state/cc-search-chats/upgrades/$release_sha"
install -d -m 700 "$upgrade_evidence"
pg_dump --dbname='service=cc_search_chats' --format=custom --file="$upgrade_evidence/cc-search-chats-before.dump"
```

Disable future timer activation, then require the service to be inactive before
installation or migration:

```fish
systemctl --user disable --now cc-search-chats-index.timer
systemctl --user is-active cc-search-chats-index.service
```

Install and prove the new CLI using **Install and prove the CLI**, then perform
the database-specific selected-pair and storage checks in the
[PostgreSQL maintenance runbook](postgresql-index-maintenance.md). Only with
separate migration authority, apply pending migrations through the installed
entrypoint and retain both output streams:

```fish
cc-search-chats index --migrate --json >"$upgrade_evidence/index-migrate.stdout.json" 2>"$upgrade_evidence/index-migrate.stderr.ndjson"
cc-search-chats index --json >"$upgrade_evidence/index.stdout.json" 2>"$upgrade_evidence/index.stderr.ndjson"
cc-search-chats index --status --json >"$upgrade_evidence/post-index-status.json" 2>"$upgrade_evidence/post-index-status.ndjson"
```

Indexing prepares and publishes one coherent replacement. A candidate failure
must leave the previously selected coherent corpus/build current; preserve the
failure output and prove the previous selection still answers a known positive
literal query. Do not reset the database as upgrade recovery.

After the new database state passes its status checks, apply **Install the Codex
host route**, the preserving-upgrade commands under **Install the plugins**, and
**Install scheduled maintenance** from the exact target tree. Leave the timer
disabled until installed acceptance passes.

Before migration, rollback may reinstall the recorded previous SHA. After a
migration, do not downgrade the CLI unless that exact older commit is proven
compatible with the applied migration ledger. Otherwise restore the database
backup and previous CLI together, or repair forward with a new migration.

## Installed acceptance and activation

Choose a phrase known to exist in the indexed native logs. Both checks must find
that expected record; an empty result is not a smoke test. Semantic search must
deliver `retrieval_mode: hybrid` without `semantic_search_degraded`.

```fish
set smoke_query 'REPLACE WITH A KNOWN PHRASE'
cc-search-chats search "$smoke_query" --literal --json
cc-search-chats search "$smoke_query" --semantic --json
```

When retrieval SQL changed, also run a read-only `EXPLAIN (ANALYZE, BUFFERS)` of
the exact statement against the production-sized laptop database and require
zero temporary blocks written at every plan node. Disposable tests do not model
that scale.

Enable the timer only after migration, one successful coherent build, and the
positive smoke controls. Confirm that its next activation is outside the UAT
window:

```fish
systemctl --user enable --now cc-search-chats-index.timer
systemctl --user list-timers --all --no-pager cc-search-chats-index.timer
```

Run the [cross-vendor installed UAT](../uat/cross-vendor-search-wip.md) when all
four standard/Ponytail roots are in scope. Record human acceptance against the
exact installed commit and durable evidence directory; a successful script does
not imply human acceptance.

Deployment is complete only when the installed CLI provenance, effective Codex
policy, database state, positive literal and semantic controls, plugin versions,
new-session behavior, and applicable human UAT all identify the same accepted
commit. Publication and legacy pruning remain separate operations.
