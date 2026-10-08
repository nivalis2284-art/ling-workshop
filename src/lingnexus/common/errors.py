class DomainError(Exception):
    """A safe, user-facing domain error, never an upstream response body."""


class Conflict(DomainError):
    pass


class RevisionConflict(Conflict):
    pass


class SourceInvalid(Conflict):
    pass


class NotFound(DomainError):
    pass


class PermissionDenied(DomainError):
    pass


class ContextTooLarge(DomainError):
    pass
