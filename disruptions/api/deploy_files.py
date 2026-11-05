"""Deployment files that live in smart-avvik-deployment-config, not in this repository.

The Helm chart and its env values, Terraform, the deploy workflows and the orchestrator manifest
are private. The
checks that hold the code and the deployment to each other still live beside the code. They run
when that repository's files are copied over this checkout at the same paths, and skip otherwise,
with a count printed at exit so a skip is never silent.
"""

import atexit
import functools
import inspect
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]

_skipped: list[str] = []


def _absent(paths: tuple[str, ...]) -> list[str]:
    return [p for p in paths if not (REPO / p).exists()]


def read(path: str) -> str:
    """The file's text, or "" when it is absent. Only checks guarded below may rely on it."""
    target = REPO / path
    return target.read_text() if target.exists() else ""


def needs(*paths: str):
    """Skip the decorated check unless every path exists."""

    def wrap(fn):
        @functools.wraps(fn)
        def run(*args, **kwargs):
            absent = _absent(paths)
            if absent:
                _skip(fn.__name__, absent)
                return None
            return fn(*args, **kwargs)

        return run

    return wrap


def missing(*paths: str) -> bool:
    """True, and the rest of the calling check counted as skipped, when any path is absent."""
    absent = _absent(paths)
    if absent:
        _skip(inspect.stack()[1].function + " (deployment half)", absent)
    return bool(absent)


def _skip(name: str, absent: list[str]) -> None:
    _skipped.append(name)
    print(f"  skip {name}: no {', '.join(absent)}")


@atexit.register
def _report() -> None:
    if _skipped:
        print("\n" + "!" * 78)
        print(f"!! SKIPPED {len(_skipped)} DEPLOYMENT CHECKS: the deployment files are not here.")
        print("!! They run in smart-avvik-deployment-config, over a checkout of this repository.")
        print("!" * 78)
