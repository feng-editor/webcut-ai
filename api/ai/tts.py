# Vercel Serverless Function: POST /api/ai/tts  {text, voice?}
# 有 DASHSCOPE_API_KEY：调 CosyVoice 合成 → 下载音频字节；
# 无 Key：本地合成演示音（纯标准库，与 ai-backend/server.py 同一套逻辑）。
# 返回 {url: 'data:audio/..;base64,...'} —— 前端用 fetch(url) 取字节，
# data: URL 原生支持 fetch，因此前端代码零改动（无需 /ai-media 文件服务）。
import base64
import json
import os
import struct
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler

# 联调实测（2026-10）端点：/api/v1/services/audio/tts/SpeechSynthesizer
# （旧 multimodal-generation 端点对 cosyvoice-v2 已失效，会报 url error）
DASHSCOPE_URL = ('https://dashscope.aliyuncs.com/api/v1/services/'
                 'audio/tts/SpeechSynthesizer')


def fetch_with_retry(url, data=None, headers=None, retries=2, timeout=25):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers or {})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception as e:
            last = e
    raise last


def cosyvoice_synth(text, voice):
    key = os.environ['DASHSCOPE_API_KEY']
    payload = json.dumps({
        'model': 'cosyvoice-v2',
        'input': {'text': text[:2000], 'voice': voice or 'longxiaochun_v2',
                  'format': 'mp3'},
    }).encode('utf-8')
    code, raw = fetch_with_retry(DASHSCOPE_URL, data=payload, headers={
        'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    j = json.loads(raw.decode('utf-8'))
    if j.get('code'):
        raise RuntimeError(str(j.get('code')) + ' ' + str(j.get('message', ''))[:160])
    url = ((j.get('output') or {}).get('audio') or {}).get('url')
    if not url:
        raise RuntimeError('CosyVoice 未返回音频 URL')
    _, audio = fetch_with_retry(url, timeout=60)
    return audio, 'audio/mpeg'


def synth_demo_wav(text):
    """无 Key 演示音：按字符生成带包络音节（验证整条链路）。"""
    sr = 22050
    chars = ''.join(text.split()) or '你好'
    total = int((len(chars) * 0.16 + 0.6) * sr)
    pcm = bytearray(total * 2)
    for i, ch in enumerate(chars):
        f0 = 140 + (ord(ch) % 120)
        n = int(0.16 * sr * 0.8)
        start = int(i * 0.16 * sr)
        for k in range(min(n, total - start)):
            t = k / sr
            env = 1 - abs(1 - 2 * k / n)
            v = (0.75 * ((2 * 3.141592653589793 * f0 * t) % 1.0 - 0.5) * 2 +
                 0.25 * ((2 * 3.141592653589793 * f0 * 2.02 * t) % 1.0 - 0.5) * 2) * 0.4 * env
            struct.pack_into('<h', pcm, (start + k) * 2,
                             int(max(-1.0, min(1.0, v)) * 32767))
    header = (b'RIFF' + struct.pack('<I', 36 + len(pcm)) + b'WAVE' + b'fmt ' +
              struct.pack('<IHHIIHH', 16, 1, 1, sr, sr * 2, 2, 16) +
              b'data' + struct.pack('<I', len(pcm)))
    return header + bytes(pcm), 'audio/wav'


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            n = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(n).decode('utf-8')) if n else {}
            text = (body.get('text') or '').strip()
            if not text:
                self._json(400, {'error': 'text 不能为空'})
                return
            if os.environ.get('DASHSCOPE_API_KEY'):
                try:
                    audio, mime = cosyvoice_synth(text, body.get('voice'))
                    self._json(200, {'url': _data_url(audio, mime), 'demo': False})
                except Exception as e:
                    self._json(502, {'error': 'TTS 失败：' + str(e)[:200]})
            else:
                audio, mime = synth_demo_wav(text)
                self._json(200, {'url': _data_url(audio, mime), 'demo': True})
        except Exception as e:
            self._json(500, {'error': str(e)[:200]})

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
