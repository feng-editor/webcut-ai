# ============================================================
# WebCut 后端 — Flask 版
# 比原 http.server 版更可靠（requests 库自动处理编码/重试）
#
# 启动：python server.py（或 docker-compose up）
# 端口：8787
# ============================================================
import sys
import io

# 强制 stdout/stderr 使用 UTF-8（修复 Docker 容器内 latin-1 编码错误）
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
os.environ['PYTHONIOENCODING'] = 'utf-8'

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import os
import json
import time
import uuid
import hashlib
import threading
import requests as req_lib

app = Flask(__name__)
CORS(app)  # 允许所有来源跨域（Netlify 前端需要）

# ============================================================
# 配置
# ============================================================
PORT = int(os.environ.get('PORT', '8787'))

def _load_env_file():
    """读取 ai-backend/.env"""
    vals = {}
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    try:
        with open(env_path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                vals[k.strip()] = v.strip().strip('\'"')
    except Exception:
        pass
    return vals

_ENV = _load_env_file()

def cfg(name, default=''):
    return os.environ.get(name, '') or _ENV.get(name, '') or default

KEY = cfg('DASHSCOPE_API_KEY')
DASHSCOPE_BASE = cfg('DASHSCOPE_BASE_URL', 'https://dashscope.aliyuncs.com')
WANX_MODEL = cfg('AI_VIDEO_MODEL', 'wanx2.1-t2v-plus')

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
TMP = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'tmp')
DATA_DIR = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'data')
CLOUD_DIR = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'cloud')
PAY_FILE = os.path.join(DATA_DIR, 'payments.json')
os.makedirs(TMP, exist_ok=True)

TASKS = {}  # AI 生成任务
_db_lock = threading.Lock()

print('=== WebCut 后端启动 ===')
print('Port:', PORT)
print('DASHSCOPE_API_KEY:', '已配置(' + KEY[:10] + '...)' if KEY else '!!未配置!!')
print('DASHSCOPE_BASE:', DASHSCOPE_BASE)
print('Model:', WANX_MODEL)
print('======================')


# ============================================================
# 健康检查
# ============================================================
@app.route('/api/ai/health', methods=['GET'])
def health():
    return jsonify({
        'available': True,
        'video': bool(KEY),
        'tts': True,
        'provider': 'dashscope-wanx+cosyvoice' if KEY else 'local-demo',
        'payments': {'alipay': False, 'demo': True}
    })


