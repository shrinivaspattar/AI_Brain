from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ingestion.text_extractor import extract_text
from app.models.import_job import ImportJob, ImportStatus

# A full-document read is meant to show more than a chunk-level search
# hit, but still needs a practical bound - both to keep a single tool
# result from flooding the model's context window and to bound what a
# best-effort-truncated audit copy (see ChatService.MAX_TOOL_RESULT_LENGTH)
# is even trying to summarize.
MAX_FILE_READ_LENGTH = 20_000


class FileAccessError(Exception):
    """A file read was refused or failed: outside any allowed directory,
    not found, or not decodable as text."""


class FileAccessService:
    """Read-only access to files on disk - the first (and, deliberately,
    only) file-operation capability in AI_Brain.

    This is a materially bigger security surface than anything else the
    chat loop can do (every other tool only ever touches rows already
    inside Postgres), so the scope here is intentionally narrow, per an
    explicit scoping decision:

    - Read-only. No write, move, rename, or delete capability exists
      here or anywhere else in the codebase.
    - A path may only be read if it falls under the `source_path` of an
      ImportJob that has actually COMPLETED - i.e. a directory AI_Brain
      has already scanned and ingested from, not an arbitrary
      filesystem path. This keeps the wider personal-data corpus and
      any "master backup" (see docs/decisions/0002-master-backup-is-read-only.md)
      out of reach unless it was explicitly imported first.
    """

    def __init__(self, db: Session):
        self.db = db

    def _allowed_roots(self) -> list[Path]:
        source_paths = self.db.scalars(
            select(ImportJob.source_path).where(
                ImportJob.status == ImportStatus.COMPLETED
            )
        )

        roots = []
        for raw_path in source_paths:
            try:
                roots.append(Path(raw_path).resolve())
            except OSError:
                continue

        return roots

    def read_file(self, path: str) -> str:
        """Read a file's full extracted text content.

        Raises FileAccessError for anything that isn't a clean read: the
        path resolves outside every allowed root, doesn't exist, isn't a
        regular file, or can't be decoded as text.
        """
        try:
            resolved = Path(path).resolve()
        except OSError as exc:
            raise FileAccessError(f"Invalid path: {path}") from exc

        allowed_roots = self._allowed_roots()

        if not any(resolved.is_relative_to(root) for root in allowed_roots):
            raise FileAccessError(
                f"Refusing to read '{path}': it is not inside any "
                "completed import job's source directory. File tools can "
                "only read paths AI_Brain has already ingested from."
            )

        if not resolved.is_file():
            raise FileAccessError(f"'{path}' does not exist or is not a file.")

        try:
            content = extract_text(resolved)
        except (UnicodeDecodeError, OSError) as exc:
            raise FileAccessError(f"Could not read '{path}' as text: {exc}") from exc

        if len(content) > MAX_FILE_READ_LENGTH:
            return content[:MAX_FILE_READ_LENGTH] + "\n... [truncated]"

        return content
