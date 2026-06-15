from __future__ import annotations

import argparse
import getpass
import sys

import admin_db


def _read_email() -> str:
    return input("Admin email: ").strip().lower()


def _read_password_pair() -> tuple[str, str]:
    password = getpass.getpass("Password: ")
    confirm = getpass.getpass("Confirm password: ")
    return password, confirm


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage LeadFlow admin users.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("create", help="Create a new administrator.")
    subparsers.add_parser("reset-password", help="Reset an existing administrator password.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    admin_db.init()
    email = _read_email()
    password, confirm = _read_password_pair()
    if password != confirm:
        print("Error: password confirmation does not match.", file=sys.stderr)
        return 1

    if args.command == "create":
        result = admin_db.create_admin(email, password)
    else:
        result = admin_db.reset_admin_password(email, password)

    if not result.get("ok"):
        print(f"Error: {result.get('error', 'operation failed')}", file=sys.stderr)
        return 1

    print("Admin account updated. Password change is required at next login.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
