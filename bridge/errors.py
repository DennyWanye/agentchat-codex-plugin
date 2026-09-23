class BridgeError(Exception):
    """Base class for expected bridge failures."""


class RemoteError(BridgeError):
    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class AuthenticationError(RemoteError):
    pass


class ProtocolError(BridgeError):
    pass


class MessageValidationError(BridgeError):
    pass
