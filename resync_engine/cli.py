"""Command-line entry point.

  # preview generated SQL, no database needed:
  python -m resync_engine.cli print-sql --catalog config.skeleton.json --config resync.yaml

  # dry-run counts against a target:
  python -m resync_engine.cli run --catalog config.skeleton.json --config resync.yaml \
      --dsn host:1521/svc --user MAS --password ***

  # apply:
  python -m resync_engine.cli run ... --apply
"""
from __future__ import annotations

import argparse

from . import runner


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="resync_engine")
    sub = ap.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    # Defaults point at the public synthetic sample so a fresh clone runs offline. Point them at
    # your generated config.skeleton.json / resync.yaml (gitignored) for a real environment.
    common.add_argument("--catalog", default="examples/sample_catalog.json")
    common.add_argument("--config", default="examples/sample_resync.yaml")

    sub.add_parser("print-sql", parents=[common], help="render SQL offline")

    rn = sub.add_parser("run", parents=[common], help="dry-run or apply against a database")
    rn.add_argument("--dsn", required=True)
    rn.add_argument("--user", required=True)
    rn.add_argument("--password", required=True)
    rn.add_argument("--apply", action="store_true", help="apply changes (default: dry-run)")
    rn.add_argument("--allow-unsequenced", action="store_true",
                    help="permit surrogate tables without a sequence (dev only; unsafe)")

    args = ap.parse_args(argv)

    if args.cmd == "print-sql":
        print(runner.print_sql(args.catalog, args.config))
    elif args.cmd == "run":
        runner.run(args.catalog, args.config, dsn=args.dsn, user=args.user,
                   password=args.password, apply=args.apply,
                   allow_unsequenced=args.allow_unsequenced)


if __name__ == "__main__":
    main()
