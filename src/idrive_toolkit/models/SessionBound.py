class SessionBound:
    """Keeps resources attached to the session that created them."""

    def __init__(self, session=None):
        self._session = session

    @property
    def session(self):
        if self._session is None:
            raise RuntimeError("This object has no client session. Create it through Client or pass session=client.session.")
        return self._session
