import asyncio
import json
import mimetypes
import os
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx
import uvicorn
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel
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
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
CAPTION_CONCURRENCY = int(os.environ.get("CAPTION_CONCURRENCY", "4"))

allowed_origins = [
    origin.strip()
    for origin in os.environ.get("ALLOWED_ORIGINS", "*").split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins or ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
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


def get_storage_public_base_url() -> str:
    return os.environ.get("S3_PUBLIC_BASE_URL", "").strip().rstrip("/")


def storage_mode() -> str:
    return "s3" if os.environ.get("S3_BUCKET", "").strip() else "local"


def normalize_same_origin_asset_url(request: Request, raw_url: str) -> str | None:
    if not raw_url:
        return None

    raw_url = raw_url.strip()
    parsed = urlparse(raw_url)
    if parsed.scheme in ("http", "https"):
        storage_base = get_storage_public_base_url()
        if storage_base and (raw_url == storage_base or raw_url.startswith(storage_base + "/")):
            return raw_url

        base = urlparse(get_public_base_url(request))
        if parsed.netloc != base.netloc:
            return None
        path = parsed.path
    else:
        path = raw_url

    if not path.startswith("/"):
        path = "/" + path

    if not (path.startswith("/uploads/") or path.startswith("/assets/")):
        return None

    return make_public_url(request, path)


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
    if storage_mode() == "s3":
        bucket = os.environ.get("S3_BUCKET", "").strip()
        public_base = get_storage_public_base_url()
        if not public_base:
            raise RuntimeError("S3_PUBLIC_BASE_URL is required when S3_BUCKET is configured")

        key_prefix = os.environ.get("S3_KEY_PREFIX", "uploads").strip().strip("/")
        key = f"{key_prefix}/{filename}" if key_prefix else filename
        client = get_s3_client()
        await asyncio.to_thread(
            client.put_object,
            Bucket=bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            CacheControl="public, max-age=31536000, immutable",
        )
        return f"{public_base}/{quote(key, safe='/')}"

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    path = UPLOAD_DIR / filename
    await asyncio.to_thread(path.write_bytes, data)
    return make_public_url(request, f"/uploads/{filename}")


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
    if not model and not vision:
        model = "ep-20250218123237-vszbd"
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


@app.get("/uploads/{filename}")
async def serve_upload(filename: str):
    if storage_mode() != "local":
        return HTMLResponse("Not Found", status_code=404)
    if "/" in filename or "\\" in filename:
        return HTMLResponse("Not Found", status_code=404)

    file_path = (UPLOAD_DIR / filename).resolve()
    if UPLOAD_DIR.resolve() not in file_path.parents:
        return HTMLResponse("Not Found", status_code=404)
    if not file_path.exists() or not file_path.is_file():
        return HTMLResponse("Not Found", status_code=404)
    return FileResponse(file_path)


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
async def recommend(request: Request):
    data = await request.json()
    album_type = data.get("albumType", "default")
    themes = data.get("themes", [])
    tracks = data.get("tracks", [])
    title = data.get("title", "")
    captions = data.get("captions", [])
    photos = data.get("photos", [])

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
    return JSONResponse({
        "ok": True,
        "storage": storage_mode(),
        "visionConfigured": bool(get_ark_config(vision=True)),
        "textConfigured": bool(get_ark_config(vision=False)),
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
