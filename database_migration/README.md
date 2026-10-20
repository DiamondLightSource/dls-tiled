# Database migration

Commands to add a read-only role to the tiled Postgres database running in
Kubernetes, back up every database as that role, and move runs into the
`beamline/proposal/session` layout.

Set these once in your shell:

```bash
NS=<namespace>
POD=<postgres-pod>      # find it with: kubectl get pods -n $NS
```

## Passwords

Postgres tools have no password option on the command line. `-W` only forces
a prompt, and a prompt can't be answered through `kubectl exec` unless it has
`-it`. `-t` must never be used when writing a dump to a file: it turns
newlines into `\r\n` and corrupts the dump.

So the commands below pass the password in the `PGPASSWORD` environment
variable. Read it once without it showing on screen:

```bash
read -rs -p "Password: " PGPASSWORD; echo
```

It ends up on the `kubectl exec` command line, so it is briefly visible in the
process list on your machine and in the pod. Run `unset PGPASSWORD` when
you're done.

## 1. Create the read-only role (as postgres)

This step needs the `postgres` superuser, once. Open `psql` in the pod; it
asks for the postgres password itself:

```bash
kubectl exec -it -n $NS $POD -- psql -U postgres
```

Inside `psql`, create the role and set its password (`\password` asks without
showing it):

```sql
CREATE ROLE tiled_readonly LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
\password tiled_readonly
ALTER ROLE tiled_readonly SET default_transaction_read_only = on;
```

Find which role owns tiled's tables, needed below:

```sql
\c tiled
SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = 'public';
```

Then, still connected to `tiled`, grant read access. Replace `<owner>` with
the owner from the query above:

```sql
GRANT CONNECT ON DATABASE tiled TO tiled_readonly;
GRANT USAGE ON SCHEMA public TO tiled_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO tiled_readonly;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO tiled_readonly;
-- tables created later (e.g. by a tiled schema upgrade) are readable too
ALTER DEFAULT PRIVILEGES FOR ROLE <owner> IN SCHEMA public GRANT SELECT ON TABLES TO tiled_readonly;
ALTER DEFAULT PRIVILEGES FOR ROLE <owner> IN SCHEMA public GRANT SELECT ON SEQUENCES TO tiled_readonly;
\q
```

Repeat the `\c <database>` and grant block for every other database you want
backed up.

Why not `GRANT pg_read_all_data`: it also lets the role read `pg_authid`, the
stored password hash of every role including `postgres`. The grants above
don't.

### Check it can read but not write

