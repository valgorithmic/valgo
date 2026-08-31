class ValgoError(Exception):
    """Base exception for the Valgo SDK."""


class AuthenticationError(ValgoError):
    pass


class AuthorizationError(ValgoError):
    pass


class NotFoundError(ValgoError):
    pass


class ConflictError(ValgoError):
    pass


class ValidationError(ValgoError):
    pass


class TransferError(ValgoError):
    pass


class IntegrityError(TransferError):
    pass
