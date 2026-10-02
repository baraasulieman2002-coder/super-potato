"""Command line: python -m namareq <command>

  init  --db PATH                         create an empty database
  create-owner --db PATH --username U     create the owner account (prompts for password)
  demo  --db PATH                         create a database filled with demo data
  serve --db PATH [--port 5000]           run the web interface (development server)
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

from . import db, demo, permissions


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="namareq")
    parser.add_argument("command", choices=("init", "create-owner", "demo", "serve"))
    parser.add_argument("--db", default=os.environ.get("NAMAREQ_DB", "namareq.sqlite"))
    parser.add_argument("--username", default="owner")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--today", help="pretend today is this date (demo/testing)")
    args = parser.parse_args(argv)

    if args.command == "init":
        conn = db.connect(args.db)
        db.init_db(conn)
        print(f"Initialised {args.db}")
    elif args.command == "create-owner":
        conn = db.connect(args.db)
        db.init_db(conn)
        password = getpass.getpass("Owner password (min 8 chars): ")
        permissions.create_user(conn, args.username, args.username, password, "owner")
        print("Owner created.")
    elif args.command == "demo":
        if os.path.exists(args.db):
            print(f"{args.db} already exists; choose another path.", file=sys.stderr)
            return 1
        conn = db.connect(args.db)
        demo.build(conn, args.today or "2026-10-02")
        print(f"Demo database written to {args.db}.\n"
              "  owner / owner-pass-123\n  accountant / accountant-pass-1\n  cashier / cashier-pass-12")
    elif args.command == "serve":
        from .web.app import create_app
        app = create_app(args.db, today=args.today)
        app.run(port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
