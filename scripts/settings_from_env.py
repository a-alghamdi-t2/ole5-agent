"""Copy the settings the Settings page manages from .env into the page, once.

    python scripts/settings_from_env.py          # what would be copied; changes nothing
    python scripts/settings_from_env.py --yes    # copy them

Every setting the page can change that is set in .env (or the environment) and
not already saved on the page is saved there with the same value -- passwords
and keys too, encrypted. Nothing changes in how the app behaves: the page
holds the values .env held. Afterwards those lines can be deleted from .env,
leaving only what the page cannot hold; the list is printed at the end.

A value already saved on the page is left alone: the page wins over .env, so
it is the one in force. Run it on the machine whose .env this is, with the
app's database reachable. Safe to run again: anything already copied is
skipped.
"""

from __future__ import annotations

import argparse
import sys

from pydantic import SecretStr

from support_agent.config import env_settings
from support_agent.db import postgres
from support_agent import settings_store as store

WHO = "copied from .env"


def _value(v):
    if isinstance(v, SecretStr):
        return v.get_secret_value()
    if isinstance(v, bool):
        return v
    return "" if v is None else str(v)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="copy them; without it, only list")
    args = ap.parse_args()

    env = env_settings()
    saved = set(store._rows())
    in_env = env.model_fields_set
    todo, skipped = {}, []
    for f in store.FIELDS:
        if f.key not in in_env:
            continue
        if f.key in saved:
            skipped.append(f.key)
            continue
        todo[f.key] = _value(getattr(env, f.key))

    for key, value in todo.items():
        f = store.BY_KEY[key]
        shown = "(set, hidden)" if f.kind == "secret" else repr(value)
        print(f"  {f.group:10} {f.label:34} {key.upper():28} {shown}")
    for key in skipped:
        print(f"  {'':10} {store.BY_KEY[key].label:34} {key.upper():28} already on the page, left alone")
    if not todo:
        print("Nothing to copy: every setting in .env the page manages is already there.")
        return
    if not args.yes:
        print(f"\n{len(todo)} to copy. Run again with --yes to copy them.")
        return

    failed = {}
    try:
        store.save(todo, WHO)
    except store.SettingsError:
        # One refused value (a fallback not locked on the Configuration page,
        # say) must not stop the rest: one at a time, and report the refused.
        for key, value in todo.items():
            try:
                store.save({key: value}, WHO)
            except store.SettingsError as exc:
                failed[key] = str(exc)
    copied = [k for k in todo if k not in failed]
    print(f"\ncopied {len(copied)} to the Settings page" + (f", {len(failed)} refused:" if failed else "."))
    for key, why in failed.items():
        print(f"  {key.upper()}: {why}")

    print("\nThese lines can now be deleted from .env:")
    for key in copied:
        print(f"  {key.upper()}")
    print("\nKeep in .env: DATABASE_URL, SESSION_SECRET, SUPER_ADMIN_EMAILS, QDRANT_URL, "
          "DOCLING_URL, and where set PRODUCT, SESSION_COOKIE, LOG_LEVEL"
          + (" and the ones refused above" if failed else "")
          + ". With Docker also POSTGRES_*, PIP_EXTRA_INDEX_URL, COMPOSE_PROJECT_NAME and the ports.")


if __name__ == "__main__":
    try:
        main()
    finally:
        postgres.close_pool()
