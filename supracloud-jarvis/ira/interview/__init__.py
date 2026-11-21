"""
interview/ — the company-wise interview question bank, and how IRA reads it.

656 companies, 3,358 LeetCode problems, and which company asks which — held as a
175 KB file in the repository rather than fetched, so this works on a laptop with
the network off, like everything else IRA answers from.

What this package is for: **practice.** It answers "what does Stripe actually ask,
and what should I drill first" — the question LeetCode sells Premium to answer. It
is a study surface, deliberately reachable from chat and the API rather than wired
into the overlay's live-call path.

Two honesty constraints run through the modules:

  * **Frequency is upstream's number, not IRA's.** Nothing here recomputes or
    validates it, and every payload says so. A ranking presented as IRA's own
    judgement would be a fabrication.
  * **The snapshot has a date.** It is stated in every response, because a
    question bank silently two years stale is worse than one you know is old.
"""

from interview.dataset import (
    DatasetUnavailable,
    company,
    company_questions,
    list_companies,
    question,
    search,
    stats,
)
from interview.plan import build_plan

__all__ = [
    "DatasetUnavailable",
    "build_plan",
    "company",
    "company_questions",
    "list_companies",
    "question",
    "search",
    "stats",
]
