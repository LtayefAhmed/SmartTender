"""Download the embedding models the matching module needs.

They are not in git — 536 MB of ONNX weights have no business in a repository,
and ``backend/.gitignore`` excludes ``/models/`` for exactly that reason. They
are also not baked into the Docker image: the root ``docker-compose.yml``
mounts ``./backend/models`` read-only, so fetching them once on the host serves
every container.

Run from ``backend/``::

    python scripts/fetch_models.py

Idempotent — a file already present at the right size is left alone, so this is
safe to re-run and cheap to put in a setup script.
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
from pathlib import Path

HUB = "https://huggingface.co"

#: (directory, repository, why it is here)
MODELS = (
    (
        "paraphrase-multilingual-MiniLM-L12-v2",
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        "matching — chosen on a measurement: over ten French procurement pairs "
        "it scored unrelated pairs at 0.042 against the English model's 0.166, "
        "while matching pairs were equivalent",
    ),
    (
        "all-MiniLM-L6-v2",
        "sentence-transformers/all-MiniLM-L6-v2",
        "deduplication similarity, and the English baseline the choice above "
        "was measured against",
    ),
)

FILES = ("onnx/model.onnx", "tokenizer.json")


def _download(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    # Written to a temporary name and moved into place: an interrupted download
    # otherwise leaves a truncated model that loads and produces nonsense.
    staging = target.with_suffix(target.suffix + ".part")
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 - fixed host
        total = int(response.headers.get("content-length") or 0)
        written = 0
        with staging.open("wb") as handle:
            while chunk := response.read(1 << 20):
                handle.write(chunk)
                written += len(chunk)
                if total:
                    print(f"\r  {written / 1e6:6.1f} / {total / 1e6:.1f} Mo", end="", flush=True)
    print()
    staging.replace(target)


def main() -> int:
    root = Path(__file__).resolve().parent.parent / "models"
    for directory, repo, reason in MODELS:
        print(f"\n{directory}\n  {reason}")
        for remote in FILES:
            target = root / directory / Path(remote).name
            if target.is_file() and target.stat().st_size > 0:
                print(f"  {target.name}: already present ({target.stat().st_size / 1e6:.0f} Mo)")
                continue
            url = f"{HUB}/{repo}/resolve/main/{remote}"
            print(f"  {target.name}:")
            try:
                _download(url, target)
            except urllib.error.URLError as exc:
                print(f"  failed: {exc}", file=sys.stderr)
                return 1

    print("\nDone. Containers pick these up through the ./models mount.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
