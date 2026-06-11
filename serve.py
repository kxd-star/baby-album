import asyncio
import hashlib
import hmac
import json
import mimetypes
import os
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
import uvicorn
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from starlette.responses import FileResponse

try:
    import boto3
except ImportError:
    boto3 = None


app = FastAPI()
ROOT_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = ROOT_DIR / "uploads"
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(8 * 1024 * 1024)))
MAX_UPLOAD_FILES = int(os.environ.get("MAX_UPLOAD_FILES", "30"))
MAX_REQUEST_BODY_BYTES = int(os.environ.get("MAX_REQUEST_BODY_BYTES", str(32 * 1024 * 1024)))
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
CAPTION_CONCURRENCY = int(os.environ.get("CAPTION_CONCURRENCY", "4"))
APP_ENV = os.environ.get("APP_ENV", "development").strip().lower()
SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-only-change-me").encode("utf-8")
SESSION_COOKIE_NAME = os.environ.get("SESSION_COOKIE_NAME", "baby_album_session")
SESSION_MAX_AGE = int(os.environ.get("SESSION_MAX_AGE", str(60 * 60 * 24 * 365)))
SIGNED_ASSET_TTL = int(os.environ.get("SIGNED_ASSET_TTL", "600"))
SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "true").lower() == "true"
SESSION_COOKIE_SAMESITE = os.environ.get("SESSION_COOKIE_SAMESITE", "lax").lower()
S3_PRESIGNED_READ = os.environ.get("S3_PRESIGNED_READ", "true").lower() == "true"

allowed_origins = [
    origin.strip()
    for origin in os.environ.get("ALLOWED_ORIGINS", "*").split(",")
    if origin.strip()
]

INSECURE_SESSION_SECRETS = {
    b"",
    b"change-me",
    b"dev-only-change-me",
    b"replace-with-a-long-random-secret",
}
SESSION_SECRET_CONFIGURED = (
    SESSION_SECRET not in INSECURE_SESSION_SECRETS
    and len(SESSION_SECRET) >= 32
)
if APP_ENV == "production" and not SESSION_SECRET_CONFIGURED:
    raise RuntimeError(
        "SESSION_SECRET must be a non-placeholder value with at least 32 characters in production"
    )
if APP_ENV == "production" and not SESSION_COOKIE_SECURE:
    raise RuntimeError("SESSION_COOKIE_SECURE must be true in production")

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins or ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials="*" not in allowed_origins,
)


class CaptionPhoto(BaseModel):
    id: str | None = None
    url: str
    caption: str | None = None


class CaptionRequest(BaseModel):
    id: str | None = None
    url: str
    albumType: str | None = "default"


class CaptionsRequest(BaseModel):
    photos: list[CaptionPhoto]
    albumType: str | None = "default"
    title: str | None = ""


class StorylinePhoto(BaseModel):
    id: str | None = None
    url: str | None = None
    caption: str | None = None


class StorylineRequest(BaseModel):
    albumType: str | None = "default"
    title: str | None = ""
    photos: list[StorylinePhoto]


class RecommendRequest(BaseModel):
    albumType: str | None = "default"
    title: str | None = Field(default="", max_length=200)
    captions: list[str] = Field(default_factory=list, max_length=30)
    photos: list[dict[str, Any]] = Field(default_factory=list, max_length=30)
    themes: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    tracks: list[dict[str, Any]] = Field(default_factory=list, max_length=50)


def sign_value(value: str) -> str:
    return hmac.new(SESSION_SECRET, value.encode("utf-8"), hashlib.sha256).hexdigest()


def create_session_token(user_id: str) -> str:
    expires = int(time.time()) + SESSION_MAX_AGE
    payload = f"{user_id}.{expires}"
    return f"{payload}.{sign_value(payload)}"


def verify_session_token(token: str | None) -> str | None:
    if not token:
        return None
    try:
        user_id, expires_text, signature = token.split(".", 2)
        payload = f"{user_id}.{expires_text}"
        if int(expires_text) < int(time.time()):
            return None
        if not hmac.compare_digest(signature, sign_value(payload)):
            return None
        if len(user_id) != 32 or not all(char in "0123456789abcdef" for char in user_id):
            return None
        return user_id
    except (TypeError, ValueError):
        return None


