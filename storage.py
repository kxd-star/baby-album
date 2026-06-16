import asyncio
import os
from pathlib import Path
from typing import Optional


class StorageBackend:
    """Abstract storage backend for uploaded files."""

    async def save(self, data: bytes, path: str, content_type: str = "") -> str:
        """Persist data and return the public-facing URL."""
        raise NotImplementedError

    async def delete(self, path: str) -> bool:
        """Delete a stored object. Missing objects count as successfully deleted."""
        raise NotImplementedError

    async def read(self, path: str) -> Optional[bytes]:
        """Read stored object bytes, or return None when unavailable."""
        raise NotImplementedError

    async def count(self, prefix: str, limit: int | None = None) -> int:
        """Count stored objects below a prefix, stopping once limit is reached."""
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
        file_path = (self.upload_dir / path).resolve()
        upload_dir = self.upload_dir.resolve()
        if upload_dir not in file_path.parents:
            raise ValueError("Path traversal detected")
        await asyncio.to_thread(file_path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(file_path.write_bytes, data)
        return f"/uploads/{path}"

    async def delete(self, path: str) -> bool:
        file_path = (self.upload_dir / path).resolve()
        upload_dir = self.upload_dir.resolve()
        if upload_dir not in file_path.parents:
            return False

        def delete_file() -> bool:
            try:
                file_path.unlink(missing_ok=True)
                parent = file_path.parent
                if parent != upload_dir:
                    try:
                        parent.rmdir()
                    except OSError:
                        pass
                return True
            except OSError:
                return False

        return await asyncio.to_thread(delete_file)

    async def read(self, path: str) -> Optional[bytes]:
        file_path = (self.upload_dir / path).resolve()
        upload_dir = self.upload_dir.resolve()
        if upload_dir not in file_path.parents:
            return None
        try:
            return await asyncio.to_thread(file_path.read_bytes)
        except OSError:
            return None

    async def count(self, prefix: str, limit: int | None = None) -> int:
        directory = (self.upload_dir / prefix).resolve()
        upload_dir = self.upload_dir.resolve()
        if directory != upload_dir and upload_dir not in directory.parents:
            return 0

        def count_files() -> int:
            if not directory.exists():
                return 0
            count = 0
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.is_file():
                        count += 1
                        if limit is not None and count >= limit:
                            break
            return count

        return await asyncio.to_thread(count_files)

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
        key = self._object_key(path)
        put_kwargs = {"Bucket": self.bucket, "Key": key, "Body": data}
        if content_type:
            put_kwargs["ContentType"] = content_type
        await asyncio.to_thread(self.client.put_object, **put_kwargs)
        return self.get_url(path)

    async def delete(self, path: str) -> bool:
        try:
            await asyncio.to_thread(
                self.client.delete_object,
                Bucket=self.bucket,
                Key=self._object_key(path),
            )
            return True
        except Exception:
            return False

    async def read(self, path: str) -> Optional[bytes]:
        try:
            resp = await asyncio.to_thread(
                self.client.get_object,
                Bucket=self.bucket,
                Key=self._object_key(path),
            )
            return await asyncio.to_thread(resp["Body"].read)
        except Exception:
            return None

    async def count(self, prefix: str, limit: int | None = None) -> int:
        object_prefix = self._object_key(prefix).rstrip("/") + "/"

        def count_objects() -> int:
            count = 0
            continuation_token: str | None = None
            while True:
                kwargs = {
                    "Bucket": self.bucket,
                    "Prefix": object_prefix,
                    "MaxKeys": min(1000, max(1, (limit or 1000) - count)),
                }
                if continuation_token:
                    kwargs["ContinuationToken"] = continuation_token
                response = self.client.list_objects_v2(**kwargs)
                count += len(response.get("Contents", []))
                if limit is not None and count >= limit:
                    return count
                if not response.get("IsTruncated"):
                    return count
                continuation_token = response.get("NextContinuationToken")
                if not continuation_token:
                    return count

        return await asyncio.to_thread(count_objects)

    def get_url(self, path: str) -> str:
        return f"/uploads/{path}?storage=s3"

    def get_presigned_url(self, path: str, ttl: int = 600) -> Optional[str]:
        """Generate a presigned S3 URL for direct read."""
        try:
            url = self.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": self._object_key(path)},
                ExpiresIn=ttl,
            )
            return url
        except Exception:
            return None

    async def read_object(self, path: str) -> Optional[bytes]:
        """Backward-compatible alias for older callers."""
        return await self.read(path)

    def _object_key(self, path: str) -> str:
        clean_path = path.strip().lstrip("/")
        prefix = self._key_prefix()
        if not prefix or clean_path == prefix or clean_path.startswith(f"{prefix}/"):
            return clean_path
        return f"{prefix}/{clean_path}"

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