# ============================================================
# AI 视频生成（通义万相）
# ============================================================
@app.route('/api/ai/video', methods=['POST'])
def create_video_task():
    print('>>> [AI] 收到生成视频请求 <<<', flush=True)
    try:
        data = request.get_json(force=True, silent=True) or {}
        prompt = (data.get('prompt') or '').strip()
        duration = data.get('duration') or 4
        try:
            print('>>> [AI] prompt=%s, duration=%s <<<' % (repr(prompt[:50]), duration), flush=True)
        except Exception:
            print('>>> [AI] prompt=(打印失败), duration=%s <<<' % duration, flush=True)

        if not prompt:
            return jsonify({'error': 'prompt 不能为空'}), 400
        if not KEY:
            return jsonify({'error': '未配置 DASHSCOPE_API_KEY'}), 501

        # 调用阿里云万相 API
        url = DASHSCOPE_BASE + '/api/v1/services/aigc/video-generation/video-synthesis'
        headers = {
            'Authorization': 'Bearer ' + KEY,
            'Content-Type': 'application/json',
            'X-DashScope-Async': 'enable'
        }
        payload = {
            'model': WANX_MODEL,
            'input': {'prompt': prompt},
            'parameters': {'size': '960*960', 'duration': duration}
        }

        print(f'>>> [AI] POST {url} <<<', flush=True)
        resp = req_lib.post(url, headers=headers, json=payload, timeout=30)
        print(f'>>> [AI] 阿里云返回: {resp.status_code} <<<', flush=True)

        if resp.status_code != 200:
            print(f'>>> [AI] 错误响应: {resp.text[:300]} <<<', flush=True)
            return jsonify({'error': f'阿里云返回 {resp.status_code}: {resp.text[:200]}'}), 502

        j = resp.json()
        task_id = (j.get('output') or {}).get('task_id')
        if not task_id:
            return jsonify({'error': f'未返回 task_id: {json.dumps(j)[:200]}'}), 502

        print(f'>>> [AI] 万相任务已创建: {task_id} <<<', flush=True)

        # 创建本地任务并启动后台轮询
        local_id = str(uuid.uuid4())
        TASKS[local_id] = {
            'id': local_id, 'status': 'running', 'progress': 0.1,
            'stage': '生成中', 'provider_task_id': task_id,
            'video_file': None, 'error': None
        }

        def poll():
            try:
                for _ in range(200):  # 最多 10 分钟
                    time.sleep(3)
                    poll_url = DASHSCOPE_BASE + '/api/v1/tasks/' + task_id
                    pr = req_lib.get(poll_url, headers={'Authorization': 'Bearer ' + KEY}, timeout=15)
                    if pr.status_code != 200:
                        print(f'>>> [AI] 轮询失败: {pr.status_code} <<<', flush=True)
                        continue

                    pj = pr.json()
                    out = pj.get('output') or {}
                    status = out.get('task_status', '')
                    print(f'>>> [AI] 轮询: {status} <<<', flush=True)

                    TASKS[local_id]['progress'] = 1.0 if status == 'SUCCEEDED' else (0.7 if status == 'RUNNING' else 0.3)

                    if status == 'SUCCEEDED':
                        video_url = out.get('video_url')
                        if video_url:
                            print(f'>>> [AI] 下载视频: {video_url[:80]} <<<', flush=True)
                            vr = req_lib.get(video_url, timeout=120)
                            if vr.status_code == 200:
                                fname = str(uuid.uuid4()) + '.mp4'
                                fpath = os.path.join(TMP, fname)
                                with open(fpath, 'wb') as f:
                                    f.write(vr.content)
                                TASKS[local_id]['video_file'] = fname
                                TASKS[local_id]['status'] = 'done'
                                TASKS[local_id]['progress'] = 1.0
                                print(f'>>> [AI] 视频已保存: {fname} ({len(vr.content)//1024}KB) <<<', flush=True)
                            else:
                                TASKS[local_id]['status'] = 'failed'
                                TASKS[local_id]['error'] = f'视频下载失败 HTTP {vr.status_code}'
                        return
                    elif status == 'FAILED':
                        TASKS[local_id]['status'] = 'failed'
                        TASKS[local_id]['error'] = f"生成失败: {out.get('message', '未知原因')}"
                        print(f'>>> [AI] 生成失败 <<<', flush=True)
                        return
            except Exception as e:
                TASKS[local_id]['status'] = 'failed'
                TASKS[local_id]['error'] = str(e)
                print(f'>>> [AI] 轮询异常: {e} <<<', flush=True)

        threading.Thread(target=poll, daemon=True).start()
        return jsonify({'taskId': local_id})

    except Exception as e:
        import traceback
        print(f'!!!! [AI] 致命异常: {e} !!!!\n{traceback.format_exc()}', flush=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/ai/video/<task_id>', methods=['GET'])
def get_video_status(task_id):
    t = TASKS.get(task_id)
    if not t:
        return jsonify({'error': '任务不存在'}), 404
    return jsonify({
        'status': t['status'], 'progress': t['progress'], 'stage': t['stage'],
        'error': t['error'],
        'url': ('/ai-media/' + t['video_file']) if t.get('video_file') else None
    })


# ============================================================
# AI 配音（CosyVoice TTS）
# ============================================================
@app.route('/api/ai/tts', methods=['POST'])
def tts():
    print('>>> [TTS] 收到配音请求 <<<', flush=True)
    try:
        data = request.get_json(force=True, silent=True) or {}
        text = (data.get('text') or '').strip()
        voice = data.get('voice') or 'longxiaochun_v2'
        if not text:
            return jsonify({'error': 'text 不能为空'}), 400

        if not KEY:
            # 无 Key：本地合成演示音
            fname = str(uuid.uuid4()) + '.wav'
            fpath = os.path.join(TMP, fname)
            _synth_demo_wav(fpath, text)
            return jsonify({'url': '/ai-media/' + fname, 'demo': True})

        url = DASHSCOPE_BASE + '/api/v1/services/audio/tts/SpeechSynthesizer'
        headers = {'Authorization': 'Bearer ' + KEY, 'Content-Type': 'application/json'}
        payload = {'model': 'cosyvoice-v2', 'input': {'text': text[:2000], 'voice': voice, 'format': 'mp3'}}

        print(f'>>> [TTS] POST {url} <<<', flush=True)
        resp = req_lib.post(url, headers=headers, json=payload, timeout=30)
        print(f'>>> [TTS] 返回: {resp.status_code} <<<', flush=True)

        if resp.status_code != 200:
            return jsonify({'error': f'TTS 失败 HTTP {resp.status_code}: {resp.text[:150]}'}), 502

        j = resp.json()
        audio_url = ((j.get('output') or {}).get('audio') or {}).get('url')
        if not audio_url:
            return jsonify({'error': 'CosyVoice 未返回音频 URL'}), 502

        # 下载音频
        ar = req_lib.get(audio_url, timeout=60)
        if ar.status_code != 200:
            return jsonify({'error': f'音频下载失败 HTTP {ar.status_code}'}), 502

        ext = 'wav' if audio_url.endswith('.wav') else 'mp3'
        fname = str(uuid.uuid4()) + '.' + ext
        with open(os.path.join(TMP, fname), 'wb') as f:
            f.write(ar.content)
        print(f'>>> [TTS] 音频已保存: {fname} <<<', flush=True)
        return jsonify({'url': '/ai-media/' + fname, 'demo': False})

    except Exception as e:
        print(f'!!!! [TTS] 异常: {e} !!!!', flush=True)
        return jsonify({'error': str(e)}), 500


def _synth_demo_wav(fpath, text):
    """无 Key 时的演示音合成"""
    import struct, math
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
            v = math.sin(2 * math.pi * f0 * t) * 0.4 * env
            struct.pack_into('<h', pcm, (start + k) * 2, int(max(-1, min(1, v)) * 32767))
    header = (b'RIFF' + struct.pack('<I', 36 + len(pcm)) + b'WAVE' + b'fmt ' +
              struct.pack('<IHHIIHH', 16, 1, 1, sr, sr * 2, 2, 16) +
              b'data' + struct.pack('<I', len(pcm)))
    with open(fpath, 'wb') as f:
        f.write(header + bytes(pcm))


# ============================================================
# 媒体文件服务
# ============================================================
@app.route('/ai-media/<path:filename>', methods=['GET'])
def serve_media(filename):
    fpath = os.path.join(TMP, os.path.basename(filename))
    if not os.path.exists(fpath):
        return jsonify({'error': '文件不存在'}), 404
    return send_file(fpath)


# ============================================================
# 支付（简化版：演示通道）
# ============================================================
PRICES = {
    'monthly': {'amount': '18.00', 'days': 31, 'subject': 'WebCut 专业版月度会员'},
    'yearly': {'amount': '128.00', 'days': 366, 'subject': 'WebCut 专业版年度会员'},
}

def _db_load():
    try:
        with open(PAY_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {'orders': {}, 'entitlements': {}}

def _db_save(db):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PAY_FILE + '.tmp', 'w', encoding='utf-8') as f:
        json.dump(db, f, ensure_ascii=False)
    os.replace(PAY_FILE + '.tmp', PAY_FILE)


@app.route('/api/orders', methods=['POST'])
def create_order():
    data = request.get_json(force=True, silent=True) or {}
    plan = (data.get('plan') or '').strip()
    cycle = (data.get('cycle') or '').strip()
    token = request.headers.get('X-User-Token', '')

    if plan != 'pro' or cycle not in PRICES:
        return jsonify({'error': '参数错误'}), 400

    order_id = 'wc' + uuid.uuid4().hex[:20]
    order = {'id': order_id, 'plan': 'pro', 'cycle': cycle, 'amount': PRICES[cycle]['amount'],
             'status': 'pending', 'provider': 'demo', 'token': token,
             'createdAt': time.time(), 'expireAt': time.time() + 900}

    with _db_lock:
        db = _db_load()
        db['orders'][order_id] = order
        _db_save(db)

    return jsonify({'orderId': order_id, 'payUrl': f'http://{request.host}/pay/{order_id}',
                    'amount': order['amount'], 'cycle': cycle, 'provider': 'demo',
                    'demo': True, 'expireAt': order['expireAt']})


@app.route('/api/orders/<order_id>/status', methods=['GET'])
def order_status(order_id):
    with _db_lock:
        db = _db_load()
    o = db['orders'].get(order_id)
    if not o:
        return jsonify({'error': '订单不存在'}), 404
    if o['status'] == 'pending' and time.time() > o['expireAt']:
        o['status'] = 'expired'
        with _db_lock:
            _db_save(db)
    return jsonify({'orderId': o['id'], 'status': o['status'], 'amount': o['amount']})


@app.route('/api/orders/<order_id>/mock-pay', methods=['POST'])
def mock_pay(order_id):
    with _db_lock:
        db = _db_load()
        o = db['orders'].get(order_id)
        if not o:
            return jsonify({'error': '订单不存在'}), 404
        if o['provider'] != 'demo':
            return jsonify({'error': '真实支付订单不可模拟'}), 400
        if o['status'] == 'pending' and time.time() <= o['expireAt']:
            o['status'] = 'paid'
            o['paidAt'] = time.time()
            token = o.get('token', '')
            if token:
                days = PRICES[o['cycle']]['days']
                ent = db['entitlements'].get(token) or {}
                base = max(time.time(), ent.get('expiresAt', 0))
                db['entitlements'][token] = {'plan': 'pro', 'expiresAt': base + days * 86400}
            _db_save(db)
    return jsonify({'orderId': o['id'], 'status': o['status']})


@app.route('/api/me/entitlements', methods=['GET'])
def get_entitlements():
    token = request.headers.get('X-User-Token', '')
    with _db_lock:
        db = _db_load()
    ent = db['entitlements'].get(token) if token else None
    if ent and ent.get('expiresAt', 0) > time.time():
        return jsonify({'plan': 'pro', 'expiresAt': ent['expiresAt'],
                        'limits': {'maxTimelines': 50, 'exportResolution': 2160, 'watermark': False}})
    return jsonify({'plan': 'free',
                    'limits': {'maxTimelines': 1, 'exportResolution': 720, 'watermark': True}})


# ============================================================
# 云同步
# ============================================================
@app.route('/api/cloud/status', methods=['GET'])
def cloud_status():
    token = request.headers.get('X-User-Token', '')
    if not token:
        return jsonify({'error': '缺少 Token'}), 401
    root = os.path.join(CLOUD_DIR, hashlib.sha256(token.encode()).hexdigest()[:32])
    proot = os.path.join(root, 'project.json')
    try:
        with open(proot, encoding='utf-8') as f:
            rec = json.load(f)
        return jsonify({'exists': True, 'updatedAt': rec.get('updatedAt'),
                        'clipCount': len(rec.get('clips', [])), 'name': rec.get('name')})
    except Exception:
        return jsonify({'exists': False})


@app.route('/api/cloud/project', methods=['GET', 'PUT'])
def cloud_project():
    token = request.headers.get('X-User-Token', '')
    if not token:
        return jsonify({'error': '缺少 Token'}), 401
    root = os.path.join(CLOUD_DIR, hashlib.sha256(token.encode()).hexdigest()[:32])
    proot = os.path.join(root, 'project.json')

    if request.method == 'GET':
        try:
            with open(proot, 'rb') as f:
                return send_file(proot, mimetype='application/json')
        except Exception:
            return jsonify({'error': '云端暂无备份'}), 404

    if request.method == 'PUT':
        try:
            raw = request.get_data()
            json.loads(raw.decode('utf-8'))  # 验证 JSON
            os.makedirs(root, exist_ok=True)
            with open(proot + '.tmp', 'wb') as f:
                f.write(raw)
            os.replace(proot + '.tmp', proot)
            print(f'[cloud] project saved ({len(raw)//1024}KB)', flush=True)
            return jsonify({'ok': True})
        except Exception as e:
            return jsonify({'error': str(e)}), 400


@app.route('/api/cloud/media/<sid>', methods=['GET', 'PUT'])
@app.route('/api/cloud/media/<sid>/<sub>', methods=['GET', 'PUT'])
def cloud_media(sid, sub=None):
    token = request.headers.get('X-User-Token', '')
    if not token:
        return jsonify({'error': '缺少 Token'}), 401
    if '/' in sid or '\\' in sid or sid.startswith('.'):
        return jsonify({'error': '非法 ID'}), 400

    root = os.path.join(CLOUD_DIR, hashlib.sha256(token.encode()).hexdigest()[:32], 'media')
    suffix = '.audio.wav' if sub == 'audio' else '.bin'
    fpath = os.path.join(root, sid + suffix)

    if request.method == 'PUT':
        data = request.get_data()
        if not data:
            return jsonify({'error': '空数据'}), 400
        os.makedirs(root, exist_ok=True)
        with open(fpath + '.tmp', 'wb') as f:
            f.write(data)
        os.replace(fpath + '.tmp', fpath)
        meta = {'name': request.headers.get('X-Media-Name', sid),
                'mime': request.headers.get('X-Media-Mime', 'application/octet-stream')}
        with open(fpath + '.json', 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False)
        print(f'[cloud] media saved: {sid} ({len(data)//1024}KB)', flush=True)
        return jsonify({'ok': True})

    if request.method == 'GET':
        try:
            return send_file(fpath)
        except Exception:
            return jsonify({'error': '媒体不存在'}), 404


@app.route('/api/cloud/usage', methods=['GET'])
def cloud_usage():
    token = request.headers.get('X-User-Token', '')
    if not token:
        return jsonify({'error': '缺少 Token'}), 401
    root = os.path.join(CLOUD_DIR, hashlib.sha256(token.encode()).hexdigest()[:32])
    used = 0
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if not fn.endswith('.tmp'):
                try:
                    used += os.path.getsize(os.path.join(dirpath, fn))
                except Exception:
                    pass
    return jsonify({'used': used, 'quota': 2 * 1024 * 1024 * 1024})


# ============================================================
# 静态文件（编辑器前端）
# ============================================================
@app.route('/', defaults={'path': 'index.html'})
@app.route('/<path:path>')
def serve_static(path):
    fpath = os.path.abspath(os.path.join(ROOT, path))
    if not fpath.startswith(os.path.abspath(ROOT)) or not os.path.isfile(fpath):
        return jsonify({'error': 'not found'}), 404
    return send_file(fpath)


# ============================================================
# 启动
# ============================================================
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
