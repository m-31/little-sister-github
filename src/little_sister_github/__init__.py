"""little-sister check types: ``github`` and ``github-rate-limit``.

Importing this package registers **both** types in little-sister's
``CHECK_TYPES``. A deployment therefore needs one line in its ``wsgi.py``, before
it imports ``little_sister.app``::

    import little_sister_github          # noqa: F401  registers its types
    from little_sister.app import app    # noqa: E402  builds the engine

Two types, one package, because they read one API through one client with one
credential and version together; why the budget is a type of its own rather than
an aspect of ``github`` is ADR-0001.

``require_api`` declares the **check API epoch** this package was built for. It
refuses at startup, naming both epochs, when the library has moved past the
surface below — the upgrade an install-time floor cannot see, because a floor has
no ceiling (little-sister ADR-0051). Without it the same mismatch would surface as
an ``ImportError`` from inside this package, which reads like our bug.
"""
from little_sister.checks import require_api

require_api(1)

from little_sister_github import (  # noqa: E402  both register their type
    github,
    rate_limit,
)

__version__ = "0.1.0"

__all__ = ["github", "rate_limit"]
