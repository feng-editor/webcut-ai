# Vercel Serverless Function: GET /api/ai/health
# 能力位声明：tts=True（tts.py 透传 CosyVoice），video=False（文生视频耗时长，
# 不适合 Serverless —— 前端探测到 video=False 会自动使用浏览器内本地演示生成，
# 与 ai-backend/server.py 的完整后端二选一部署，前端无需任何改动）。
import json
import os
from http.server import BaseHTTPRequestHandler


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        has_key = bool(os.environ.get('DASHSCOPE_API_KEY'))
        body = json.dumps({
            'available': True,
            'video': False,           # Serverless 不承载长任务文生视频
            'tts': True,              # AI 配音可用（无 Key 时为演示音）
            'provider': 'cosyvoice-serverless' if has_key else 'serverless-demo-tts',
        }).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