@app.middleware("http")
async def request_body_limit_middleware(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_REQUEST_BODY_BYTES:
                return JSONResponse({"detail": "Request body is too large"}, status_code=413)
        except ValueError:
            return JSONResponse({"detail": "Invalid Content-Length header"}, status_code=400)
    return await call_next(request)


@app.middleware("http")
async def user_session_middleware(request: Request, call_next):
    user_id = verify_session_token(request.cookies.get(SESSION_COOKIE_NAME))
    is_new_session = user_id is None
    if is_new_session:
        user_id = uuid.uuid4().hex
    request.state.user_id = user_id

    response = await call_next(request)
    if is_new_session:
        response.set_cookie(
            SESSION_COOKIE_NAME,
            create_session_token(user_id),
            max_age=SESSION_MAX_AGE,
            httponly=True,
            secure=SESSION_COOKIE_SECURE,
            samesite=SESSION_COOKIE_SAMESITE,
            path="/",
        )
    return response


def get_public_base_url(request: Request) -> str:
    configured = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if configured:
        return configured
    return str(request.base_url).rstrip("/")


def make_public_url(request: Request, path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if not path.startswith("/"):
        path = "/" + path
    return f"{get_public_base_url(request)}{path}"


def storage_mode() -> str:
    return "s3" if os.environ.get("S3_BUCKET", "").strip() else "local"


def storage_key(user_id: str, filename: str) -> str:
    key_prefix = os.environ.get("S3_KEY_PREFIX", "uploads").strip().strip("/")
    relative_key = f"{user_id}/{filename}"
    return f"{key_prefix}/{relative_key}" if key_prefix else relative_key


def create_asset_signature(user_id: str, filename: str, expires: int) -> str:
    return sign_value(f"asset.{user_id}.{filename}.{expires}")


def verify_asset_signature(user_id: str, filename: str, expires: int | None, signature: str | None) -> bool:
    if not expires or not signature or expires < int(time.time()):
        return False
    expected = create_asset_signature(user_id, filename, expires)
    return hmac.compare_digest(signature, expected)


def make_signed_asset_url(request: Request, user_id: str, filename: str) -> str:
    expires = int(time.time()) + SIGNED_ASSET_TTL
    query = urlencode({
        "expires": expires,
        "signature": create_asset_signature(user_id, filename, expires),
    })
    return f"{get_public_base_url(request)}/uploads/{user_id}/{filename}?{query}"


def normalize_same_origin_asset_url(request: Request, raw_url: str) -> str | None:
    if not raw_url:
        return None

    raw_url = raw_url.strip()
    parsed = urlparse(raw_url)
    if parsed.scheme in ("http", "https"):
        base = urlparse(get_public_base_url(request))
        if parsed.netloc != base.netloc:
            return None
        path = parsed.path
    else:
        path = raw_url

    if not path.startswith("/"):
        path = "/" + path

    if path.startswith("/assets/"):
        return make_public_url(request, path)
    if not path.startswith("/uploads/"):
        return None

    parts = path.strip("/").split("/")
    if len(parts) != 3 or parts[0] != "uploads":
        return None
    _, user_id, filename = parts
    if user_id != request.state.user_id:
        return None
    return make_signed_asset_url(request, user_id, filename)


def sniff_image_content_type(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


async def read_upload_bytes(file: UploadFile) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Image is too large")
        chunks.append(chunk)
    return b"".join(chunks)


@lru_cache(maxsize=1)
def get_s3_client():
    if storage_mode() != "s3":
        return None
    if boto3 is None:
        raise RuntimeError("boto3 is required when S3_BUCKET is configured")

    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("S3_ENDPOINT_URL") or None,
        aws_access_key_id=os.environ.get("S3_ACCESS_KEY_ID") or os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY") or os.environ.get("AWS_SECRET_ACCESS_KEY"),
        region_name=os.environ.get("S3_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "auto",
    )


async def persist_upload(request: Request, filename: str, data: bytes, content_type: str) -> str:
    user_id = request.state.user_id
    if storage_mode() == "s3":
        bucket = os.environ.get("S3_BUCKET", "").strip()
        key = storage_key(user_id, filename)
        client = get_s3_client()
        await asyncio.to_thread(
            client.put_object,
            Bucket=bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            CacheControl="private, max-age=3600",
        )
        return make_public_url(request, f"/uploads/{user_id}/{filename}")

    user_upload_dir = UPLOAD_DIR / user_id
    user_upload_dir.mkdir(parents=True, exist_ok=True)
    path = user_upload_dir / filename
    await asyncio.to_thread(path.write_bytes, data)
    return make_public_url(request, f"/uploads/{user_id}/{filename}")


def safe_json_from_text(content: str) -> dict[str, Any]:
    text = (content or "").replace("```json", "").replace("```", "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end >= start:
        text = text[start : end + 1]
    return json.loads(text)


def get_ark_config(vision: bool = False) -> tuple[str, str, str] | None:
    api_key = os.environ.get("ARK_API_KEY") or os.environ.get("VOLC_API_KEY")
    if not api_key:
        return None

    base_url = os.environ.get(
        "ARK_BASE_URL",
        "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
    )
    model = (
        os.environ.get("ARK_VISION_MODEL")
        if vision
        else os.environ.get("ARK_TEXT_MODEL")
    )
    model = model or os.environ.get("ARK_MODEL")
    if not model:
        return None

    return api_key, base_url, model


async def call_ark_chat(messages: list[dict[str, Any]], *, vision: bool = False) -> str | None:
    config = get_ark_config(vision=vision)
    if not config:
        return None

    api_key, url, model = config
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": messages, "temperature": 0.3}

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        resp_data = resp.json()
        return resp_data["choices"][0]["message"]["content"]


def fallback_caption(album_type: str | None = "default") -> str:
    if album_type == "baby":
        return "宝宝的温暖成长瞬间"
    if album_type == "wedding":
        return "浪漫而珍贵的幸福时刻"
    return "值得珍藏的美好瞬间"


def storyline_templates(album_type: str | None = "default") -> list[dict[str, str]]:
    if album_type == "baby":
        return [
            {"title": "初见", "description": "宝宝初来人世的美好瞬间"},
            {"title": "成长", "description": "记录一点点变化和新本领"},
            {"title": "日常", "description": "日常陪伴里的温馨片段"},
            {"title": "珍藏", "description": "值得反复回看的幸福时刻"},
        ]
    if album_type == "wedding":
        return [
            {"title": "相遇", "description": "故事开始时的心动瞬间"},
            {"title": "心动", "description": "彼此靠近的浪漫片段"},
            {"title": "相守", "description": "携手同行的温柔承诺"},
            {"title": "定格", "description": "把幸福留在这一刻"},
        ]
    return [
        {"title": "开篇", "description": "回忆从这些画面慢慢展开"},
        {"title": "日常", "description": "平凡生活里的温暖片段"},
        {"title": "高光", "description": "最值得停留的闪亮瞬间"},
        {"title": "珍藏", "description": "留给以后反复回看的记忆"},
    ]


def fallback_storyline(photos: list[StorylinePhoto], album_type: str | None = "default") -> list[dict[str, Any]]:
    photo_ids = [str(photo.id) for photo in photos if photo.id]
    if not photo_ids:
        return []

    if len(photo_ids) == 1:
        chapter_count = 1
    elif len(photo_ids) <= 4:
        chapter_count = 2
    elif len(photo_ids) <= 9:
        chapter_count = 3
    else:
        chapter_count = 4

    templates = storyline_templates(album_type)
    chunk_size = (len(photo_ids) + chapter_count - 1) // chapter_count
    chapters: list[dict[str, Any]] = []

    for index in range(chapter_count):
        ids = photo_ids[index * chunk_size : (index + 1) * chunk_size]
        if not ids:
            continue
        template = templates[index] if index < len(templates) else templates[-1]
        chapters.append({
            "title": template["title"],
            "description": template["description"],
            "photo_ids": ids,
        })

    return chapters


def normalize_storyline_chapters(
    raw_chapters: Any,
    photos: list[StorylinePhoto],
    album_type: str | None = "default",
) -> list[dict[str, Any]]:
    known_ids = [str(photo.id) for photo in photos if photo.id]
    known = set(known_ids)
    if not known_ids or not isinstance(raw_chapters, list):
        return fallback_storyline(photos, album_type)

    templates = storyline_templates(album_type)
    used: set[str] = set()
    chapters: list[dict[str, Any]] = []

    for index, item in enumerate(raw_chapters):
        if not isinstance(item, dict):
            continue
        raw_ids = item.get("photo_ids", [])
        if not isinstance(raw_ids, list):
            continue

        clean_ids = []
        for raw_id in raw_ids:
            photo_id = str(raw_id)
            if photo_id in known and photo_id not in used:
                clean_ids.append(photo_id)
                used.add(photo_id)

        if not clean_ids:
            continue

        template = templates[min(index, len(templates) - 1)]
        title = str(item.get("title") or template["title"]).strip()[:12]
        description = str(item.get("description") or template["description"]).strip()[:80]
        chapters.append({
            "title": title or template["title"],
            "description": description or template["description"],
            "photo_ids": clean_ids,
        })

    missing_ids = [photo_id for photo_id in known_ids if photo_id not in used]
    if missing_ids:
        if chapters:
            chapters[-1]["photo_ids"].extend(missing_ids)
        else:
            return fallback_storyline(photos, album_type)

    if len(chapters) > 4:
        merged = chapters[:4]
        for extra in chapters[4:]:
            merged[-1]["photo_ids"].extend(extra["photo_ids"])
        chapters = merged

    if len(known_ids) > 1 and len(chapters) < 2:
        return fallback_storyline(photos, album_type)

    return chapters


def uploaded_filename_for(file: UploadFile, content_type: str | None = None) -> str:
    content_type = (content_type or file.content_type or "").lower()
    guessed_ext = mimetypes.guess_extension(content_type) or ""
    original_ext = Path(file.filename or "").suffix.lower()
    ext = guessed_ext if guessed_ext in {".jpg", ".jpeg", ".png", ".webp", ".gif"} else original_ext
    if ext == ".jpe":
        ext = ".jpg"
    if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        ext = ".jpg"
    return f"{uuid.uuid4().hex}{ext}"


@app.post("/api/upload")
async def upload_photos(request: Request, files: list[UploadFile] = File(...)):
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")
    if len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(status_code=400, detail=f"Upload at most {MAX_UPLOAD_FILES} images")

    photos = []

    for file in files:
        data = await read_upload_bytes(file)
        content_type = sniff_image_content_type(data)
        if not content_type or content_type not in ALLOWED_IMAGE_TYPES:
            raise HTTPException(status_code=400, detail="Unsupported or invalid image")

        filename = uploaded_filename_for(file, content_type)
        photo_id = Path(filename).stem

        try:
            url = await persist_upload(request, filename, data, content_type)
        except Exception as e:
            print("Upload persistence failed:", repr(e))
            raise HTTPException(status_code=503, detail="Upload storage is unavailable") from e

        photos.append({
            "id": photo_id,
            "url": url,
            "absoluteUrl": url,
            "size": len(data),
            "contentType": content_type,
        })

    return JSONResponse({"photos": photos})


@app.get("/uploads/{user_id}/{filename}")
async def serve_upload(
    request: Request,
    user_id: str,
    filename: str,
    expires: int | None = None,
    signature: str | None = None,
):
    if user_id != request.state.user_id and not verify_asset_signature(user_id, filename, expires, signature):
        return HTMLResponse("Not Found", status_code=404)
    if "/" in filename or "\\" in filename or len(user_id) != 32:
        return HTMLResponse("Not Found", status_code=404)

    if storage_mode() == "s3":
        try:
            bucket = os.environ.get("S3_BUCKET", "").strip()
            key = storage_key(user_id, filename)
            if S3_PRESIGNED_READ:
                url = await asyncio.to_thread(
                    get_s3_client().generate_presigned_url,
                    "get_object",
                    Params={"Bucket": bucket, "Key": key},
                    ExpiresIn=SIGNED_ASSET_TTL,
                )
                return RedirectResponse(
                    url,
                    status_code=307,
                    headers={"Cache-Control": "private, no-store"},
                )

            obj = await asyncio.to_thread(
                get_s3_client().get_object,
                Bucket=bucket,
                Key=key,
            )
            data = await asyncio.to_thread(obj["Body"].read)
            return Response(
                content=data,
                media_type=obj.get("ContentType") or "application/octet-stream",
                headers={"Cache-Control": "private, no-store"},
            )
        except Exception:
            return HTMLResponse("Not Found", status_code=404)

    user_upload_dir = (UPLOAD_DIR / user_id).resolve()
    file_path = (user_upload_dir / filename).resolve()
    if user_upload_dir not in file_path.parents:
        return HTMLResponse("Not Found", status_code=404)
    if not file_path.exists() or not file_path.is_file():
        return HTMLResponse("Not Found", status_code=404)
    return FileResponse(file_path, headers={"Cache-Control": "private, no-store"})


async def generate_caption_for_photo(request: Request, photo: CaptionPhoto, album_type: str | None) -> str:
    image_url = normalize_same_origin_asset_url(request, photo.url)
    if not image_url:
        return fallback_caption(album_type)

    prompt = (
        "用一句中文描述这张相册照片，突出温馨、自然、可爱或有纪念意义的感觉。"
        "不要编造具体身份，不超过20个字，只返回一句配文。"
    )
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": image_url}},
        ],
    }]

    try:
        content = await call_ark_chat(messages, vision=True)
        caption = (content or "").strip().strip("\"'“”")
        if caption:
            return caption[:40]
    except Exception as e:
        print("Caption generation failed:", repr(e))

    return fallback_caption(album_type)


@app.post("/api/caption")
async def generate_caption(request: Request, data: CaptionRequest):
    photo = CaptionPhoto(id=data.id, url=data.url)
    caption = await generate_caption_for_photo(request, photo, data.albumType)
    return JSONResponse({"id": data.id, "caption": caption})


@app.post("/api/captions")
async def generate_captions(request: Request, data: CaptionsRequest):
    photos = data.photos[:30]
    semaphore = asyncio.Semaphore(max(1, CAPTION_CONCURRENCY))

    async def caption_one(photo: CaptionPhoto):
        async with semaphore:
            return await generate_caption_for_photo(request, photo, data.albumType)

    generated = await asyncio.gather(*(caption_one(photo) for photo in photos))
    captions = []
    for photo, caption in zip(photos, generated):
        captions.append({
            "id": photo.id,
            "url": photo.url,
            "caption": caption,
        })
    return JSONResponse({"captions": captions})


@app.post("/api/storyline")
async def generate_storyline(data: StorylineRequest):
    photos = [photo for photo in data.photos[:30] if photo.id]
    if not photos:
        return JSONResponse({"chapters": []})

    compact_photos = [
        {
            "id": str(photo.id),
            "caption": (photo.caption or fallback_caption(data.albumType)).strip()[:80],
        }
        for photo in photos
    ]
    prompt = f"""你是相册故事线编辑师。
相册类型: {data.albumType or "default"}，标题: {data.title or ""}

每张照片的信息:
{json.dumps(compact_photos, ensure_ascii=False)}

任务：
1. 根据配文内容和氛围，把所有照片分成 2-4 个章节
2. 每个章节起一个标题（2-4字）+ 一句话描述
3. 给每个章节内照片排序，形成叙事节奏

请只返回合法 JSON，不要 Markdown，不要解释。格式如下：
{{"chapters":[{{"title":"初见","description":"宝宝初来人世的美好瞬间","photo_ids":["p3","p1","p5"]}}]}}
photo_ids 必须只使用输入中出现过的 id，且每张照片必须且只能出现一次。"""

    try:
        content = await call_ark_chat([{"role": "user", "content": prompt}], vision=False)
        if content:
            result = safe_json_from_text(content)
            chapters = normalize_storyline_chapters(
                result.get("chapters"),
                photos,
                data.albumType,
            )
            return JSONResponse({"chapters": chapters})
    except Exception as e:
        print("Storyline generation failed:", repr(e))

    return JSONResponse({"chapters": fallback_storyline(photos, data.albumType)})


@app.post("/api/recommend")
async def recommend(request: Request, data: RecommendRequest):
    album_type = data.albumType or "default"
    themes = data.themes[:50]
    tracks = data.tracks[:50]
    title = (data.title or "")[:200]
    captions = data.captions[:30]
    photos = data.photos[:30]

    photo_urls = []
    for photo in photos[:3]:
        raw_url = photo.get("src") if isinstance(photo, dict) else photo
        image_url = normalize_same_origin_asset_url(request, str(raw_url or ""))
        if image_url:
            photo_urls.append(image_url)

    prompt = f"""你是一个高级的相册主题搭配师。
当前相册类型: {album_type}
相册标题: {title}
部分图片配文: {json.dumps(captions[:8], ensure_ascii=False)}

候选主题资料（包含 ID、情绪、场景、配色和视觉语言）:
{json.dumps([t for t in themes if isinstance(t, dict)], ensure_ascii=False)}

候选音乐资料（包含 SRC、情绪、能量和适用场景）:
{json.dumps([t for t in tracks if isinstance(t, dict)], ensure_ascii=False)}

任务：根据相册照片内容、人物关系、色调、场景、情绪和叙事节奏，选出1个最匹配的主题(Theme ID)和1个音乐(Track SRC)。
音乐无法试听，请严格依据候选音乐的 mood、energy 和 scenes 标签匹配。
参考规则：
- 户外、自然、绿植多：优先 botanical/forest/mint
- 城市、人物写真、构图高级：优先 editorial/gallery/minimal
- 夜景、纪实、故事感强：优先 cinema/bokeh/starry
- 婚礼、周年、仪式感：优先 romantic-wedding/celebration/dark-gold
- 宝宝、儿童、亲子：优先 baby-blue/polaroid/classic

请只返回合法 JSON，形如 {{"themeId": "xxx", "trackSrc": "xxx"}}。"""

    content: str | None = None
    try:
        if photo_urls:
            messages = [{
                "role": "user",
                "content": [{"type": "text", "text": prompt}]
                + [{"type": "image_url", "image_url": {"url": url}} for url in photo_urls],
            }]
            content = await call_ark_chat(messages, vision=True)
        if not content:
            content = await call_ark_chat([{"role": "user", "content": prompt}], vision=False)
        if not content:
            return JSONResponse({"themeId": "", "trackSrc": ""})

        result = safe_json_from_text(content)
        theme_ids = {str(t.get("id", "")) for t in themes if isinstance(t, dict)}
        track_srcs = {str(t.get("src", "")) for t in tracks if isinstance(t, dict)}
        theme_id = result.get("themeId", "")
        track_src = result.get("trackSrc", "")
        return JSONResponse({
            "themeId": theme_id if theme_id in theme_ids else "",
            "trackSrc": track_src if track_src in track_srcs else "",
        })
    except Exception as e:
        print("LLM Call failed:", repr(e))
        return JSONResponse({"themeId": "", "trackSrc": ""})


@app.get("/api/health")
async def health():
    warnings = []
    if "*" in allowed_origins:
        warnings.append("ALLOWED_ORIGINS=* disables credentialed cross-origin sessions")
    if not SESSION_SECRET_CONFIGURED:
        warnings.append("SESSION_SECRET is insecure, a placeholder, or shorter than 32 characters")
    if not SESSION_COOKIE_SECURE:
        warnings.append("SESSION_COOKIE_SECURE is disabled")

    return JSONResponse({
        "ok": True,
        "environment": APP_ENV,
        "storage": storage_mode(),
        "s3PresignedRead": storage_mode() == "s3" and S3_PRESIGNED_READ,
        "visionConfigured": bool(get_ark_config(vision=True)),
        "textConfigured": bool(get_ark_config(vision=False)),
        "sessionSecretConfigured": SESSION_SECRET_CONFIGURED,
        "warnings": warnings,
    })


def safe_public_file(base_dir: Path, path: str) -> FileResponse | HTMLResponse:
    file_path = (base_dir / path).resolve()
    if base_dir.resolve() not in file_path.parents:
        return HTMLResponse("Not Found", status_code=404)
    if not file_path.exists() or not file_path.is_file():
        return HTMLResponse("Not Found", status_code=404)
    return FileResponse(file_path)


@app.get("/")
@app.get("/index.html")
async def serve_index():
    file_path = ROOT_DIR / "index.html"
    if not file_path.exists():
        return HTMLResponse("Not Found", status_code=404)
    content = file_path.read_text(encoding="utf-8")
    return HTMLResponse(content=content, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })


@app.get("/config.js")
async def serve_config():
    api_base = os.environ.get("FRONTEND_API_BASE_URL", "").strip().rstrip("/")
    return PlainTextResponse(
        f"window.ALBUM_API_BASE_URL = {json.dumps(api_base)};\n",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/sw.js")
async def serve_service_worker():
    return safe_public_file(ROOT_DIR, "sw.js")


@app.get("/assets/{path:path}")
async def serve_assets(path: str):
    return safe_public_file(ROOT_DIR / "assets", path)


@app.get("/{path:path}")
async def not_found(path: str):
    return HTMLResponse("Not Found", status_code=404)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
