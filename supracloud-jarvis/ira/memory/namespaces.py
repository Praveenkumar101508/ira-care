"""memory/namespaces.py — the reserved ``user_id`` namespaces of memory_embeddings.

``memory_embeddings.user_id`` carries two kinds of value in one column:

  * an account name — chat passes the signed-in username
    (``api/routes/chat.py::_workspace_username``), so each person's transcripts
    stay apart from everyone else's within a workspace;
  * a fixed marker that a subsystem writes its own rows under — the Memory
    Vault, the self-knowledge corpus, meeting transcripts and so on.

Nothing in the column tells the two apart. An account whose name equals a
marker therefore reads and writes that subsystem's rows as if they were its own
chat memory: a delegated account named ``owner`` would recall the owner's
vault without being the owner.

This module is the single list of markers. Each subsystem takes its constant
from here, and ``security/delegated.py`` refuses every one of them as a
delegated username, so adding a namespace here reserves it in the same edit.
``tests/test_delegated_access.py`` fails if a namespace is defined anywhere
else, which is what stops the list from drifting.

Deliberately import-free: ``security/`` imports it on the login path, and it
must not drag the embedding stack in with it.
"""
from __future__ import annotations

#: The Memory Vault — the owner's curated entries (``api/routes/memory.py``),
#: the realtime brain's ``IraMemorySink`` and command-created memories.
VAULT_NAMESPACE = "owner"

#: The default scope of ``memory.store.retrieve`` and system-level writers.
SYSTEM_NAMESPACE = "system"

#: IRA's own documentation corpus (``memory/self_knowledge.py``). Kept apart
#: from the two above so neither ``retrieve()`` nor the vault listing sees it.
SELF_KNOWLEDGE_NAMESPACE = "self_knowledge"

#: Meeting transcripts (``meetings/store.py``). They never surface in owner
#: recall, and owner memories never surface in a meeting answer.
MEETING_NAMESPACE = "meeting"

#: The technical library (``memory/technical_library.py``). Its retrieval SQL
#: also spells this value as a literal; that module refuses to import if the
#: two ever disagree.
TECH_LIBRARY_NAMESPACE = "technical_library"

#: Every value above. No account may be named after one of these.
RESERVED_NAMESPACES: frozenset[str] = frozenset({
    VAULT_NAMESPACE,
    SYSTEM_NAMESPACE,
    SELF_KNOWLEDGE_NAMESPACE,
    MEETING_NAMESPACE,
    TECH_LIBRARY_NAMESPACE,
})


#: Namespaces no age-based retention pass deletes: the weekly purge
#: (``memory.store.purge_old_memories``) and a user's own window
#: (``core.privacy.retention.enforce_for``) both skip them.
#:
#: These two are reference corpora, rebuilt by their own ingest rather than
#: anything a person said. A purge with no namespace filter deleted them, and
#: the Technical Library's ingest skips a document it has already indexed, so
#: nothing put them back. Meeting transcripts are deliberately NOT here: they
#: hold other people's speech, exist only in memory_embeddings, and nothing
#: deletes a single meeting, so retention is what removes them.
#:
#: Every namespace above needs a retention decision, and
#: ``tests/test_memory_retention_scope.py`` fails until a new one has one.
RETENTION_EXEMPT_NAMESPACES: frozenset[str] = frozenset({
    TECH_LIBRARY_NAMESPACE,
    SELF_KNOWLEDGE_NAMESPACE,
})


def is_reserved_namespace(name: str | None) -> bool:
    """True when ``name`` would land in a reserved namespace.

    Normalised the way delegated usernames are (stripped, lower-cased), so a
    differently-cased spelling is refused too rather than slipping past.
    """
    return (name or "").strip().lower() in RESERVED_NAMESPACES


__all__ = [
    "VAULT_NAMESPACE",
    "SYSTEM_NAMESPACE",
    "SELF_KNOWLEDGE_NAMESPACE",
    "MEETING_NAMESPACE",
    "TECH_LIBRARY_NAMESPACE",
    "RESERVED_NAMESPACES",
    "RETENTION_EXEMPT_NAMESPACES",
    "is_reserved_namespace",
]
