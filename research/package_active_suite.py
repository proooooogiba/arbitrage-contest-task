"""Package the twelve active signal variants outside the source repository."""

import argparse
import hashlib
import json
import shutil
import zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    if output == root or root in output.parents:
        raise ValueError("package output must be outside the source repository")
    output.mkdir(parents=True, exist_ok=True)
    source = root / "examples" / "active_suite"
    base = json.loads((source / "parameters.json").read_text())
    variants = json.loads((source / "variants.json").read_text())
    manifest = []
    for mode, settings in variants.items():
        folder = output / "submissions" / mode
        folder.mkdir(parents=True, exist_ok=True)
        for filename in ("strategy.py", "execution.py", "model.json"):
            shutil.copy2(source / filename, folder / filename)
        parameters = dict(base, mode=mode, **settings)
        (folder / "parameters.json").write_text(json.dumps(parameters, indent=2) + "\n")
        archive = output / ("active_" + mode + "_20261115.zip")
        hashes = {}
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as stream:
            for filename in ("strategy.py", "execution.py", "model.json", "parameters.json"):
                data = (folder / filename).read_bytes()
                hashes[filename] = hashlib.sha256(data).hexdigest()
                stream.writestr(filename, data)
        manifest.append(
            dict(
                mode=mode,
                archive=str(archive),
                folder=str(folder),
                sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                files=hashes,
                parameters=parameters,
            )
        )
    (output / "submissions.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        json.dumps([dict(mode=row["mode"], archive=row["archive"]) for row in manifest], indent=2)
    )


if __name__ == "__main__":
    main()
