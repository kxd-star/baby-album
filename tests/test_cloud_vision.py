import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from PIL import Image

import serve
from storage import LocalStorage


def make_png(size=(64, 64), noisy=False) -> bytes:
    image = Image.effect_noise(size, 100).convert("RGB") if noisy else Image.new("RGB", size, "red")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class MemoryStorage:
    def __init__(self):
        self.objects = {}

    async def save(self, data, path, content_type=""):
        self.objects[path] = data
        return f"/uploads/{path}"

    async def read(self, path):
        return self.objects.get(path)

    async def delete(self, path):
        self.objects.pop(path, None)
        return True

    async def count(self, prefix, limit=None):
        return sum(path.startswith(f"{prefix}/") for path in self.objects)

    def mode_name(self):
        return "s3"

    def get_presigned_url(self, path, ttl=600):
        return None


class PresignedMemoryStorage(MemoryStorage):
    def get_presigned_url(self, path, ttl=600):
        return "https://storage.example/signed-photo"


class CloudVisionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.previous_storage = serve._storage
        self.previous_upload_dir = serve.UPLOAD_DIR
        self.previous_call_vision = serve.call_vision_llm
        self.previous_call_llm = serve.call_llm

    def tearDown(self):
        serve._storage = self.previous_storage
        serve.UPLOAD_DIR = self.previous_upload_dir
        serve.call_vision_llm = self.previous_call_vision
        serve.call_llm = self.previous_call_llm

    async def test_photo_data_uri_reads_s3_and_checks_owner(self):
        storage = MemoryStorage()
        user_id = "a" * 32
        storage.objects[f"{user_id}/photo.png"] = make_png()
        serve._storage = storage
        own_request = SimpleNamespace(state=SimpleNamespace(user_id=user_id))
        other_request = SimpleNamespace(state=SimpleNamespace(user_id="b" * 32))
        url = f"https://album.example/uploads/{user_id}/photo.png"

        self.assertTrue((await serve._photo_to_data_uri(own_request, url)).startswith("data:image/png;base64,"))
        self.assertIsNone(await serve._photo_to_data_uri(other_request, url))

    async def test_large_image_is_downscaled_for_vision(self):
        data = make_png((1800, 1400), noisy=True)
        self.assertGreater(len(data), serve.AI_IMAGE_COMPRESS_THRESHOLD)

        prepared, content_type = serve.prepare_ai_image(data)
        with Image.open(io.BytesIO(prepared)) as image:
            self.assertLessEqual(max(image.size), serve.AI_IMAGE_MAX_SIDE)
        self.assertEqual(content_type, "image/jpeg")
        self.assertLess(len(prepared), len(data))

    async def test_minimax_and_ark_use_provider_specific_formats(self):
        data_uri = "data:image/jpeg;base64,AAAA"
        minimax = serve.build_minimax_vision_messages("prompt", [data_uri])
        ark = serve.build_ark_vision_messages("prompt", [data_uri])

        self.assertIsInstance(minimax[0]["content"], list)
        self.assertEqual(minimax[0]["content"][0]["type"], "image_url")
        self.assertEqual(minimax[0]["content"][0]["image_url"]["url"], data_uri)
        self.assertIsInstance(ark[0]["content"], list)
        self.assertEqual(ark[0]["content"][0]["type"], "image_url")
        self.assertEqual(ark[0]["content"][0]["image_url"]["url"], data_uri)


