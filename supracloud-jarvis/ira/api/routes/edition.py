"""Which product this process is serving — GET /api/v1/edition.

WHAT IT IS FOR

A client cannot currently tell which product it is talking to. That gap is not
theoretical: gating the billing routes off in the private edition left
`frontend/app/billing/page.tsx` showing an error where it should say the product
has no billing, because the page had no way to ask.

WHY IT IS DERIVED, NOT WRITTEN

Every value below comes from `core.editions.edition_default`, the same table the
mounts and the runtime behaviour read. That is the whole design. A capabilities
endpoint maintained by hand is one more claim that drifts from the code — which
is the defect class this repository has spent a programme finding, and it would
be a poor joke to introduce a fresh instance of it in the endpoint whose job is
to tell clients the truth.

Adding a key to `_EDITION_DEFAULTS` therefore adds it here, with no edit to this
file, and `tests/core/test_edition_endpoint.py` fails if the two ever disagree.

WHAT `capabilities` DOES AND DOES NOT MEAN

It reports whether the product OFFERS a capability in this edition. It does NOT
promise that the corresponding routes are absent when it says false, and a
client must not infer that:

  * `billing` false means the routes are genuinely not mounted — that is a
    mount-time gate, and `/api/v1/billing/*` returns 404.
  * `armory` and `forge` false mean the capability is off, while the routers
    are still mounted and answer 403 to a non-owner. `21f8226` chose a
    different remedy for those: the process REFUSES TO START if either is
    enabled on cloud, which is a stronger guarantee than an absent route and a
    deliberately different one.

Reporting both under one honest word — "does this product do X" — is the
question a client actually has. How each is enforced is the server's business,
and is recorded here so nobody reads more into a `false` than it says.

AUTHENTICATION

Behind `require_auth`. The edition is not a secret — the repository is public
and `/docs` lists what is mounted — but a client only needs this once it is
already signed in and deciding what to render, so there is no reason to add an
unauthenticated surface for it.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from api.middleware.auth import require_auth
from core.editions import current_edition, edition_default, edition_default_keys

router = APIRouter(prefix="/edition", tags=["edition"])

#: Keys that describe a CAPABILITY a client might render for, rather than an
#: internal posture. `auth_fail_closed_on_redis` is a correct edition default
#: and none of a client's business; `privacy_mode` is reported because the
#: interface says which it is running under.
#:
#: Listed as an exclusion rather than an allow-list, so a new capability is
#: published by default and someone has to decide, in writing, to hide it. An
#: allow-list would silently omit whatever the next person forgot to add — the
#: same shape as a hand-maintained count.
_NOT_CLIENT_FACING: frozenset[str] = frozenset({"auth_fail_closed_on_redis"})


def capability_map() -> dict[str, object]:
    """Every client-facing edition default, read from the one table."""
    return {
        key: edition_default(key)
        for key in edition_default_keys()
        if key not in _NOT_CLIENT_FACING
    }


@router.get("")
async def edition(_user: str = Depends(require_auth)) -> dict[str, object]:
    """The edition this process serves, and what it offers."""
    return {
        "edition": current_edition(),
        "capabilities": capability_map(),
    }
