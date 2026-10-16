"""Back up a Postgres pod in Kubernetes, or add a read-only role to it.

Every command runs inside the pod with `kubectl exec`, so the pod's own
Postgres tools (same version as the server) are used and nothing needs
installing locally. Dumps are written to this machine.

Examples:
    # Write globals.sql plus one <database>.dump per database into ./backup
    python postgres_tools.py --pod tiled-postgres-0 --namespace tiled dump backup

    # Create a role that can read every database but change nothing.
    # Asks for the new role's password.
    python postgres_tools.py --pod tiled-postgres-0 --namespace tiled readonly-role

Restore (roles first on a fresh server, then each database). Scale tiled to
zero first, or it creates the schema in the empty database and the restore
fails with "already exists" errors:
    kubectl scale -n NS deploy/TILED --replicas=0
    kubectl exec -i -n NS POD -- psql -U postgres -d postgres < backup/globals.sql
    kubectl exec -n NS POD -- createdb -U postgres tiled
    kubectl exec -i -n NS POD -- pg_restore -U postgres -d tiled --exit-on-error < backup/tiled.dump
    kubectl scale -n NS deploy/TILED --replicas=1
"""

import argparse
import getpass
import subprocess
from datetime import datetime
from pathlib import Path

LIST_DATABASES = (
    "SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate"
)


def kubectl_exec(args, command: list[str]) -> list[str]:
    """The kubectl command that runs `command` inside the postgres pod."""
    cmd = ["kubectl", "exec", "-i"]
    if args.namespace:
        cmd += ["-n", args.namespace]
    if args.container:
        cmd += ["-c", args.container]
    return cmd + [args.pod, "--", *command, "-U", args.user]


def dump(args) -> None:
    out_dir = Path(args.out_dir or datetime.now().strftime("pg_dump_%Y%m%d_%H%M%S"))
    out_dir.mkdir(parents=True, exist_ok=True)

    # Roles and tablespaces live outside any one database, pg_dump skips them.
    with open(out_dir / "globals.sql", "wb") as f:
        subprocess.run(
            kubectl_exec(args, ["pg_dumpall", "--globals-only"]), stdout=f, check=True
        )

    databases = subprocess.run(
        kubectl_exec(args, ["psql", "-d", "postgres", "-AtX", "-c", LIST_DATABASES]),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()

    for database in databases:
        print(f"Dumping {database}")
        # Custom format: compressed, and pg_restore can pick out single tables.
        with open(out_dir / f"{database}.dump", "wb") as f:
            subprocess.run(
                kubectl_exec(args, ["pg_dump", "--format=custom", "-d", database]),
                stdout=f,
                check=True,
            )
    print(f"Dumps written to {out_dir}")


def readonly_role(args) -> None:
    password = getpass.getpass(f"Password for new role {args.role}: ")
    if password != getpass.getpass("Again: "):
        raise SystemExit("Passwords don't match.")

    # psql's \set reads backslashes as escapes and needs quotes doubled.
    quoted = password.replace("\\", "\\\\").replace("'", "''")
    # pg_read_all_data (Postgres 14+) is SELECT on every table, view and
    # sequence in every database, including ones created later. It grants no
    # INSERT/UPDATE/DELETE/DDL, so the role cannot change anything.
    # default_transaction_read_only is a second guard on top of that.
    # On Postgres < 15 every role may CREATE in the public schema unless that
    # was revoked (REVOKE CREATE ON SCHEMA public FROM PUBLIC), so new tables
    # are only blocked there by the read-only default.
    sql = f"""
\\set pw '{quoted}'
CREATE ROLE :"role" LOGIN PASSWORD :'pw'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
GRANT pg_read_all_data TO :"role";
ALTER ROLE :"role" SET default_transaction_read_only = on;
"""
    # The password goes in on stdin, not the command line, so it never shows
    # up in the pod's process list.
    subprocess.run(
        kubectl_exec(
            args,
            [
                "psql",
                "-d",
                "postgres",
                "-v",
                "ON_ERROR_STOP=1",
                "-v",
                f"role={args.role}",
            ],
        ),
        input=sql,
        text=True,
        check=True,
    )
    print(f"Created read-only role {args.role}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--pod", required=True, help="postgres pod name")
    parser.add_argument("--namespace", help="default: current kubectl namespace")
    parser.add_argument("--container", help="default: the pod's default container")
    parser.add_argument("--user", default="postgres", help="superuser to run as")
    commands = parser.add_subparsers(dest="command", required=True)

    dump_parser = commands.add_parser("dump", help="dump every database")
    dump_parser.add_argument("out_dir", nargs="?", help="default: pg_dump_<timestamp>")
    dump_parser.set_defaults(run=dump)

    role_parser = commands.add_parser("readonly-role", help="create a read-only role")
    role_parser.add_argument("role", nargs="?", default="tiled_readonly")
    role_parser.set_defaults(run=readonly_role)

    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
