"""Errors the archive readers raise for files that are cut short or malformed.

Every reader in this package (TWRF in ``twrf``, TWTI/TWTZ in ``twti``, TWVL
in ``twvl``, and ``inspect``, which reuses their checks) refuses a file that
is cut short or does not follow its spec (TWS-1 section 8.2, TWS-2 section
9.2, TWS-3 section 6.2) rather than hand back short or misplaced data as if
it were the tape. The inputs are the user's own captures and images, so these
guard against accidents (an unfinished copy, a stray edit), not against
deliberately crafted files; see TWS-2 section 11. Both errors are ``ValueError`` subclasses, so the CLIs'
existing ``except ValueError`` turns them into a one-line error and exit 1
(STYLE.md section 2.5); callers that care which it was can catch these.

The message always names the file, what was expected and what was found,
because the usual cause is a copy that did not finish -- and the fix (copy
or convert it again) depends on knowing which file.
"""

from __future__ import annotations

from os import PathLike


class MalformedFileError(ValueError):
    """A TWRF, TWTI, TWTZ or TWVL file that breaks its format's rules."""

    def __init__(
        self, path: str | PathLike[str], kind: str, problem: str, *, verdict: str = "malformed"
    ) -> None:
        self.path = str(path)
        self.kind = kind
        self.problem = problem
        super().__init__(f"{self.path}: {kind} file is {verdict}: {problem}")


class TruncatedFileError(MalformedFileError):
    """A file shorter than its own preamble, header or tables say it must be.

    ``expected`` is the length in bytes the file needs to hold ``part``;
    ``found`` is the length it has. For a TWTZ, both count decompressed bytes.
    A sparse file's length includes its holes, so a sparse image whose length
    is right is *not* truncated, however little of it is allocated.
    """

    def __init__(
        self,
        path: str | PathLike[str],
        kind: str,
        part: str,
        expected: int | None = None,
        found: int | None = None,
        *,
        note: str = "",
    ) -> None:
        self.part = part
        self.expected = expected
        self.found = found
        # A zstd stream that just stops gives no expected length, only where
        # it stopped; the caller then says so in ``note``.
        sizes = (
            f": needs at least {expected:,} bytes, but has {found:,}"
            if expected is not None and found is not None
            else ""
        )
        problem = (
            f"the {part} is cut short{sizes}{note}. The file was probably left "
            "incomplete by an unfinished copy or write; copy or convert it again."
        )
        super().__init__(path, kind, problem, verdict="truncated")
