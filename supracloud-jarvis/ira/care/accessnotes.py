"""Access notes (key safe, which door, pets): who may open them, and the
column names. The sealing itself lives in `core/connectors/care_access_notes.py`,
the one package allowed to handle key material (see
tests/architecture/test_credential_boundary.py). The notes are encrypted under a
per-row data key wrapped under the configured key-encryption key, bound to the
tenant and the person's row. Opening is allowed only for tier 1 and tier 2
recipients while an alert is open, and the caller writes the access log."""
from __future__ import annotations

import uuid
from typing import Optional

from core.connectors.care_access_notes import (  # noqa: F401 - re-exported for the care package
    COLUMNS,
    MAX_LENGTH,
    AccessNotesUnavailable,
    configured,
    seal_notes as seal,
    use_notes,
)


def can_view(alert_status: str, *, viewer_user_id: Optional[uuid.UUID], circle: list[dict], is_manager: bool) -> bool:
    """Tier 1 and 2 recipients, while the alert is open, escalating or
    acknowledged (the responder is on the way). A manager of the care
    organisation is tier 2 by role. Viewers and tier-3 contacts never."""
    if alert_status not in ("open", "escalating", "acknowledged"):
        return False
    if is_manager:
        return True
    return any(m.get("user_id") == viewer_user_id and int(m.get("tier") or 3) <= 2 and m.get("active", True)
               for m in circle if viewer_user_id is not None)
