# ACQP-568: Tiled catalog node restructuring

Runbook for `tiled_data_restructure_script.py`, written 2026-08-04 after
running this for real against a p46 replica seeded with a real production
snapshot. If you're reading this because it's a few years later and the
target structure needs to change again, start here.

## What this was for

Diamond's production Tiled catalog (`tiled.diamond.ac.uk`) stored every
experimental run as a flat node directly under the catalog root. This
script reorganizes them into:

```
root/<instrument>/<instrument_session>/<run>
```

e.g. `root/p51/cm44254-1/<run>`.

The `instrument` and `instrument_session` values come from each run's own
metadata (`metadata.start.instrument` / `metadata.start.instrument_session`
— this is standard bluesky run-start-document metadata, not something
Diamond-specific), not from any existing path structure — the catalog was
completely flat before this ran, with ~3,000 top-level nodes each keyed by
an opaque UUID.

**This was deliberately built as a one-off script, not reusable
infrastructure.** The team lead's call at the time: the target structure
was expected to be stable for 2-3 years, so it wasn't worth building this
as a separate ops repo/package.

## Prerequisites / access setup

Two separate Diamond Kubernetes clusters are involved, and switching
between them requires re-authenticating (they're not simultaneously
available in one terminal's kubeconfig):

- **`pollux`** — hosts `p46-beamline` namespace, the staging/rehearsal
  replica (`p46-tiled.diamond.ac.uk`). `module load pollux` (fedid +
  password login, `klogout` to force a fresh login if you hit stale-auth
  errors).

- **`argus`** — hosts the `tiled` namespace, i.e. **production**
  (`tiled.diamond.ac.uk`). `module load argus`, same login pattern.

Run `kubectl config current-context` if something that worked a moment ago
suddenly returns "not found" — it's almost always because the context
silently switched clusters.

Postgres access on both: connect as the **`postgres`** superuser (not the
bootstrapped `tiled`/`USERDB_USER` app account, which lacks table-level
grants), password is the `ADMIN_PASS` key in the `postgres-secret`
(`SealedSecret`) in that namespace, this can be found on the pollux.
Two databases matter: `tiled-db` (thecatalog — this is what this script operates on)
 and `data` (writable storage backend, unrelated to node structure).

To connect a local tool (psql, a GUI client, or this script) to a
cluster's postgres without exec-ing into the pod every time:

```
kubectl port-forward -n <namespace> svc/<postgres-service-name> 5432:5432
```

Leave that running in its own terminal; connect to `localhost:5432` from
wherever you're running the script/client.

## The two implementations

**`tiled_data_restructure_script.py`** (this directory) — the version
actually used. Python + SQLAlchemy async, does everything in one
transaction, set-based (not row-by-row) for performance.

```
python scripts/tiled_data_restructure_script.py <database_uri> [--dry-run]
```

```
cd /scratch/<ret id>/path/to/tiled   # or wherever you have it
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[all]"
```

**Always dry-run first**, on the replica, not production:

```
python scripts/tiled_data_restructure_script.py \
  "postgresql+asyncpg://postgres:<password>@localhost:5432/tiled-db" --dry-run
```

This prints the instrument/session groupings and counts with zero writes.
Sanity-check the numbers against a manual query before trusting it:

```sql
SELECT n2.metadata->'start'->>'instrument' AS instrument,
       n2.metadata->'start'->>'instrument_session' AS session,
       count(*)
FROM nodes n1 JOIN nodes n2 ON n2.parent = n1.id
WHERE n1.parent IS NULL
GROUP BY 1, 2 ORDER BY 1, 2;
```

Also worth checking before running for real: how many top-level nodes are
missing either metadata field (the script refuses to proceed if any are
found, but knowing the number up front avoids surprises):

```sql
SELECT count(*) FILTER (WHERE n2.metadata->'start'->>'instrument' IS NULL) AS missing_instrument,
       count(*) FILTER (WHERE n2.metadata->'start'->>'instrument_session' IS NULL) AS missing_session
FROM nodes n1 JOIN nodes n2 ON n2.parent = n1.id
WHERE n1.parent IS NULL;
```

## Rehearsal process used for the p46 run (repeat this for any future run)
these can be found also in the old Jira ticket, in the comments https://jira.diamond.ac.uk/browse/ACQP-568

1. Get a real snapshot to test against — don't invent synthetic data.
   Dump production's `tiled-db` (and `data`, if relevant) via `pg_dump -F c`
   from inside the postgres pod, `kubectl cp` it out, sanity-check with
   `pg_restore -l` before trusting it.
2. Seed the target replica for real: `dropdb --force` + `createdb` the
   replica's existing `tiled-db` (needs `--force` since the live app pod
   holds open connections), then `pg_restore` the production dump into it.
   Restart the replica's app deployment afterward
   (`kubectl rollout restart deployment ...`) so it picks up a clean
   connection rather than reusing stale state.
3. Confirm the app itself is healthy against the reseeded data — check
   `kubectl logs` on the new pod for connection/startup errors, not just
   that the database rows exist.
4. `--dry-run` the migration script, cross-check its printed counts
   against a manual SQL query (above).
5. Run it for real (drop `--dry-run`).
6. Verify: spot-check that a specific instrument/session actually has the
   expected run count as *children* of the new container (not just trust
   the script's own summary), e.g.:
   ```sql
   SELECT count(*) FROM nodes n JOIN nodes sess ON n.parent = sess.id
   WHERE sess.key = '<a session id from the dry run>';
   ```
   Compare against the dry-run's printed count for that session.

script also stored here, as we're unsure if we need a tiled/tiled repo currently and I dont want to lose this.
"""

Example:
    python scripts/acqp568_migrate_node_structure.py \
        postgresql+asyncpg://postgres:<password>@localhost:5432/tiled-db --dry-run
"""
