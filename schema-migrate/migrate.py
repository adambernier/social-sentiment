"""CLI entry point for the one-shot database migration service."""

from schema_migrations import apply_migrations


def main() -> None:
    print("Checking for pending database migrations...", flush=True)
    applied = apply_migrations()
    if applied:
        print(f"Applied database migrations: {', '.join(applied)}", flush=True)
    else:
        print("Database schema is already current.", flush=True)


if __name__ == "__main__":
    main()
