"""The installed package holds certifi at or above the patched release.

`requests` verifies TLS against certifi's CA bundle, and every API call carries
the user's bearer token. certifi before 2026.10.19 trusts emSign roots whose
eMudhra intermediate keys are compromised (GHSA-6q48-6c7w-9gw2), so the
package's own requirement is what keeps a fresh install or an upgrade off that
trust store. The check reads the installed metadata, which is what installers
resolve from.
"""

from __future__ import annotations

from importlib.metadata import requires

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

PATCHED_CERTIFI = Version("2026.10.19")
LOWER_BOUND_OPERATORS = (">=", ">", "~=", "==")


def _base_requirement(name: str) -> Requirement:
    # Base dependencies carry no marker; extras such as `dev` do.
    matches = [
        req
        for req in map(Requirement, requires("scriptit-cli") or [])
        if canonicalize_name(req.name) == canonicalize_name(name) and req.marker is None
    ]
    assert len(matches) == 1, f"scriptit-cli must require {name} exactly once"
    return matches[0]


def test_certifi_minimum_is_the_patched_release():
    certifi = _base_requirement("certifi")
    lower_bounds = [
        Version(spec.version)
        for spec in certifi.specifier
        if spec.operator in LOWER_BOUND_OPERATORS
    ]
    assert lower_bounds and max(lower_bounds) >= PATCHED_CERTIFI, (
        f"certifi needs a minimum of at least {PATCHED_CERTIFI}; got {certifi}"
    )
