"""
Dotenv loader for this repository.

Vendored from NuKa's tools/utils/env.py and pointed at THIS repo's own dotenv file
rather than a shared multi-provider one. That is deliberate: this repository is
public, and reaching into a shared dotenv would couple a public repo to a file
holding unrelated provider credentials.

Resolution order: this repo's dotenv file, then the process environment. The
environment fallback is what lets the Alpaca CLI and these scripts share one set
of values -- the CLI reads ALPACA_API_KEY / ALPACA_SECRET_KEY from the env, and so
do we when the file does not supply them.
"""
import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
DOTENV_PATH = os.path.join(REPO_ROOT, '.' + 'env')


def load_env_var(name, required=True):
    """Read `name` from the dotenv file, then the environment. Exit if required and absent."""
    if os.path.exists(DOTENV_PATH):
        with open(DOTENV_PATH, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if line.startswith(f'{name}='):
                    return line.split('=', 1)[1].strip().strip('"\'')

    if os.environ.get(name):
        return os.environ[name]

    if not required:
        return None

    print(f"ERROR: {name} is not set.")
    print(f"       Create {DOTENV_PATH} from the committed example, or export {name}.")
    sys.exit(1)


def env_flag(name, default=False):
    """
    Read a 0/1/true/false style flag -- ENVIRONMENT FIRST, unlike load_env_var.

    Credentials keep the dotenv-first order above on purpose: this repository is
    deliberately self-contained and must not silently pick up a shared dotenv's
    keys that happen to be exported in the shell.

    Operational switches are the opposite case. `SHADOW=1` in the launchd plist is
    an override the operator sets for one scheduled run; a stale `SHADOW=0` line in
    the dotenv silently winning over it is how the agent came to trade for real
    while both the plist and preflight reported it was in shadow mode. An override
    that the thing it overrides can veto is not an override.
    """
    # `or None`, not `.get(name)`: an exported-but-empty SHADOW= is not an answer.
    # Treating '' as a definitive false would let a blank plist value or a bare
    # `export SHADOW=` silently force real orders -- the same class of accident this
    # precedence order exists to prevent, arriving from the other direction.
    raw = os.environ.get(name) or None
    if raw is None:
        raw = load_env_var(name, required=False)
    if raw is None:
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')
