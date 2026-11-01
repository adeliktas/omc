"""One bounded, verbatim attachment policy for host collection and backend retention."""

import hashlib
import re

from app.core.evidence_redaction import (
    evidence_name_valid,
    evidence_paths_conflict,
    redact_evidence,
)

MAX_EVIDENCE_FILES, MAX_EVIDENCE_BYTES = 16, 64 * 1024
EVIDENCE_MANIFEST = "_identical-to-repository.list"
EVIDENCE_HEAD = "\n\n## Files the worker left under /tmp (attached verbatim by the host)\n"


def evidence_appendix(files: dict[str, str] | None, *, attached: set[str] | None = None) -> str:
    """Render whole safe UTF-8 files, including framing and omission diagnostics in the cap.

    Keep runner priority (cited files first), with the repository-copy manifest
    ahead of everything. Never truncate contents or infer attachment from prose.
    """
    if attached is not None:
        attached.clear()
    if not files or evidence_paths_conflict(files):
        return ""
    names = sorted(files, key=lambda name: name != EVIDENCE_MANIFEST)
    blocks: dict[str, str] = {}
    skipped: dict[str, str] = {}
    omissions: dict[str, str] = {}
    budget = MAX_EVIDENCE_BYTES - len(EVIDENCE_HEAD.encode("utf-8"))
    for name in names[:MAX_EVIDENCE_FILES]:
        text = files[name] if isinstance(files[name], str) else ""
        valid = evidence_name_valid(name)
        label = f"/tmp/{name}" if valid else "(invalid name)"
        raw = text.encode("utf-8", "surrogatepass")
        digest = hashlib.sha256(raw).hexdigest()
        prefix = f"- {label}: {len(raw)} bytes, sha256 {digest} — not attached"
        omissions[name] = f"{prefix} (over the 64 KiB evidence budget)"
        try:
            data = text.encode("utf-8")
        except UnicodeEncodeError:
            data = b""
        reason = (
            "invalid file name"
            if not valid
            else (
                "not valid UTF-8"
                if not data and text
                else "needs credential redaction" if redact_evidence(text) != text else ""
            )
        )
        if reason:
            skipped[name] = f"{prefix} ({reason})"
            continue
        fence = "`" * max(3, max((len(run) for run in re.findall(r"`+", text)), default=0) + 1)
        block = f"\n### {label} — {len(data)} bytes, sha256 {digest}\n{fence}\n{text}\n{fence}\n"
        size = len(block.encode("utf-8"))
        if size > budget:
            skipped[name] = omissions[name]
            continue
        blocks[name] = block
        budget -= size

    overflow = (
        [f"- {len(names) - MAX_EVIDENCE_FILES} more file(s) not attached (over the file limit)"]
        if len(names) > MAX_EVIDENCE_FILES
        else []
    )

    def render() -> str:
        notes = [skipped[name] for name in names if name in skipped] + overflow
        return (
            EVIDENCE_HEAD
            + "".join(blocks.values())
            + ("\nNot attached:\n" + "\n".join(notes) + "\n" if notes else "")
        )

    appendix = render()
    # Pay for actual omission lines, not a separate fixed reserve. If they push
    # the packet over the cap, evict the lowest-priority whole attachment.
    while len(appendix.encode("utf-8")) > MAX_EVIDENCE_BYTES and blocks:
        name, _ = blocks.popitem()
        skipped[name] = omissions[name]
        appendix = render()
    # Pieces are vetted individually; their combination must also be safe.
    if redact_evidence(appendix) != appendix or len(appendix.encode("utf-8")) > MAX_EVIDENCE_BYTES:
        return ""
    if attached is not None:
        attached.update(blocks)
    return appendix
