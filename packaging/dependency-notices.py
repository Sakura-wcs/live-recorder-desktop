"""Collect upstream notices shipped with dependency wheels for the Windows release."""

import shutil
import sys
from importlib.metadata import distributions
from pathlib import Path


def main(destination: Path) -> None:
    destination.mkdir()
    versions = []
    for dist in sorted(distributions(), key=lambda item: item.metadata["Name"].casefold()):
        name = dist.metadata["Name"]
        if name == "liverecorder-desktop":
            continue
        versions.append(f"{name} {dist.version}")
        for entry in dist.files or ():
            if not any(part.endswith(".dist-info") or part == "LICENSES" for part in entry.parts):
                continue
            if not any(part.upper().startswith(("LICENSE", "COPYING", "NOTICE", "COPYRIGHT")) for part in entry.parts):
                continue
            source = dist.locate_file(entry)
            if source.is_file():
                target = destination / f"{name}-{dist.version}" / entry
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
    (destination / "versions.txt").write_text("\n".join(versions) + "\n", encoding="utf-8")
    shutil.copyfile(Path(sys.base_prefix) / "LICENSE.txt", destination / "Python-LICENSE.txt")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
