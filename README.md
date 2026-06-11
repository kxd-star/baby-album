# AI Dynamic Album

用户上传照片后，系统会完成：

1. 持久化保存照片
2. 使用视觉模型生成中文配文
3. 使用文本模型规划故事线章节
4. 根据照片内容推荐主题和音乐
5. 生成包含章节页、照片、配文和音乐的动态翻页相册

## 云服务器部署

服务器需要安装 Docker 和 Docker Compose，并将域名解析到服务器。

```bash
git clone https://github.com/kxd-star/baby-album.git
cd baby-album
cp .env.example .env
```

至少配置：

```env
ARK_API_KEY=your_key
ARK_VISION_MODEL=your_vision_endpoint
ARK_TEXT_MODEL=your_text_endpoint
PUBLIC_BASE_URL=https://album.example.com
ALLOWED_ORIGINS=https://album.example.com
```

启动：

```bash
docker compose up -d --build
curl http://127.0.0.1:8080/api/health
```

使用 Nginx 或 Caddy 将域名反向代理到 `127.0.0.1:8080`。上传文件会保存在宿主机的 `data/uploads/`，重新部署不会丢失。

Nginx 需要允许较大的图片请求：

```nginx
server {
    server_name album.example.com;
    client_max_body_size 32m;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

## 对象存储

需要多实例部署时，配置 S3 兼容对象存储：

```env
S3_BUCKET=baby-album
S3_ENDPOINT_URL=https://your-s3-endpoint
S3_ACCESS_KEY_ID=your-access-key
S3_SECRET_ACCESS_KEY=your-secret-key
S3_REGION=auto
S3_PUBLIC_BASE_URL=https://cdn.example.com
```

`S3_PUBLIC_BASE_URL` 必须能被浏览器和视觉模型公开访问。

## 前后端分离

同域部署无需修改 `config.js`。若前端部署在 GitHub Pages，将 `config.js` 中的地址改成后端公网地址，并在后端配置对应的 `ALLOWED_ORIGINS`。
