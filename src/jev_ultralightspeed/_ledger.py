"""
An append-only sidecar, so a job that dies at row 800,000 does not start over.

The index in memory holds a fixed-size digest and a file offset per answered
item, never the item itself. A million rows of 20,000 characters would cost
more than the machine has; a million digests and offsets cost about 100MB, and
the answer is read back off disk when it is wanted, which for a resumed run is
once per item.

The file is plain JSON lines and can be read with anything. The first line says
what it is. A record is written the moment its request lands, so a crash loses
at most whatever the operating system still had in its buffer.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass, field

FORMAT = 3                        # 2 added the pack depth to the key, 3 the question's shape
DIGEST_BYTES = 16                 # 128 bits of a sha256, which is plenty for a run of a million
FLUSH_EVERY = 256                 # answers, so a hard kill costs at most this many re-asks
_FAST_PREFIX = b'{"k":"'          # every record we write starts this way


@dataclass
class _Record:
    """One answer as it sits on disk, without the item text."""

    p: float
    label: str
    kind: str
    distribution: dict = field(default_factory=dict)
    confidence: float | None = None
    position: int = 1
    packed: int = 1


class NotACheckpoint(ValueError):
    """The path exists but holds something else. Better than appending to it."""


def digest_of(key: tuple) -> bytes:
    """A short, stable digest of the cache key: model, text, question, criteria."""
    material = json.dumps(list(key), sort_keys=False, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(material).digest()[:DIGEST_BYTES]


class Ledger:
    """Answers already on disk, and somewhere to put the new ones."""

    def __init__(self, path: str | os.PathLike, model: str) -> None:
        self.path = os.fspath(path)
        self.model = model
        self._at: dict[bytes, int] = {}
        self._lock = threading.Lock()
        self._reader = None
        self.replayed = 0                # records found when the file was opened
        self._unflushed = 0
        self._load()
        self._writer = open(self.path, "ab", buffering=1 << 16)

    # -- reading what is already there -------------------------------------
    def _load(self) -> None:
        if not os.path.exists(self.path) or os.path.getsize(self.path) == 0:
            with open(self.path, "wb") as handle:
                header = {"jev_checkpoint": FORMAT, "model": self.model}
                handle.write(json.dumps(header).encode("utf-8") + b"\n")
            return

        with open(self.path, "rb") as handle:
            first = handle.readline()
            try:
                header = json.loads(first)
            except ValueError:
                header = None
            if not isinstance(header, dict) or "jev_checkpoint" not in header:
                raise NotACheckpoint(f"{self.path} is not a jev checkpoint file; "
                                     f"point --checkpoint somewhere else")
            if header.get("jev_checkpoint") != FORMAT:
                raise NotACheckpoint(
                    f"{self.path} is checkpoint format {header.get('jev_checkpoint')} and this "
                    f"version writes {FORMAT}. Earlier formats keyed answers without the pack "
                    f"depth or the shape of the question, so "
                    f"reusing it could serve an answer from one depth for another. Delete it and "
                    f"start again, or keep it and point somewhere new.")
            offset = handle.tell()
            for line in handle:
                found = _key_in(line)
                if found is not None:
                    self._at[found] = offset       # a later line wins, which is what re-asking means
                offset += len(line)

        # A crash can leave the last line half written. One newline repairs it,
        # and the half line is ignored by everything that reads the file.
        if os.path.getsize(self.path) and not _ends_with_newline(self.path):
            with open(self.path, "ab") as handle:
                handle.write(b"\n")
        self.replayed = len(self._at)

    def answer_for(self, key: tuple, text: str):
        """The stored answer for this key, against the caller's own text."""
        offset = self._at.get(digest_of(key))
        if offset is None:
            return None
        with self._lock:
            self._writer.flush()                  # a record written a moment ago is readable
            if self._reader is None:
                self._reader = open(self.path, "rb")
            self._reader.seek(offset)
            line = self._reader.readline()
        try:
            stored = json.loads(line)
        except ValueError:
            return None                           # a torn line is simply asked again
        return _Record(p=float(stored["p"]), label=str(stored["l"]), kind=str(stored["t"]),
                       distribution=dict(stored.get("d") or {}),
                       confidence=stored.get("c"),
                       position=int(stored.get("i", 1)), packed=int(stored.get("n", 1)))

    # -- writing -----------------------------------------------------------
    def record(self, key: tuple, answer) -> None:
        """
        Append one answer. Called from whichever thread the request landed on.

        Flushed every so often rather than every time: a flush per answer costs
        real throughput at a million rows, and a hard kill between flushes loses
        at most a couple of hundred answers, which are simply asked again.
        """
        found = digest_of(key)
        line = json.dumps({"k": found.hex(), "p": answer.p, "l": answer.label,
                           "t": answer.kind, "d": answer.distribution,
                           "c": answer.confidence, "i": answer.position, "n": answer.packed},
                          separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        with self._lock:
            self._at[found] = self._writer.tell()
            self._writer.write(line + b"\n")
            self._unflushed += 1
            if self._unflushed >= FLUSH_EVERY:
                self._writer.flush()
                self._unflushed = 0

    def flush(self) -> None:
        with self._lock:
            self._writer.flush()
            self._unflushed = 0

    def close(self) -> None:
        with self._lock:
            self._writer.close()
            if self._reader is not None:
                self._reader.close()
                self._reader = None

    def __len__(self) -> int:
        return len(self._at)


def _key_in(line: bytes) -> bytes | None:
    """
    The digest out of a record, without parsing the rest of it. A million
    `json.loads` calls to read one field each is the difference between a
    resume that starts in a second and one that starts in half a minute.
    """
    if line.startswith(_FAST_PREFIX):
        raw = line[len(_FAST_PREFIX):len(_FAST_PREFIX) + DIGEST_BYTES * 2]
        # The length is checked, or a line torn mid-digest gives a short one
        # that looks like a perfectly good key for an item nobody asked about.
        if len(raw) == DIGEST_BYTES * 2:
            try:
                return bytes.fromhex(raw.decode("ascii"))
            except (ValueError, UnicodeDecodeError):
                pass
    if not line.strip():
        return None
    try:                                          # written by something else, or by hand
        return bytes.fromhex(json.loads(line)["k"])
    except (ValueError, KeyError, TypeError):
        return None                               # including the half line a crash leaves


def _ends_with_newline(path: str) -> bool:
    with open(path, "rb") as handle:
        handle.seek(-1, os.SEEK_END)
        return handle.read(1) == b"\n"
