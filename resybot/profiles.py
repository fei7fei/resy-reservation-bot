"""Profiles: one Resy account each, stored as data/profiles/<slug>.yaml."""
from __future__ import annotations

import glob
import os
import re
import shutil

import yaml

from . import paths

MAIN = "me"
# Settings that belong to an account and are never copied into a new profile.
PERSONAL_KEYS = ("auth_token", "api_key", "email", "password", "payment_method_id", "profile_name", "alerts")
HEADER = "# Resy Bot profile. Contains a Resy login token -- don't share this file.\n"


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]


def ensure_main():
    os.makedirs(paths.PROFILES, exist_ok=True)
    if not os.path.exists(paths.profile_file(MAIN)):
        save(MAIN, {"profile_name": "Me", "dry_run": True})


def list_profiles() -> list[str]:
    ensure_main()
    slugs = sorted(os.path.splitext(os.path.basename(f))[0] for f in glob.glob(os.path.join(paths.PROFILES, "*.yaml")))
    return [MAIN] + [s for s in slugs if s != MAIN]


def load(slug: str) -> dict:
    try:
        with open(paths.profile_file(slug), "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def save(slug: str, cfg: dict):
    os.makedirs(paths.PROFILES, exist_ok=True)
    tmp = paths.profile_file(slug) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(HEADER)
        yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)
    os.replace(tmp, paths.profile_file(slug))  # atomic: a crash never leaves half a file


def label(slug: str) -> str:
    return load(slug).get("profile_name") or ("Me" if slug == MAIN else slug)


def create(name: str, copy_from: str | None = None) -> str:
    """New profile named `name`; copies restaurant/dates/times (never the account) from copy_from."""
    slug = slugify(name)
    if not slug or os.path.exists(paths.profile_file(slug)):
        raise ValueError("That name is taken — pick another.")
    base = {k: v for k, v in load(copy_from).items() if k not in PERSONAL_KEYS} if copy_from else {}
    base.update(profile_name=name.strip(), dry_run=True)
    save(slug, base)
    return slug


def delete(slug: str):
    if slug == MAIN:
        raise ValueError("The main profile can't be deleted.")
    os.remove(paths.profile_file(slug))
    shutil.rmtree(paths.run_dir(slug), ignore_errors=True)
