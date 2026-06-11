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
git switch codex/cloud-ready-ai-album
cp .env.example .env
```

至少配置：

```env
ARK_API_KEY=your_key
ARK_VISION_MODEL=your_vision_endpoint
ARK_TEXT_MODEL=your_text_endpoint
PUBLIC_BASE_URL=https://album.example.com
ALLOWED_ORIGINS=https://album.example.com
SESSION_SECRET=请使用至少32位随机字符串
SESSION_COOKIE_SECURE=true
```

`docker compose` 默认以生产模式启动。生产模式下，若 `SESSION_SECRET` 未配置、仍为示例占位值或少于 32 位，服务会拒绝启动。可使用以下命令生成密钥：

```bash
openssl rand -hex 32
```

生产环境必须配置明确的 `ALLOWED_ORIGINS`。使用 `*` 时，浏览器不会为跨域 API 请求携带匿名用户会话 Cookie，用户隔离功能无法正常工作。

仅在本地使用纯 HTTP 开发时，可临时设置 `SESSION_COOKIE_SECURE=false`；公网部署必须保持为 `true`。

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
S3_PRESIGNED_READ=true
```

对象存储保持私有。浏览器只能访问当前匿名用户自己的图片，视觉模型通过短期签名地址读取图片。用户通过服务端鉴权后会重定向到短期 S3 预签名地址，图片下载流量不会经过 FastAPI 中转。若对象存储不支持预签名 URL，可设置 `S3_PRESIGNED_READ=false` 恢复服务端中转。

## 用户数据隔离

服务端会为每个浏览器签发带签名的匿名会话 Cookie。上传文件按匿名用户 ID 分目录保存，其他用户即使拿到图片 URL 也无法访问。AI 识图使用短期签名 URL。

这能隔离不同设备和不同浏览器会话；若需要同一设备切换账号、跨设备同步或账号找回，还需要继续接入登录系统并将匿名用户 ID 绑定到正式用户账号。

当前相册故事线与设置保存在浏览器 `localStorage` 中。清理浏览器数据、更换浏览器或更换设备后无法恢复历史相册；正式账号和服务端相册数据库属于后续扩展，不是 `main` 分支现有能力。

## 上线检查

启动后检查容器状态和健康接口：

```bash
docker compose ps
curl https://album.example.com/api/health
```

健康接口中的 `sessionSecretConfigured`、`visionConfigured` 和 `textConfigured` 应为 `true`，`warnings` 应为空。启用 S3 时，`storage` 应为 `s3`，`s3PresignedRead` 应为 `true`。

## 前后端分离

同域部署无需修改 `config.js`，FastAPI 的动态 `/config.js` 路由会提供运行时配置；仓库中的静态 `config.js` 只用于纯静态部署。

若前端部署在 GitHub Pages，将静态 `config.js` 中的地址改成后端公网地址，并在后端配置对应的 `ALLOWED_ORIGINS`、`SESSION_COOKIE_SAMESITE=none` 和 `SESSION_COOKIE_SECURE=true`。
