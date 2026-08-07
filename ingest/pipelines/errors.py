"""Exception hierarchy shared across ingest pipelines."""


class IngestionError(Exception):
    pass


class DatabaseError(IngestionError):
    pass


class DownloadError(IngestionError):
    pass


class ValidationError(IngestionError):
    pass