Read the `tiled_readonly` password into `PGPASSWORD` (see
[Passwords](#passwords)), then:

```bash
kubectl exec -n $NS $POD -- env PGPASSWORD=$PGPASSWORD psql -U tiled_readonly -d tiled -c "SELECT count(*) FROM nodes"
kubectl exec -n $NS $POD -- env PGPASSWORD=$PGPASSWORD psql -U tiled_readonly -d tiled -c "DELETE FROM nodes WHERE false"
kubectl exec -n $NS $POD -- env PGPASSWORD=$PGPASSWORD psql -U tiled_readonly -d postgres -c "SELECT count(*) FROM pg_authid"
```

The first prints a count. The second must fail with
`cannot execute DELETE in a read-only transaction`. The third must fail with
`permission denied for table pg_authid`.

## 2. Back up every database (as tiled_readonly)

With the `tiled_readonly` password in `PGPASSWORD`:

```bash
mkdir -p backup

# Roles and tablespaces (pg_dump doesn't include them). --no-role-passwords
# because tiled_readonly can't read password hashes; restored roles get no
# password and need one set again.
kubectl exec -n $NS $POD -- env PGPASSWORD=$PGPASSWORD \
  pg_dumpall -U tiled_readonly -l postgres --globals-only --no-role-passwords > backup/globals.sql

# List the databases
kubectl exec -n $NS $POD -- env PGPASSWORD=$PGPASSWORD psql -U tiled_readonly -d postgres -AtXc \
  "SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate"

# Dump each database listed above, one command per database
kubectl exec -n $NS $POD -- env PGPASSWORD=$PGPASSWORD \
  pg_dump -U tiled_readonly --format=custom -d tiled > backup/tiled.dump
```

A database the role wasn't granted in step 1 fails with
`permission denied`; grant it or skip it.

Check the files aren't empty before going further:

```bash
ls -lh backup
```

## 3. Move runs into the new layout

`migrate_node_structure.py` writes to the database, so it runs as the tiled
owner or `postgres`, not `tiled_readonly`. It connects from your machine, so
forward the Postgres port first. Leave this running in its own terminal:

```bash
kubectl port-forward -n $NS pod/$POD 5432:5432
```

In another terminal (uv installs the script's dependencies, pinned to the
tiled version in the runtime image):

```bash
URI=postgresql://postgres:<password>@localhost:5432/tiled

# Preview: prints what would move and retag, changes nothing
uv run --script migrate_node_structure.py $URI --dry-run

# Upgrade the tiled schema (to --revision, default head), then move the runs
uv run --script migrate_node_structure.py $URI --revision head --authz-data data.json
```

`--authz-data` is the authz bundle's `data.json` (in `bundle.tar.gz` from
authz.diamond.ac.uk, or `GET /v1/data` from an OPA that has it loaded). The
script uses it for nodes still tagged with a bare numeric session id that it
can't move, such as runs without `start.instrument_session`: without the data
they stay tagged with the id, which the current policy never grants, so only
admins see them. With it they are retagged in place from that session's
beamline, proposal and visit. They stay at the root, since the data has the
proposal number but not the code (`cm`) the tree's keys use. The script prints
how many ids it couldn't find.

Besides moving runs, it gives nodes already in the tree that carry only their
own tag (from blueapi under an earlier policy, or an earlier run of this
script) the full tag list the current policy stamps, so beamline and proposal
grants reach them. Deploy the matching authz policy with it: under an older
policy, a session member's beamline tag would match every node on the beamline.

The dry run only works if the database is already at tiled schema revision
`e05e918092c3` or later; otherwise it says so, and the real run upgrades it
first. Running the migration twice is safe: the second run moves and retags
nothing.

## Restore from the backup (as postgres)

With the `postgres` password in `PGPASSWORD`:

```bash
# Roles, only when restoring onto a fresh server
kubectl exec -i -n $NS $POD -- env PGPASSWORD=$PGPASSWORD psql -U postgres -d postgres < backup/globals.sql

# Stop tiled first: on startup it creates the schema in an empty database,
# and the restore then fails with "already exists" errors
kubectl scale -n $NS deploy/<tiled-deployment> --replicas=0

# Drop the broken database first if it still exists:
#   kubectl exec -n $NS $POD -- env PGPASSWORD=$PGPASSWORD dropdb -U postgres --force tiled
kubectl exec -n $NS $POD -- env PGPASSWORD=$PGPASSWORD createdb -U postgres tiled
kubectl exec -i -n $NS $POD -- env PGPASSWORD=$PGPASSWORD pg_restore -U postgres -d tiled --exit-on-error < backup/tiled.dump

kubectl scale -n $NS deploy/<tiled-deployment> --replicas=1
```

If the restore fails partway, drop the database and start again; don't try
to patch a partial restore.

## Troubleshooting

### `KeyError: 'postgresql://<host>:5432/data'` when reading data

After you restore a backup into a different deployment (for example, a prod
dump restored into tiled-testing), reads of tabular data fail in
`tiled/storage.py`, in `get_storage`.

Each row in the catalog's `assets` table has a `data_uri` that names the
storage it was written to. Tiled keys its storage by URI without credentials,
built from the `writable_storage` and `readable_storage` entries in its config.
If the restored rows still name the old host, the lookup fails. The migration
script doesn't touch `data_uri`.

To fix it, point the assets at the new host. Run this against the catalog
database (`tiled`), not `data`. The `data` database must already exist in
the new deployment with the same tables, or the `KeyError` becomes a
missing-table error. Tiled reads `data_uri` on every request, so it doesn't
need a restart.

With the `postgres` password in `PGPASSWORD`, set the host that's in the
error (`OLD`) and the host in this deployment's `writable_storage` (`NEW`):

```bash
OLD=tiled-postgres
NEW=tiled-testing-postgres

# Preview: the rows that will change
kubectl exec -i -n $NS $POD -- env PGPASSWORD=$PGPASSWORD psql -U postgres -d tiled <<SQL
SELECT data_uri, count(*) FROM assets
WHERE data_uri LIKE 'postgresql://$OLD:5432/%'
GROUP BY data_uri;
SQL

# Rewrite the host
kubectl exec -i -n $NS $POD -- env PGPASSWORD=$PGPASSWORD psql -U postgres -d tiled -v ON_ERROR_STOP=1 <<SQL
BEGIN;
UPDATE assets
SET data_uri = replace(data_uri, '//$OLD:5432/', '//$NEW:5432/')
WHERE data_uri LIKE 'postgresql://$OLD:5432/%';
COMMIT;
SQL
```

Don't add the old URI to `readable_storage` instead. The deployment would
then read from the other deployment's database, if it can reach it at all.
