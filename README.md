# WebCut — 网页版视频剪辑器

本地优先的 Web 端视频剪辑工具：全部解码 / 预览 / 导出均在浏览器本地完成，素材不上传；配套可选的 Python 后端提供 AI 生成（通义万相 / CosyVoice）、支付订单与工程云同步。

## 一键部署

### 方案 A：前端 Vercel + 后端 Docker（推荐）

**前端（Vercel，免费）：**
```bash
# 1. 把以下文件推到 GitHub 仓库（仓库根目录 = video-editor/）
#    index.html, entitlements-config.js, vercel.json, js/, api/
# 2. Vercel → Add New Project → 导入仓库 → Deploy
# 3. 部署后访问 https://你的项目.vercel.app
```

**后端（Docker，需要云服务器）：**
```bash
# 1. 在云服务器上克隆同一仓库
git clone https://github.com/你的用户名/你的仓库.git
cd 你的仓库

# 2. 创建 .env 配置文件
cp ai-backend/.env.example ai-backend/.env
vim ai-backend/.env  # 填入 DASHSCOPE_API_KEY 等

# 3. 一键启动
docker-compose up -d

# 4. 验证
curl http://127.0.0.1:8787/api/ai/health
# 应返回 {"available": true, ...}
```

**前端连后端**：前端部署在 Vercel（`xxx.vercel.app`），后端部署在云服务器（`IP:8787`）。在云服务器上配置 Nginx 反代 + HTTPS 后，修改前端 `AIClient` 中的 API 地址即可连接。

### 方案 B：全 Docker（单服务器部署）

```bash
# 整个项目在一个 Docker 容器里（前端 + 后端一起服务）
docker-compose up -d
# 访问 http://服务器IP:8787 即可使用完整功能
```

### 方案 C：本地开发

```bash
cd video-editor
pip install qrcode pypng cryptography
python ai-backend/server.py
# 浏览器打开 http://127.0.0.1:8787
```

## 功能概览

| 模块 | 能力 |
|---|---|
| 剪辑 | 三轨时间线（视频/音频/文字）、拖拽/边缘裁剪/分割/吸附、撤销重做（Ctrl+Z / Ctrl+Y）、真实缩略图与 PCM 波形渐进填充 |
| 解码 | WebCodecs 硬解 H.264 + AAC；内置最小 MP4 解封装器（零依赖）；长音频流式分段解码（30s/段，LRU） |
| AI 生成 | 一句话生成视频（通义万相 wanx2.1-t2v-plus）+ CosyVoice 真人旁白，自动入轨、静音检测、PTS 音画同步 |
| 导出 | H.264+AAC 双轨 MP4（有声视频）、水印/分辨率分档（免费 720P 含水印 / 专业 4K 无水印）、本地渲染 |
| 支付 | 真实订单架构（服务端权益持久化、真二维码、轮询）；支付宝当面付 Provider 配置即启用，默认演示通道 |
| 持久化 | IndexedDB 自动保存（2.5s 防抖）+ 云同步（自动备份 / 从云端恢复 / 容量统计 / 断网重试） |
| 其他 | 用户协议弹窗、移动端响应式（≤768px 抽屉式面板）、深色 UI |

## 目录结构

```
video-editor/
├── index.html               # 编辑器单文件应用（UI + 引擎接线）
├── entitlements-config.js   # 权益配置（免费/专业档位与限制；后端在位时以服务端下发为准）
├── js/cloud-sync.js         # 云同步模块（WebCutCloud 工厂：重试/进度/用量）
├── paywall-test.html        # 付费墙独立测试页（开发用）
├── vercel.json              # Vercel 部署配置（TTS Serverless 函数）
├── api/ai/                  # Vercel Functions（health / tts）
├── ai-backend/
│   ├── server.py            # Python 后端（推荐）：AI 生成 + TTS + 支付 + 云同步 + 静态托管
│   ├── server.js            # Node 版后端（AI 功能等价，无支付/云同步）
│   ├── .env.example         # 环境变量模板（复制为 .env 使用）
│   ├── tmp/  data/  cloud/  # 运行时产物（已 gitignore）
└── js/ …
```

## 快速开始

### 环境要求

- Python 3.9+（后端）；Chrome / Edge 94+（前端，需支持 WebCodecs）
- 后端可选依赖：`pip install qrcode pypng cryptography`（云同步二维码与支付宝签名；不装则对应功能降级）

### 启动（完整功能，推荐）

```bat
cd video-editor
pip install qrcode pypng cryptography
copy ai-backend\.env.example ai-backend\.env   :: 编辑 .env 填入 Key（见下）
python ai-backend\server.py
:: 浏览器打开 http://127.0.0.1:8787
```

### 启动（仅前端，零配置）

```bat
cd video-editor
python -m http.server 8899
:: 或直接双击 index.html（file:// 亦可运行）
```

无后端时：剪辑/导出/AI 本地演示生成/本地保存全部可用；AI 配音、真实文生视频、支付、云同步自动停用并在 UI 中明示。

## 环境变量（ai-backend/.env）

| 变量 | 必填 | 说明 |
|---|---|---|
| `DASHSCOPE_API_KEY` | 推荐 | 阿里云百炼 Key（https://bailian.console.aliyun.com）。启用真实文生视频（wanx2.1-t2v-plus，有免费额度）与 CosyVoice 配音；不填则 AI 走本地演示 |
| `ALIPAY_APP_ID` | 可选 | 支付宝开放平台应用 APPID。三项齐备后支付自动从演示通道切换为真实收款（当面付） |
| `ALIPAY_PRIVATE_KEY` | 可选 | 支付宝密钥工具生成的「应用私钥」（PEM 或裸 base64 均可） |
| `ALIPAY_PUBLIC_KEY` | 可选 | 平台显示的「支付宝公钥」（响应验签） |
| `AI_VIDEO_MODEL` | 可选 | 文生视频模型名，默认 `wanx2.1-t2v-plus` |
| `PORT` | 可选 | 后端端口，默认 8787 |

## Vercel 部署（纯静态 + TTS 函数）

1. 推送 `index.html`、`entitlements-config.js`、`js/`、`api/`、`vercel.json` 至 GitHub 仓库；
2. Vercel 导入仓库，Framework Preset 选 **Other**，无需构建命令；
3. 环境变量（可选）：`DASHSCOPE_API_KEY`（启用真实 CosyVoice；不配则为演示音）；
4. 部署后编辑器全功能可用（文生视频走本地演示生成）；支付与云同步需自托管 `ai-backend/server.py`。

## 常用快捷键

`空格` 播放/暂停 · `S` 分割 · `Del` 删除 · `Ctrl+Z/Y` 撤销/重做 · `Ctrl+S` 保存 · `←→`（`Shift` 加速）移动播放头 · 时间线 `Ctrl+滚轮` 缩放

## 已知边界

- 导出视频暂为固定 30fps；AAC 编码存在 ~20ms priming 偏移（不可闻）；
- 云同步按浏览器 Token 记账（无账号体系），换浏览器 = 新空间；配额 2GB/用户；
- 微信支付 Provider 未实现（需企业资质，架构已预留）；
- 长时间运行的后台进程可能被系统清理，正式使用请自开终端运行后端。

## 隐私

所有视频/音频处理（解码、预览、导出）均在浏览器本地完成；后端仅中转 AI 生成请求与云备份文件（你主动上传时）。
