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

In another terminal, from a Python environment with `tiled[server]` installed:

```bash
URI=postgresql://postgres:<password>@localhost:5432/tiled

# Preview: prints what would move, changes nothing
python migrate_node_structure.py $URI --dry-run

# Upgrade the tiled schema (to --revision, default head), then move the runs
python migrate_node_structure.py $URI --revision head
```

The dry run only works if the database is already at tiled schema revision
`e05e918092c3` or later; otherwise it says so, and the real run upgrades it
first. Running the migration twice is safe: the second run moves nothing.

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
