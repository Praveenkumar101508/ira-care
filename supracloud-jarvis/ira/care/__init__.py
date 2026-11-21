"""IRA CARE: the care data model and its API (`/api/v1/care`).

Everything here is scoped to the caller's verified tenant and workspace through
`core.identity.db_context.workspace_connection`, and every table it touches is
under forced row-level security (migration 099). Nothing in this package makes
a medical judgement: alerts name what was observed, and the ranges that drive
them are set per person by a carer or clinician.
"""
