"""Where baseline evidence physically lives.

Two backends, one record format, identical semantics. The scorer never touches
a file or an object; it asks a backend for ``(case_id, label, text)`` triples
and gets the same thing either way.

LOCAL is the default and stays the default. The store travels with the
checkout, so a developer running the gate needs no credential and no network,
and every unit test is hermetic. It is also how the format is documented.

GCS is the intended production home, for one reason: on the local backend
something has to *commit* the file, and the postsubmit that measures the
evidence has no push credential. Every way of giving it one -- a bot with write
access to ``main``, a pull request per merge, a weekly batched pull request --
was worse than the problem. See ``docs/designs/eval-baseline-storage.md``.

The GCS layout is one immutable object per batch::

    gs://<bucket>/<prefix>/<case-id>/<recorded_at>-<build-id>.jsonl

never appended to, because the grant this is built for is
``roles/storage.objectCreator`` -- create yes, overwrite and delete no. That
makes append-only an IAM guarantee rather than a convention, which is strictly
stronger than git, where a force-push can rewrite history. Object names begin
with an ISO-8601 UTC stamp so lexical order is chronological and the reader
gets newest-first for free.

``gcloud storage`` is shelled out to rather than importing
``google-cloud-storage``. The bench package has no GCP dependency today and
this is not worth acquiring one for; ``gcloud`` is already present wherever
this runs.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: How many of a case's newest objects a GCS read will pull.
#:
#: 200 objects is roughly 600 runs at three repetitions -- two orders of
#: magnitude past the twenty the admission bar wants -- so this never binds in
#: practice. It is here to bound a read that would otherwise grow without limit
#: as a case accumulates years of history. When it does bind, the reader says
#: so: a cap that is silent reads as "I considered everything" when it did not.
DEFAULT_MAX_OBJECTS = 200

#: Seconds before a `gcloud storage` call is treated as unreachable.
DEFAULT_TIMEOUT = 60


class StoreUnreachable(RuntimeError):
    """The store could not be reached at all.

    Deliberately distinct from a parse error, and the two are handled
    differently by the gate. Bytes that arrived and will not parse are a
    corrupt store and stop the job; a store that cannot be reached degrades to
    advisory with a loud banner, because a network blip redding every pull
    request is the failure mode that gets gates switched off.
    """


@dataclass(frozen=True)
class EvidenceSource:
    """One case's raw lines, and something to name it by in an error."""

    case_id: str
    label: str
    text: str


def is_gcs(location: str | Path) -> bool:
    return str(location).startswith("gs://")


def max_objects_from_env() -> int:
    """``EVAL_BASELINE_MAX_OBJECTS``, or the default.

    A junk or non-positive value falls back rather than raising. This bounds a
    read; it is not a correctness knob, and a typo in it must not be the reason
    a merge to main cannot be graded.
    """
    raw = os.environ.get("EVAL_BASELINE_MAX_OBJECTS", "")
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_OBJECTS
    return value if value > 0 else DEFAULT_MAX_OBJECTS


def open_backend(location: str | Path) -> LocalBackend | GcsBackend:
    """Pick a backend from the location string. ``gs://`` means GCS."""
    if is_gcs(location):
        return GcsBackend(str(location), max_objects=max_objects_from_env())
    return LocalBackend(location)


