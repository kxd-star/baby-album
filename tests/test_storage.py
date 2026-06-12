import os
import tempfile
import unittest
from pathlib import Path

from storage import LocalStorage, S3Storage


class FakeS3Client:
    def __init__(self):
        self.calls = []

    def put_object(self, **kwargs):
        self.calls.append(("put", kwargs))

    def delete_object(self, **kwargs):
        self.calls.append(("delete", kwargs))

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        self.calls.append(("sign", {
            "operation": operation,
            "Params": Params,
            "ExpiresIn": ExpiresIn,
        }))
        return "https://storage.example/signed"

    def get_object(self, **kwargs):
        self.calls.append(("get", kwargs))
        return {"Body": FakeBody()}


class FakeBody:
    def read(self):
        return b"image"


class LocalStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_save_and_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = LocalStorage(Path(tmp))
            path = f"{'a' * 32}/photo.jpg"

            await storage.save(b"photo", path, "image/jpeg")
            self.assertEqual((Path(tmp) / path).read_bytes(), b"photo")
            self.assertTrue(await storage.delete(path))
            self.assertFalse((Path(tmp) / path).exists())

    async def test_delete_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = LocalStorage(Path(tmp))
            self.assertFalse(await storage.delete("../outside.jpg"))


class S3StorageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.previous_prefix = os.environ.get("S3_KEY_PREFIX")
        os.environ["S3_KEY_PREFIX"] = "uploads"
        self.client = FakeS3Client()
        self.storage = S3Storage.__new__(S3Storage)
        self.storage.client = self.client
        self.storage.bucket = "bucket"
        self.storage.presigned_read = True

    def tearDown(self):
        if self.previous_prefix is None:
            os.environ.pop("S3_KEY_PREFIX", None)
        else:
            os.environ["S3_KEY_PREFIX"] = self.previous_prefix

    async def test_save_read_and_delete_use_one_prefix(self):
        await self.storage.save(b"photo", "user/photo.jpg", "image/jpeg")
        self.assertEqual(self.client.calls[-1][1]["Key"], "uploads/user/photo.jpg")

        self.assertEqual(await self.storage.read_object("uploads/user/photo.jpg"), b"image")
        self.assertEqual(self.client.calls[-1][1]["Key"], "uploads/user/photo.jpg")

        self.assertTrue(await self.storage.delete("user/photo.jpg"))
        self.assertEqual(self.client.calls[-1][1]["Key"], "uploads/user/photo.jpg")

    def test_presigned_url_does_not_duplicate_prefix(self):
        self.assertEqual(
            self.storage.get_presigned_url("uploads/user/photo.jpg"),
            "https://storage.example/signed",
        )
        self.assertEqual(
            self.client.calls[-1][1]["Params"]["Key"],
            "uploads/user/photo.jpg",
        )


if __name__ == "__main__":
    unittest.main()
