import os
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode


class StorageBackend:
    """Abstract storage backend for uploaded files."""

    async def save(self, data: bytes, path: str, content_type: str = "") -> str:
        """Persist data and return the public-facing URL."""
        raise NotImplementedError

    def get_url(self, path: str) -> str:
        """Return a URL for the given storage path (may be signed)."""
        raise NotImplementedError

    def get_presigned_url(self, path: str, ttl: int = 600) -> Optional[str]:
        """Return a time-limited signed URL, or None if not supported."""
        return None

    def mode_name(self) -> str:
        return "local"

    @staticmethod
    def _key_prefix() -> str:
        return os.environ.get("S3_KEY_PREFIX", "uploads").strip().strip("/")


class LocalStorage(StorageBackend):
    def __init__(self, upload_dir: Path) -> None:
        self.upload_dir = upload_dir

    async def save(self, data: bytes, path: str, content_type: str = "") -> str:
        file_path = self.upload_dir / path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(data)
        return f"/uploads/{path}"

    def get_url(self, path: str) -> str:
        return f"/uploads/{path}"

    def mode_name(self) -> str:
        return "local"


class S3Storage(StorageBackend):
    def __init__(
        self,
        bucket: str,
        endpoint: Optional[str] = None,
        region: Optional[str] = None,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        presigned_read: bool = True,
    ) -> None:
        import boto3

        kwargs: dict[str, str] = {}
        if endpoint:
            kwargs["endpoint_url"] = endpoint
        if region:
            kwargs["region_name"] = region
        if access_key and secret_key:
            kwargs["aws_access_key_id"] = access_key
            kwargs["aws_secret_access_key"] = secret_key
        self.client = boto3.client("s3", **kwargs)
        self.bucket = bucket
        self.presigned_read = presigned_read

    async def save(self, data: bytes, path: str, content_type: str = "") -> str:
        import asyncio

        put_kwargs = {"Bucket": self.bucket, "Key": path, "Body": data}
        if content_type:
            put_kwargs["ContentType"] = content_type
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: self.client.put_object(**put_kwargs),
        )
        if self.presigned_read:
            # Return a key-based path; URL generation happens at read time
            return self.get_url(path)
        return self.get_url(path)

    def get_url(self, path: str) -> str:
        return f"/uploads/{path}?storage=s3"

    def get_presigned_url(self, path: str, ttl: int = 600) -> Optional[str]:
        """Generate a presigned S3 URL for direct read."""
        try:
            url = self.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": path},
                ExpiresIn=ttl,
            )
            return url
        except Exception:
            return None

    def read_object(self, path: str) -> Optional[bytes]:
        """Read object bytes directly from S3 (server-side proxy)."""
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=path)
            return resp["Body"].read()
        except Exception:
            return None

    def mode_name(self) -> str:
        return "s3"


def get_storage() -> StorageBackend:
    """Factory: returns the appropriate storage backend based on environment."""
    bucket = os.environ.get("S3_BUCKET", "").strip()
    if bucket:
        return S3Storage(
            bucket=bucket,
            endpoint=os.environ.get("S3_ENDPOINT_URL") or None,
            region=os.environ.get("S3_REGION") or None,
            access_key=os.environ.get("S3_ACCESS_KEY_ID") or None,
            secret_key=os.environ.get("S3_SECRET_ACCESS_KEY") or None,
            presigned_read=os.environ.get("S3_PRESIGNED_READ", "true").lower() == "true",
        )
    upload_dir = Path(__file__).resolve().parent / "uploads"
    return LocalStorage(upload_dir)
