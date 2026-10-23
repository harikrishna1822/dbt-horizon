class DbtHorizonError(Exception):
    """An expected failure with a message meant for the user (no traceback)."""


class ConfigError(DbtHorizonError):
    pass


class RepoError(DbtHorizonError):
    pass


class CatalogError(DbtHorizonError):
    pass