class UserIsolationTests(unittest.TestCase):
    def setUp(self):
        self.previous_storage = serve._storage
        self.previous_upload_dir = serve.UPLOAD_DIR
        self.previous_call_vision = serve.call_vision_llm
        self.previous_call_llm = serve.call_llm
        self.previous_max_user_upload_files = serve.MAX_USER_UPLOAD_FILES
        self.temp_dir = tempfile.TemporaryDirectory()
        serve.UPLOAD_DIR = Path(self.temp_dir.name)
        serve._storage = LocalStorage(serve.UPLOAD_DIR)

    def tearDown(self):
        serve._storage = self.previous_storage
        serve.UPLOAD_DIR = self.previous_upload_dir
        serve.call_vision_llm = self.previous_call_vision
        serve.call_llm = self.previous_call_llm
        serve.MAX_USER_UPLOAD_FILES = self.previous_max_user_upload_files
        self.temp_dir.cleanup()

    def test_other_user_cannot_read_or_caption_photo(self):
        calls = []

        async def fake_vision(prompt, images, **kwargs):
            calls.append((prompt, images))
            return "vision caption"

        serve.call_vision_llm = fake_vision
        owner = TestClient(serve.app)
        other = TestClient(serve.app)
        upload = owner.post(
            "/api/upload",
            files={"files": ("photo.png", make_png(), "image/png")},
            data={"photo_ids": "photo12345678"},
        )
        photo = upload.json()["photos"][0]
        path = "/" + photo["url"].split("/", 3)[3]

        self.assertEqual(owner.get(path).status_code, 200)
        self.assertEqual(other.get(path).status_code, 404)
        caption = other.post(
            "/api/caption",
            json={"id": photo["id"], "url": photo["url"], "albumType": "default"},
        )
        self.assertEqual(caption.status_code, 200)
        self.assertEqual(calls, [])

    def test_same_user_can_upload_more_than_one_album_limit(self):
        serve.MAX_USER_UPLOAD_FILES = serve.MAX_UPLOAD_FILES + 3
        client = TestClient(serve.app)

        for index in range(serve.MAX_UPLOAD_FILES):
            response = client.post(
                "/api/upload",
                files={"files": ("photo.png", make_png(), "image/png")},
                data={"photo_ids": f"quota{index:04d}"},
            )
            self.assertEqual(response.status_code, 200)

        extra = client.post(
            "/api/upload",
            files={"files": ("photo.png", make_png(), "image/png")},
            data={"photo_ids": "quotaextra"},
        )

        self.assertEqual(extra.status_code, 200)

    def test_s3_backed_photo_reaches_vision_model(self):
        calls = []

        async def fake_vision(prompt, images, **kwargs):
            calls.append(images)
            if "故事线编辑师" in prompt:
                return (
                    '{"chapters":[{"title":"chapter","description":"desc",'
                    '"photo_ids":["photo12345678"]}]}'
                )
            if "主题搭配师" in prompt:
                return '{"themeId":"classic","trackSrc":"assets/bgm.mp3"}'
            return "vision caption"

        serve._storage = MemoryStorage()
        serve.call_vision_llm = fake_vision
        client = TestClient(serve.app)
        upload = client.post(
            "/api/upload",
            files={"files": ("photo.png", make_png(), "image/png")},
            data={"photo_ids": "photo12345678"},
        )
        photo = upload.json()["photos"][0]
        caption = client.post(
            "/api/caption",
            json={"id": photo["id"], "url": photo["url"], "albumType": "default"},
        )
        storyline = client.post(
            "/api/storyline",
            json={
                "albumType": "default",
                "title": "test",
                "photos": [{"id": photo["id"], "url": photo["url"], "caption": "vision caption"}],
            },
        )
        recommendation = client.post(
            "/api/recommend",
            json={
                "albumType": "default",
                "title": "test",
                "captions": ["vision caption"],
                "photos": [{"src": photo["url"]}],
                "themes": [{"id": "classic"}],
                "tracks": [{"src": "assets/bgm.mp3"}],
            },
        )

        self.assertEqual(caption.json()["caption"], "vision caption")
        self.assertEqual(caption.json()["source"], "ai")
        self.assertEqual(caption.json()["mode"], "vision")
        self.assertEqual(storyline.json()["chapters"][0]["title"], "chapter")
        self.assertEqual(storyline.json()["source"], "ai")
        self.assertEqual(storyline.json()["mode"], "vision")
        self.assertEqual(recommendation.json()["themeId"], "classic")
        self.assertEqual(recommendation.json()["mode"], "vision")
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(call[0].startswith("data:image/png;base64,") for call in calls))

    def test_caption_reports_error_when_photo_is_not_readable(self):
        client = TestClient(serve.app)
        response = client.post(
            "/api/caption",
            json={"id": "missing", "url": "/uploads/" + "a" * 32 + "/missing.png", "albumType": "default"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["caption"], "值得珍藏的美好瞬间")
        self.assertEqual(response.json()["source"], "error")
        self.assertEqual(response.json()["mode"], "none")

    def test_caption_refuses_unrecognized_vision_reply(self):
        async def vision_refusal(*args, **kwargs):
            return "抱歉，我当前无法直接查看或解析您提供的图片内容，因此没办法"

        serve.call_vision_llm = vision_refusal
        client = TestClient(serve.app)
        upload = client.post(
            "/api/upload",
            files={"files": ("photo.png", make_png(), "image/png")},
            data={"photo_ids": "photo12345678"},
        )
        photo = upload.json()["photos"][0]
        response = client.post(
            "/api/caption",
            json={"id": photo["id"], "url": photo["url"], "albumType": "baby"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["caption"], "宝宝的温暖成长瞬间")
        self.assertEqual(response.json()["source"], "fallback")
        self.assertEqual(response.json()["mode"], "local")

    def test_storyline_reports_fallback_source(self):
        async def empty_llm(*args, **kwargs):
            return None

        serve.call_llm = empty_llm
        client = TestClient(serve.app)
        response = client.post(
            "/api/storyline",
            json={
                "albumType": "default",
                "title": "test",
                "photos": [
                    {"id": "p1", "url": "", "caption": "one"},
                    {"id": "p2", "url": "", "caption": "two"},
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["source"], "fallback")
        self.assertEqual(response.json()["mode"], "local")
        self.assertGreaterEqual(len(response.json()["chapters"]), 1)

    def test_storyline_fallback_chapter_count_strategy(self):
        def photos(count):
            return [
                serve.StorylinePhoto(id=f"p{i}", url="", caption=f"photo {i}")
                for i in range(count)
            ]

        self.assertEqual(len(serve.fallback_storyline(photos(1), "baby")), 1)
        self.assertEqual(len(serve.fallback_storyline(photos(2), "baby")), 1)
        self.assertEqual(len(serve.fallback_storyline(photos(3), "baby")), 2)
        self.assertEqual(len(serve.fallback_storyline(photos(6), "baby")), 2)
        self.assertEqual(len(serve.fallback_storyline(photos(7), "baby")), 3)
        self.assertEqual(len(serve.fallback_storyline(photos(12), "baby")), 3)
        self.assertEqual(len(serve.fallback_storyline(photos(13), "baby")), 4)

    def test_storyline_fallback_reorders_photos_by_narrative_stage(self):
        photos = [
            serve.StorylinePhoto(id="p1", url="", caption="\u5b89\u9759\u7761\u68a6"),
            serve.StorylinePhoto(id="p2", url="", caption="\u521d\u89c1\u5c0f\u5c0f\u8138"),
            serve.StorylinePhoto(id="p3", url="", caption="\u5f00\u5fc3\u707f\u70c2\u7b11"),
            serve.StorylinePhoto(id="p4", url="", caption="\u4e00\u8d77\u4e92\u52a8\u63a2\u7d22"),
        ]

        chapters = serve.fallback_storyline(photos, "baby")
        ordered_ids = [
            photo_id
            for chapter in chapters
            for photo_id in chapter["photo_ids"]
        ]

        self.assertEqual(ordered_ids, ["p2", "p4", "p3", "p1"])

    def test_recommendation_falls_back_to_valid_theme_and_track(self):
        async def empty_llm(*args, **kwargs):
            return None

        serve.call_llm = empty_llm
        client = TestClient(serve.app)
        response = client.post(
            "/api/recommend",
            json={
                "albumType": "default",
                "title": "test",
                "captions": [],
                "photos": [],
                "themes": [{"id": "classic"}, {"id": "editorial"}],
                "tracks": [{"src": "assets/bgm.mp3"}, {"src": "assets/music/lullaby.mp3"}],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "themeId": "editorial",
            "trackSrc": "assets/music/lullaby.mp3",
            "source": "fallback",
            "mode": "local",
        })

    def test_invalid_ai_recommendation_is_replaced(self):
        async def invalid_llm(*args, **kwargs):
            return '{"themeId":"missing","trackSrc":"missing.mp3"}'

        serve.call_llm = invalid_llm
        client = TestClient(serve.app)
        response = client.post(
            "/api/recommend",
            json={
                "albumType": "baby",
                "title": "test",
                "captions": [],
                "photos": [],
                "themes": [{"id": "classic"}, {"id": "moonlight-baby"}],
                "tracks": [{"src": "assets/bgm.mp3"}, {"src": "assets/music/lullaby.mp3"}],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "themeId": "moonlight-baby",
            "trackSrc": "assets/music/lullaby.mp3",
            "source": "fallback",
            "mode": "local",
        })

    def test_cloud_bundle_contains_playable_music_and_demo_image(self):
        client = TestClient(serve.app)
        image = client.get("/assets/opt/photo_1.webp")

        for path in (
            "/assets/bgm.mp3",
            "/assets/bgm_2.mp3",
            "/assets/bgm_3.mp3",
            "/assets/music/cinematic.mp3",
            "/assets/music/lullaby.mp3",
            "/assets/music/travel_upbeat.mp3",
        ):
            music = client.get(path)
            self.assertEqual(music.status_code, 200)
            self.assertGreater(len(music.content), 100_000)
            self.assertNotIn(b"<Error>", music.content[:500])
            self.assertNotIn(b"<!DOCTYPE html", music.content[:500])
        self.assertEqual(image.status_code, 200)
        self.assertTrue(image.content.startswith(b"RIFF"))

    def test_s3_photo_proxy_avoids_cross_origin_redirect(self):
        serve._storage = PresignedMemoryStorage()
        client = TestClient(serve.app, follow_redirects=False)
        upload = client.post(
            "/api/upload",
            files={"files": ("photo.png", make_png(), "image/png")},
            data={"photo_ids": "photo12345678"},
        )
        path = "/" + upload.json()["photos"][0]["url"].split("/", 3)[3]

        self.assertEqual(client.get(path).status_code, 307)
        proxied = client.get(f"{path}?proxy=1")
        self.assertEqual(proxied.status_code, 200)
        self.assertTrue(proxied.content.startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
