"""Writing run artifacts, and being clear about what it means when that fails.

A transcript is evidence *about* a run. It is not the run. If the demonstration proved what it set
out to prove and the file could not be written, the demonstration still proved it — so a failure
here is reported loudly and does not change anyone's verdict.

That distinction matters more than usual in this project. Its entire subject is not mistaking a
green result for a correct one, and an exit code that contradicts the verdict printed directly above
it would be the same category of mistake.
"""

from __future__ import annotations

from pathlib import Path

REMEDY = (
    "The output directory is mounted from the host, and this container runs as a non-root user. "
    "If Docker created the directory it will be owned by root; create it first with "
    "`install -d -m 0777 artifacts` and run again. The run itself is unaffected."
)


def persist(path: Path, text: str) -> str | None:
    """Write ``text`` to ``path``. Returns ``None`` on success, or a message explaining the failure.

    Never raises, and never decides anything about whether the run passed.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    except OSError as exc:
        return f"could not write {path}: {exc}\n{REMEDY}"
    return None
