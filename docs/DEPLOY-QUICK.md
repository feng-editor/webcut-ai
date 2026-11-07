# WebCut 公网部署一键指南

> 所有代码已推送 GitHub（f9d6a7c）。以下命令在服务器上执行即可上线。
> 我没有你的 SSH 凭据，无法远程执行——请按此指南操作（复制粘贴即可）。

## 快速方式 A：服务器有 git 工作区

SSH 登录服务器后：

```bash
cd /root/webcut
git pull origin main
docker-compose down
docker-compose up -d --build
```

## 快速方式 B：直接上传包

**在你电脑上执行（Git Bash）：**

```bash
scp C:/剪辑/webcut-v1.7.0-full-source.tar.gz root@116.62.153.186:/root/
```

**在服务器上执行：**

```bash
cd /root
tar -xzf webcut-v1.7.0-full-source.tar.gz
cd video-editor
docker-compose down
docker-compose up -d --build
```

## 验证部署成功

```bash
curl http://127.0.0.1:8787/api/status
# → {"version":"1.0.0",...}

curl http://127.0.0.1:8787/api/ai/health
# → {"available":true,...}
```

然后浏览器打开 `http://116.62.153.186:8787` 应该能看到 WebCut 编辑器。

## 手机安装

1. 手机浏览器打开 `http://116.62.153.186:8787`
2. 底部出现「📱 安装 WebCut 到手机桌面」
3. 点击安装 → 桌面出现图标 → 全屏独立运行

## HTTPS（如果需要）

当前部署是 HTTP。如果需要 HTTPS（PWA 安装在某些浏览器要求 HTTPS），可以在服务器上安装 Nginx + Let's Encrypt：

```bash
apt install -y nginx certbot python3-certbot-nginx
certbot --nginx -d 你的域名
```

或者使用已有的 Netlify 域名（netlify.toml 已配置反向代理）。
