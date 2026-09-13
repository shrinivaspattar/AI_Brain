from __future__ import annotations

from pathlib import Path

from app.classification.eligibility_service import IngestionEligibility
from app.models.content_identity_group import ContentPipelineState

# Shared between IdentityResolutionService and ArchiveProcessingService -
# both discover a leaf item's raw bytes (a loose file being read to hash
# it, or an archive member just extracted) and need the identical
# workspace layout / initial-pipeline-state decision once identity is
# established.


def initial_pipeline_state_for_eligibility(
    eligibility: IngestionEligibility,
) -> ContentPipelineState:
    if eligibility == IngestionEligibility.EXCLUDED:
        return ContentPipelineState.EXCLUDED
    if eligibility == IngestionEligibility.UNSUPPORTED:
        return ContentPipelineState.UNSUPPORTED
    # By the time a ContentIdentityGroup is created here, its bytes have
    # necessarily already been read (that's how the hash was computed) -
    # there is no separate EXTRACTING claim left to perform, so an
    # eligible group starts life past that step.
    return ContentPipelineState.EXTRACTED


def workspace_content_path(workspace_root: Path, group_id: int, suffix: str = "") -> Path:
    """Where a ContentIdentityGroup's raw extracted bytes live, once
    available - the extraction workspace, entirely separate from the
    (never-written-to) T7 and from durable AI_Brain knowledge (the
    Document/DocumentChunk rows themselves), per `0002`/`29d6864`.

    `suffix` (e.g. ".pdf") is preserved from whichever occurrence first
    resolved this identity, so `extract_text()`'s extension-based
    dispatch works during normalization - content identity guarantees
    the BYTES are identical regardless of which copy's extension is
    used, so any one occurrence's suffix is as good as another's.
    """
    return workspace_root / f"group_{group_id}" / f"content{suffix}"


def write_workspace_content(
    workspace_root: Path,
    group_id: int,
    content: bytes,
    *,
    suffix: str = "",
) -> Path:
    destination = workspace_content_path(workspace_root, group_id, suffix)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    return destination


def find_existing_workspace_content(workspace_root: Path, group_id: int) -> Path | None:
    """Locates an already-written workspace content file for a group,
    regardless of its suffix - used by normalization, which only knows
    the group id, not which extension the original occurrence used."""
    group_dir = workspace_root / f"group_{group_id}"
    if not group_dir.is_dir():
        return None
    matches = sorted(group_dir.glob("content*"))
    return matches[0] if matches else None
