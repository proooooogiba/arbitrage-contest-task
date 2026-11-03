"""Optional trusted worker-side bridge for CodaBench ingestion.

Only the organizer calls this API. Participant code runs in a separate Docker
container. Dataset/config are fixed at service startup, never supplied by callers.
"""

import argparse
import hmac
import io
import os
import shutil
import stat
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from .cli import evaluate
from .models import Config

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_UNPACKED_BYTES = 1024**3


def extract_submission(data: bytes, destination: Path):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        items = archive.infolist()
        if len(items) > 10000 or sum(i.file_size for i in items) > MAX_UNPACKED_BYTES:
            raise ValueError("archive exceeds submission limits")
        seen = set()
        for item in items:
            path = PurePosixPath(item.filename)
            mode = item.external_attr >> 16
            if (
                not path.parts
                or path.is_absolute()
                or ".." in path.parts
                or "\\" in item.filename
                or "\x00" in item.filename
                or stat.S_ISLNK(mode)
                or str(path) in seen
            ):
                raise ValueError("unsafe or duplicate archive path")
            seen.add(str(path))
            target = destination.joinpath(*path.parts)
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=65536)
    if not (destination / "strategy.py").is_file():
        raise ValueError("strategy.py must be at the archive root")


def create_app(
    config: Path,
    dataset: Path,
    artifacts: Path,
    token: str,
    image: str = "spot-contest-strategy:0.1.0",
    runtime: str = "",
):
    import asyncio

    from fastapi import Depends, FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
    from starlette.concurrency import run_in_threadpool

    app = FastAPI(title="Trusted Contest Judge", docs_url=None, redoc_url=None, openapi_url=None)
    bearer = HTTPBearer(auto_error=False)
    gate = asyncio.Lock()

    def authorize(credentials: HTTPAuthorizationCredentials = Depends(bearer)):
        if (
            credentials is None
            or credentials.scheme.lower() != "bearer"
            or not hmac.compare_digest(credentials.credentials.encode(), token.encode())
        ):
            raise HTTPException(401, "unauthorized")

    @app.post("/evaluate", dependencies=[Depends(authorize)])
    async def evaluate_upload(request: Request):
        if gate.locked():
            raise HTTPException(429, "worker is busy")
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_ARCHIVE_BYTES:
                raise HTTPException(413, "archive exceeds 64 MiB")
        if not data:
            raise HTTPException(400, "empty archive")
        if gate.locked():
            raise HTTPException(429, "worker is busy")
        job_id = uuid.uuid4().hex

        def run_job():
            with tempfile.TemporaryDirectory(prefix="spot-upload-") as temp:
                root = Path(temp)
                extract_submission(bytes(data), root)
                evaluate(config, dataset, root, artifacts / job_id, "docker", image, runtime)
            import json

            return json.loads((artifacts / job_id / "scores.json").read_text())

        # CPU slots are serialized; the ASGI event loop remains responsive.
        async with gate:
            try:
                scores = await run_in_threadpool(run_job)
                return {"job_id": job_id, "scores": scores}
            except Exception:
                return JSONResponse(
                    status_code=422,
                    content={
                        "job_id": job_id,
                        "error": "evaluation failed; organizer can inspect this job",
                    },
                )

    return app


def main():
    parser = argparse.ArgumentParser(
        description="Trusted CodaBench bridge; never expose to participants"
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--image", default="spot-contest-strategy:0.1.0")
    parser.add_argument("--runtime", default="")
    args = parser.parse_args()
    token = os.environ.get("SPOT_JUDGE_TOKEN", "")
    if len(token) < 32 or not token.isascii():
        parser.error("SPOT_JUDGE_TOKEN must be an ASCII secret of at least 32 characters")
    Config.load(args.config)
    if not args.data.is_file():
        parser.error("data file does not exist")
    args.artifacts.mkdir(parents=True, exist_ok=True)
    app = create_app(
        args.config.resolve(),
        args.data.resolve(),
        args.artifacts.resolve(),
        token,
        args.image,
        args.runtime,
    )
    import uvicorn

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        workers=1,
        limit_concurrency=4,
        timeout_keep_alive=5,
        access_log=False,
    )


if __name__ == "__main__":
    main()
