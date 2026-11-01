"""Who is calling, and what they may touch.

The service is single-organization (no tenants). Every consuming application
authenticates as an **API client**. Its scopes decide which operations it may call, and
an optional KB allow-list restricts which knowledge bases it may see (ARCHITECTURE.md
§11). KB lookups in the repository layer take an :class:`AccessContext`, so the
allow-list is enforced in one place and new routes can't forget it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

# Scopes understood by the API (ARCHITECTURE.md §11.2).
SCOPE_ADMIN = "admin"  # everything, incl. managing API clients of any kind
SCOPE_KB_READ = "kb:read"  # read KBs, documents, jobs
SCOPE_KB_WRITE = "kb:write"  # create/update/delete KBs and documents
SCOPE_QUERY = "query"  # call /retrieve and /query
SCOPE_CLIENTS = "clients:manage"  # create/list/revoke API clients
ALL_SCOPES = frozenset({SCOPE_ADMIN, SCOPE_KB_READ, SCOPE_KB_WRITE, SCOPE_QUERY, SCOPE_CLIENTS})
# Default grant for a new application's key: everything except administration.
DEFAULT_APP_SCOPES = frozenset({SCOPE_KB_READ, SCOPE_KB_WRITE, SCOPE_QUERY})


@dataclass(frozen=True, slots=True)
class AccessContext:
    """The authenticated caller of one request (or the system, inside workers)."""

    client_id: uuid.UUID | None  # None for the bootstrap admin key and internal callers
    scopes: frozenset[str] = field(default_factory=frozenset)
    # None = every KB; otherwise only the listed ones.
    allowed_kb_ids: frozenset[uuid.UUID] | None = None

    @property
    def is_admin(self) -> bool:
        return SCOPE_ADMIN in self.scopes

    def has_scope(self, scope: str) -> bool:
        # Admins implicitly hold every scope.
        return self.is_admin or scope in self.scopes

    def can_access_kb(self, kb_id: uuid.UUID) -> bool:
        """Apply the client's optional KB allow-list (admins are never restricted)."""
        if self.is_admin or self.allowed_kb_ids is None:
            return True
        return kb_id in self.allowed_kb_ids


# Used by Temporal activities. They act on IDs that an authorized API call already
# validated before starting the workflow.
SYSTEM_CONTEXT = AccessContext(client_id=None, scopes=frozenset({SCOPE_ADMIN}))
