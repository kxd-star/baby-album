import os
import json
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse
from starlette.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/api/recommend")
async def recommend(request: Request):
    data = await request.json()
    album_type = data.get("albumType", "default")
    themes = data.get("themes", [])
    tracks = data.get("tracks", [])
    title = data.get("title", "")
    captions = data.get("captions", [])
    
    prompt = f"""你是一个高级的相册主题搭配师。
当前相册类型: {album_type}
相册标题: {title}
部分图片配文: {json.dumps(captions[:5], ensure_ascii=False)}

候选主题列表(Theme ID): {json.dumps([t['id'] for t in themes], ensure_ascii=False)}
候选音乐列表(Track SRC): {json.dumps([t['src'] for t in tracks], ensure_ascii=False)}

任务：根据相册氛围，选出1个最匹配的主题(Theme ID)和1个音乐(Track SRC)。
注意：
如果类型是 baby，务必选择充满童趣、温暖的主题（比如 classic/macaron/starry-night）和欢快音乐。
如果类型是 wedding，务必选择浪漫、神圣的主题（比如 romantic-wedding/golden-vintage）和浪漫婚礼音乐。

请只返回一段合法的 JSON，形如 {{"themeId": "xxx", "trackSrc": "xxx"}}。不要包含任何多余的 Markdown 格式！"""

    api_key = os.environ.get("ARK_API_KEY") or os.environ.get("VOLC_API_KEY")
    if not api_key:
        print("Warning: API Key missing!")
        return JSONResponse({"themeId": "", "trackSrc": ""})
        
    url = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": "ep-20250218123237-vszbd",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3
    }
    
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp_data = resp.json()
            content = resp_data["choices"][0]["message"]["content"]
            content = content.replace("```json", "").replace("```", "").strip()
            result = json.loads(content)
            return JSONResponse(result)
    except Exception as e:
        print("LLM Call failed:", repr(e))
        return JSONResponse({"themeId": "", "trackSrc": ""})

@app.get("/{path:path}")
async def serve_static(path: str):
    if not path or path == "/":
        path = "index.html"
    
    file_path = os.path.join(".", path)
    if os.path.exists(file_path) and os.path.isfile(file_path):
        if path == "index.html":
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
            return HTMLResponse(content=content, headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0"
            })
        else:
            return FileResponse(file_path)
    return HTMLResponse("Not Found", status_code=404)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
