"""Assembling and persisting a companion.

Two layers, kept apart because only one of them needs a database:

  * :func:`build_companion` validates a set of selections against the catalogue
    and returns a profile. Pure. This is where a selection is refused.
  * :func:`save_companion` / :func:`load_companion` persist it, tenant-scoped
    through `workspace_connection` so Row-Level Security is bound to the
    authenticated Principal for every statement.

**Selections are re-validated on the way in, not trusted from the client.** The
Companion Creator only ever shows offerable options, but "the UI only offered
valid choices" is a statement about the UI, not about the request that arrived.
A client that posts `avatar_slug=ira-portrait` to a cloud deployment gets a
refusal here, not a rendered likeness of somebody else.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from core.companion.catalogue import (
    default_avatar,
    default_voice,
    get_avatar,
    get_personality,
    get_voice,
    list_personalities,
)
from core.companion.licence import (
    evaluate_avatar_licence,
    evaluate_voice_availability,
)
from core.companion.models import CompanionProfile
from core.companion.profile import (
    CompanionValidationError,
    validate_profile,
)
from core.editions import current_edition
from core.identity.db_context import workspace_connection
from core.identity.principal import Principal

logger = logging.getLogger("ira.companion.service")


def build_companion(
    *,
    edition: Optional[str] = None,
    display_name: str = "IRA",
    avatar_slug: str = "",
    voice_slug: str = "",
    personality_slug: str = "supportive",
    language: str = "en-GB",
    form_of_address: str = "",
    memory_mode: str = "ask",
    autonomy_level: str = "confirm_before_action",
    status: str = "draft",
    version: int = 1,
) -> CompanionProfile:
    """Validate selections against the catalogue and return a profile.

    Raises :class:`CompanionValidationError` naming the offending field.

    An EMPTY avatar or voice slug is filled from the edition's default rather
    than refused — the creator saves partial drafts as the user moves through
    the steps, and a draft with nothing chosen yet is a normal state, not an
    error. A slug that is present but not offerable is always refused.
    """
    edition_name = edition or current_edition()

    profile = validate_profile(
        display_name=display_name,
        avatar_slug=avatar_slug,
        voice_slug=voice_slug,
        personality_slug=personality_slug,
        language=language,
        form_of_address=form_of_address,
        memory_mode=memory_mode,
        autonomy_level=autonomy_level,
        status=status,
        version=version,
    )

    resolved_avatar = profile.avatar_slug
    if resolved_avatar:
        entry = get_avatar(resolved_avatar)
        if entry is None:
            raise CompanionValidationError(
                "avatar_slug", f"{resolved_avatar!r} is not in the catalogue"
            )
        decision = evaluate_avatar_licence(entry, edition=edition_name)
        if not decision.allowed:
            raise CompanionValidationError(
                "avatar_slug",
                f"{resolved_avatar!r} cannot be offered in the {edition_name} "
                f"edition: {decision.reason}",
            )
    else:
        fallback = default_avatar(edition=edition_name)
        resolved_avatar = fallback.slug if fallback else ""

    resolved_voice = profile.voice_slug
    if resolved_voice:
        voice = get_voice(resolved_voice)
        if voice is None:
            raise CompanionValidationError(
                "voice_slug", f"{resolved_voice!r} is not in the catalogue"
            )
        decision = evaluate_voice_availability(voice, edition=edition_name)
        if not decision.allowed:
            raise CompanionValidationError(
                "voice_slug",
                f"{resolved_voice!r} cannot be offered in the {edition_name} "
                f"edition: {decision.reason}",
            )
    else:
        fallback_voice = default_voice(edition=edition_name)
        resolved_voice = fallback_voice.slug if fallback_voice else ""

    personality = get_personality(profile.personality_slug)
    if personality is None or not personality.active:
        raise CompanionValidationError(
            "personality_slug",
            f"{profile.personality_slug!r} is not an available personality "
            f"(choose one of: {', '.join(p.slug for p in list_personalities())})",
        )

    # A companion cannot be ACTIVATED without an avatar and a voice. A draft may
    # be missing them; an active companion that has neither is a face the user
    # never chose and a voice they never heard.
    if profile.status == "active":
        if not resolved_avatar:
            raise CompanionValidationError(
                "avatar_slug",
                "no avatar is available in this edition — a companion cannot be "
                "activated without one",
            )
        if not resolved_voice:
            raise CompanionValidationError(
                "voice_slug",
                "no voice is available in this edition — a companion cannot be "
                "activated without one",
            )

    from dataclasses import replace

    return replace(profile, avatar_slug=resolved_avatar, voice_slug=resolved_voice)


# ── Persistence ──────────────────────────────────────────────────────────────
#
# Every statement runs inside `workspace_connection`, which sets `app.tenant_id`
# with SET LOCAL from the authenticated Principal. The WHERE clauses below still
# name user_id — RLS scopes to the tenant, and within a tenant a user must not
# read another member's companion.


_SELECT_COLUMNS = (
    "display_name, avatar_slug, voice_id, personality, language, "
    "form_of_address, memory_mode, autonomy_level, status, version"
)


def _row_to_profile(row: Any) -> CompanionProfile:
    return CompanionProfile(
        display_name=row["display_name"],
        avatar_slug=row["avatar_slug"] or "",
        voice_slug=row["voice_id"] or "",
        personality_slug=row["personality"] or "supportive",
        language=row["language"],
        form_of_address=row["form_of_address"] or "",
        memory_mode=row["memory_mode"],
        autonomy_level=row["autonomy_level"],
        status=row["status"],
        version=row["version"],
    )


async def load_companion(principal: Principal) -> Optional[CompanionProfile]:
    """The caller's active companion, or their draft, or None.

    Active is preferred over draft: a user who has activated a companion and
    then started editing a new one is still talking to the active one.
    """
    async with workspace_connection(principal) as conn:
        row = await conn.fetchrow(
            f"""
            SELECT {_SELECT_COLUMNS} FROM companions
             WHERE user_id = $1 AND status IN ('active', 'draft')
             ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, updated_at DESC
             LIMIT 1
            """,
            principal.user_id,
        )
    return _row_to_profile(row) if row else None


async def save_companion(
    principal: Principal,
    profile: CompanionProfile,
    *,
    reason: str = "",
) -> CompanionProfile:
    """Upsert the caller's companion and append a version snapshot.

    The profile write and the version row are one transaction — `tenant_connection`
    opens one and everything here runs inside it. A history that can be missing
    its most recent entry because the second statement failed is a history the
    Privacy Centre cannot make a claim from.
    """
    async with workspace_connection(principal) as conn:
        existing = await conn.fetchrow(
            "SELECT id, version FROM companions WHERE user_id = $1 AND status = $2",
            principal.user_id,
            profile.status,
        )

        if existing:
            version = int(existing["version"]) + 1
            companion_id = existing["id"]
            await conn.execute(
                """
                UPDATE companions
                   SET display_name = $2, avatar_slug = $3, voice_id = $4,
                       personality = $5, language = $6, form_of_address = $7,
                       memory_mode = $8, autonomy_level = $9, status = $10,
                       version = $11, updated_at = NOW(),
                       activated_at = CASE WHEN $10 = 'active' AND activated_at IS NULL
                                           THEN NOW() ELSE activated_at END
                 WHERE id = $1
                """,
                companion_id, profile.display_name, profile.avatar_slug,
                profile.voice_slug, profile.personality_slug, profile.language,
                profile.form_of_address, profile.memory_mode,
                profile.autonomy_level, profile.status, version,
            )
        else:
            version = 1
            companion_id = await conn.fetchval(
                """
                INSERT INTO companions
                    (tenant_id, user_id, display_name, avatar_slug, voice_id,
                     personality, language, form_of_address, memory_mode,
                     autonomy_level, status, version, activated_at, workspace_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                        CASE WHEN $11 = 'active' THEN NOW() ELSE NULL END,
                        NULLIF(current_setting('app.workspace_id', true), '')::uuid)
                RETURNING id
                """,
                principal.tenant_id, principal.user_id, profile.display_name,
                profile.avatar_slug, profile.voice_slug, profile.personality_slug,
                profile.language, profile.form_of_address, profile.memory_mode,
                profile.autonomy_level, profile.status, version,
            )

        from dataclasses import replace

        saved = replace(profile, version=version)

        await conn.execute(
            """
            INSERT INTO companion_versions
                (tenant_id, user_id, companion_id, version, snapshot, changed_by,
                 reason, workspace_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7,
                    NULLIF(current_setting('app.workspace_id', true), '')::uuid)
            ON CONFLICT (companion_id, version) DO NOTHING
            """,
            principal.tenant_id, principal.user_id, companion_id, version,
            json.dumps(saved.snapshot()), "user", reason,
        )

    return saved


async def list_versions(principal: Principal, *, limit: int = 50) -> list[dict[str, Any]]:
    """The caller's companion history, newest first."""
    capped = max(1, min(int(limit), 200))
    async with workspace_connection(principal) as conn:
        rows = await conn.fetch(
            """
            SELECT version, snapshot, changed_by, reason, created_at
              FROM companion_versions
             WHERE user_id = $1
             ORDER BY version DESC
             LIMIT $2
            """,
            principal.user_id,
            capped,
        )
    return [
        {
            "version": r["version"],
            "snapshot": r["snapshot"] if isinstance(r["snapshot"], dict) else json.loads(r["snapshot"]),
            "changed_by": r["changed_by"],
            "reason": r["reason"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]
