"""Keep hook/CLI tests away from the real vault data dir.

The hooks append to `<data>/.graph/usage.log`, where <data> comes from
VAULT_DATA, then VAULT_HOME. Importing this module removes both from
os.environ, so every subprocess copy of the environment is clean; tests that
need a data dir set their own temp dir explicitly."""
from __future__ import annotations

import os

VAULT_DATA_VARS = ("VAULT_DATA", "VAULT_HOME")


def scrub_vault_env() -> None:
    for var in VAULT_DATA_VARS:
        os.environ.pop(var, None)


scrub_vault_env()