class LocalBackend:
    """``<directory>/<case-id>.jsonl``, appended to in place."""

    def __init__(self, directory: str | Path):
        self.root = Path(directory)

    def describe(self) -> str:
        return str(self.root)

    def sources(self) -> list[EvidenceSource]:
        """Every ``<case>.jsonl`` in the directory.

        A missing directory is an empty store, not an error: that is the state
        a fresh checkout is in before anything has been screened.
        """
        if not self.root.is_dir():
            return []

        # A leftover `<case>.json` is refused rather than ignored. Skipping it
        # would read as "this case has never been screened", which silently
        # de-admits the case instead of saying the format changed.
        for stray in sorted(self.root.glob("*.json")):
            if stray.name != "VERSIONS.json":
                raise ValueError(
                    f"{stray}: the store is JSONL now; rename it to "
                    f"{stray.stem}.jsonl, one record per line"
                )

        found: list[EvidenceSource] = []
        for path in sorted(self.root.glob("*.jsonl")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise ValueError(f"{path}: cannot read: {exc}") from exc
            found.append(EvidenceSource(path.stem, str(path), text))
        return found

    def append(self, case_id: str, line: str) -> str:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{case_id}.jsonl"

        # A file whose last line has no newline would swallow the next append
        # into it, turning two records into one unparseable one. Cheap to
        # prevent, and the only way it happens -- a half-written append -- is
        # exactly the case where nobody is watching.
        prefix = ""
        if path.exists() and path.stat().st_size:
            with path.open("rb") as fh:
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
                    prefix = "\n"

        with path.open("a", encoding="utf-8") as fh:
            fh.write(prefix + line + "\n")
        return str(path)


class GcsBackend:
    """One immutable object per batch under ``gs://<bucket>/<prefix>/<case>/``."""

    def __init__(
        self,
        location: str,
        *,
        max_objects: int = DEFAULT_MAX_OBJECTS,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.location = location.rstrip("/")
        self.max_objects = max_objects
        self.timeout = timeout
        self.truncated: dict[str, int] = {}

    def describe(self) -> str:
        return self.location

    def _run(self, args: list[str], stdin: str | None = None) -> str:
        try:
            done = subprocess.run(
                ["gcloud", "storage", *args],
                input=stdin,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except FileNotFoundError as exc:  # no gcloud on PATH at all
            raise StoreUnreachable(f"gcloud not available: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise StoreUnreachable(
                f"gcloud storage {args[0]} timed out after {self.timeout}s"
            ) from exc
        if done.returncode != 0:
            raise StoreUnreachable(
                f"gcloud storage {args[0]} failed ({done.returncode}): "
                f"{(done.stderr or '').strip()[:400]}"
            )
        return done.stdout

    def _list(self) -> list[str]:
        """Every object URL under the prefix, or [] if the prefix is empty.

        An empty prefix is an empty store. gcloud reports "matched no objects"
        as a non-zero exit, which must not be mistaken for the bucket being
        unreachable -- one is the ordinary state before anything is recorded,
        the other disarms the gate.
        """
        try:
            out = self._run(["ls", f"{self.location}/**"])
        except StoreUnreachable as exc:
            if "matched no objects" in str(exc).lower():
                return []
            raise
        return [
            line.strip()
            for line in out.splitlines()
            if line.strip().startswith("gs://") and line.strip().endswith(".jsonl")
        ]

    def sources(self) -> list[EvidenceSource]:
        by_case: dict[str, list[str]] = {}
        for url in self._list():
            # gs://bucket/prefix/<case>/<stamp>-<build>.jsonl
            parts = url.rsplit("/", 2)
            if len(parts) != 3:
                continue
            by_case.setdefault(parts[1], []).append(url)

        found: list[EvidenceSource] = []
        for case_id, urls in sorted(by_case.items()):
            urls.sort()  # names start with an ISO stamp, so this is chronological
            if len(urls) > self.max_objects:
                self.truncated[case_id] = len(urls) - self.max_objects
                urls = urls[-self.max_objects :]
            text = self._run(["cat", *urls])
            found.append(
                EvidenceSource(case_id, f"{self.location}/{case_id}/", text)
            )
        return found

    def append(self, case_id: str, line: str) -> str:
        """Write one new object. Never overwrites, by construction and by IAM.

        The name is taken from the record's own ``recorded_at`` so the object
        sorts into place chronologically, with the build id to keep two batches
        in the same second from colliding.
        """
        import json

        stamp = "unknown"
        try:
            stamp = str(json.loads(line).get("recorded_at") or "unknown")
        except ValueError:
            pass
        build = os.environ.get("BUILD_ID") or os.environ.get("PROW_JOB_ID") or "local"
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in f"{stamp}-{build}")
        url = f"{self.location}/{case_id}/{safe}.jsonl"
        self._run(["cp", "-", url], stdin=line + "\n")
        return url
