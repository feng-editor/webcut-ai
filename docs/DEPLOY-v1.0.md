# WebCut v1.0.0 部署说明

> 本地已全量验证。云服务器（116.62.153.186）更新需要执行以下命令——本机无 SSH 凭据，此为唯一需人工执行的步骤。

## 一、云服务器更新（约 2 分钟）

SSH 登录服务器后执行：

```bash
cd /root/webcut            # 或服务器上的工程目录（git clone 的位置）
git pull                   # 拉取 v1.0.0（仓库需先 push：见下）
docker-compose down
docker-compose up -d --build
```

若服务器不是 git 工作区，可上传包覆盖：

```bash
# 本机执行（Git Bash）：
scp C:/剪辑/webcut-v1.0.0-source.tar.gz root@116.62.153.186:/root/
# 服务器执行：
cd /root && tar -xzf webcut-v1.0.0-source.tar.gz && cd video-editor
docker-compose down && docker-compose up -d --build
```

**部署前备份**（务必）：

```bash
docker cp webcut-ai-1:/app/data ./backup-data-$(date +%F) 2>/dev/null || cp -r ai-backend/data ./backup-data-$(date +%F)
```

## 二、v1.0.0 新增数据

- `ai-backend/data/webcut.db`（SQLite）：用户/会话/云端工程 —— 首次启动自动建表，无需迁移
- Docker 卷挂载 `./ai-backend/data` 已覆盖该路径（docker-compose.yml 数据卷不变）
- 旧匿名云数据（cloud/ 目录）保留只读兼容，不受影响

## 三、部署后验证

```bash
curl http://127.0.0.1:8787/api/ai/health      # → {"available":true,...}
curl http://127.0.0.1:8787/api/status          # → {"version":"1.0.0",...}
curl -X POST http://127.0.0.1:8787/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"smoke@test.app","password":"smoke123"}'   # → {"token":...}
```

浏览器打开站点 → 头像菜单 →「登录 / 注册账号」→ 注册 → 个人中心应显示邮箱与 AI 积分 100。

## 四、v1.0.0 架构摘要

| 层 | 内容 |
|---|---|
| 用户 | SQLite users/sessions/cloud_projects（WAL），PBKDF2-HMAC-SHA256 10 万次 |
| 隔离 | 登录用户云工程按 user_id 存 SQLite；匿名 token 走原文件目录（兼容） |
| 商业化 | plan/plan_expires/ai_credits 字段 + mock-pay 升级链路；AI 积分扣减（视频20/图片5/配音2/分镜1） |
| 安全 | 密码仅存哈希；注册限流 10/时；token 服务端 session 表；静态服务黑名单（既有） |
| 监控 | GET /api/status（版本/uptime/任务数/DB 体积） |

## 五、已知限制（如实）

- 字幕 ASR / 背景移除 / 超分：依赖外部模型服务，UI 如实标注开发中
- 支付为演示通道（mock-pay）；接真实支付宝需商户凭证（existing 配置位不变）
- 代理为会话级（刷新后按需重新生成）
