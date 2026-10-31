# ============================================================
# WebCut 后端 — Flask 版
# 比原 http.server 版更可靠（requests 库自动处理编码/重试）
#
# 启动：python server.py（或 docker-compose up）
# 端口：8787
# ============================================================
import sys
import io
import os

# 强制 stdout/stderr 使用 UTF-8（修复 Docker 容器内 latin-1 编码错误）
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
os.environ['PYTHONIOENCODING'] = 'utf-8'

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import json
import time
import uuid
import hashlib
import threading
import requests as req_lib
import re as _re

app = Flask(__name__)
CORS(app)  # 允许所有来源跨域（Netlify 前端需要）

# ============================================================
# 配置
# ============================================================
PORT = int(os.environ.get('PORT', '8787'))
_STARTED = time.time()

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
WANX_I2V_MODEL = cfg('AI_I2V_MODEL', 'wanx2.1-i2v-plus')
WANX_T2I_MODEL = cfg('AI_T2I_MODEL', 'wanx2.1-t2i-turbo')
QWEN_MODEL = cfg('AI_TEXT_MODEL', 'qwen-plus')

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
TMP = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'tmp')
DATA_DIR = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'data')
CLOUD_DIR = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'cloud')
PAY_FILE = os.path.join(DATA_DIR, 'payments.json')
os.makedirs(TMP, exist_ok=True)

TASKS = {}  # AI 生成任务
TASKS_LOCK = threading.Lock()  # Flask threaded=True 下保护 TASKS 并发访问
_db_lock = threading.Lock()

print('=== WebCut 后端启动 ===')
print('Port:', PORT)
print('DASHSCOPE_API_KEY:', '已配置(' + KEY[:10] + '...)' if KEY else '!!未配置!!')
print('DASHSCOPE_BASE:', DASHSCOPE_BASE)
print('Model:', WANX_MODEL)
print('======================')


# ============================================================
# API 限流（令牌桶，按 IP）—— 防止公网刷接口烧 DashScope 按量额度
# 额度（真实成本约束）：视频生成是最贵操作，严格限
# ============================================================
import collections

_RATE_RULES = {  # 端点前缀 → (次数, 窗口秒)
    '/api/ai/video': (2, 600),      # 2 次 / 10 分钟
    '/api/ai/image': (6, 600),      # 6 次 / 10 分钟
    '/api/ai/tts': (10, 600),       # 10 次 / 10 分钟
    '/api/ai/prompt': (30, 600),    # 30 次 / 10 分钟
    '/api/ai/storyboard': (10, 600),  # 10 次 / 10 分钟
    '/api/auth/register': (10, 3600),  # 注册限流：10 次 / 小时
}
_rate_hits = collections.defaultdict(collections.deque)  # (ip, prefix) → [timestamps]
_rate_lock = threading.Lock()

def _rate_check():
    """命中限流返回 (True, 重试等待秒)；否则记录并返回 (False, 0)"""
    path = request.path
    rule = None
    for prefix, cfg2 in _RATE_RULES.items():
        if path.startswith(prefix):
            rule = cfg2
            break
    if not rule:
        return (False, 0)
    ip = (request.headers.get('X-Forwarded-For') or request.remote_addr or 'unknown').split(',')[0].strip()
    limit, window = rule
    now = time.time()
    key = (ip, prefix)
    with _rate_lock:
        dq = _rate_hits[key]
        while dq and dq[0] <= now - window:
            dq.popleft()
        if len(dq) >= limit:
            retry = int(window - (now - dq[0])) + 1
            return (True, retry)
        dq.append(now)
        # 防内存膨胀：全表过大时清理空队列
        if len(_rate_hits) > 4096:
            for k in [k2 for k2, v in _rate_hits.items() if not v]:
                _rate_hits.pop(k, None)
    return (False, 0)


@app.before_request
def _enforce_rate_limit():
    hit, retry = _rate_check()
    if hit:
        resp = jsonify({'error': f'请求过于频繁（限流：每 10 分钟内该类操作有次数上限），请约 {retry} 秒后重试'})
        resp.status_code = 429
        resp.headers['Retry-After'] = str(retry)
        return resp


def _translate_upstream_error(text):
    """把阿里云原始错误翻译成用户可操作的提示（不泄露内部细节）"""
    t = text or ''
    if 'InvalidApiKey' in t or 'Invalid API-key' in t:
        return ('API Key 已失效或被轮换——请在阿里云百炼控制台 (bailian.console.aliyun.com) '
                '重新生成 API-KEY，更新 ai-backend/.env 的 DASHSCOPE_API_KEY 后重启后端')
    if 'Throttling' in t or 'RateLimit' in t:
        return ('请求过于频繁（上游限流）——请稍等 1-2 分钟后重试')
    if 'Arrearage' in t or '欠费' in t:
        return ('账户欠费或额度用尽——请在阿里云费用中心检查百炼服务余额')
    if 'ModelNotExist' in t or 'UnsupportedOperation' in t:
        return ('当前工作区不支持该模型——请在百炼控制台确认模型开通状态')
    return None


# ============================================================
# 健康检查
# ============================================================
@app.route('/api/ai/health', methods=['GET'])
def health():
    return jsonify({
        'available': True,
        'video': bool(KEY),
        'image': bool(KEY),
        'prompt': bool(KEY),
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

        # 图生视频：请求带 image（data URL 或公网 URL）时走 i2v 模型
        image_ref = (data.get('image') or '').strip()
        is_i2v = bool(image_ref)
        if is_i2v and not (image_ref.startswith('data:image/') or image_ref.startswith('http://') or image_ref.startswith('https://')):
            return jsonify({'error': 'image 需为 data:image/... Base64 或公网 URL'}), 400

        # 调用阿里云万相 API
        url = DASHSCOPE_BASE + '/api/v1/services/aigc/video-generation/video-synthesis'
        headers = {
            'Authorization': 'Bearer ' + KEY,
            'Content-Type': 'application/json',
            'X-DashScope-Async': 'enable'
        }
        model = WANX_I2V_MODEL if is_i2v else WANX_MODEL
        inp = {'prompt': prompt}
        if is_i2v:
            inp['img_url'] = image_ref
        payload = {
            'model': model,
            'input': inp,
            'parameters': {'size': '960*960', 'duration': duration}
        }

        print(f'>>> [AI] POST {url} <<<', flush=True)
        resp = req_lib.post(url, headers=headers, json=payload, timeout=30)
        print(f'>>> [AI] 阿里云返回: {resp.status_code} <<<', flush=True)

        if resp.status_code != 200:
            print(f'>>> [AI] 错误响应: {resp.text[:300]} <<<', flush=True)
            human = _translate_upstream_error(resp.text)
            return jsonify({'error': human or f'阿里云返回 {resp.status_code}: {resp.text[:200]}'}), 502

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

        u = _auth_user()
        if u and not _spend_credits(u['id'], CREDIT_COST['video']):
            return jsonify({'error': f'AI 积分不足（本次需 {CREDIT_COST["video"]}，余额 {u["ai_credits"]}）——可在个人中心查看套餐'}), 402
        threading.Thread(target=poll, daemon=True).start()
        return jsonify({'taskId': local_id})

    except Exception as e:
        import traceback
        print(f'!!!! [AI] 致命异常: {e} !!!!\n{traceback.format_exc()}', flush=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/ai/video/<task_id>', methods=['GET'])
def get_video_status(task_id):
    with TASKS_LOCK:
        t = TASKS.get(task_id)
        if not t:
            return jsonify({'error': '任务不存在'}), 404
        # 返回快照，避免轮询线程在序列化时并发修改
        return jsonify({
            'status': t['status'], 'progress': t['progress'], 'stage': t['stage'],
            'error': t['error'],
            'url': ('/ai-media/' + t['video_file']) if t.get('video_file') else None
        })


@app.route('/api/ai/task/<task_id>', methods=['GET'])
def get_task_status_alias(task_id):
    return get_video_status(task_id)


@app.route('/api/ai/task/<task_id>', methods=['DELETE'])
def cancel_task_alias(task_id):
    return cancel_video_task(task_id)


@app.route('/api/ai/video/<task_id>', methods=['DELETE'])
def cancel_video_task(task_id):
    """取消本地任务：置 canceled 并尽力取消 DashScope 侧任务（已完成的不可取消，返回真实状态）"""
    with TASKS_LOCK:
        t = TASKS.get(task_id)
        if not t:
            return jsonify({'error': '任务不存在'}), 404
    ptid = t.get('provider_task_id')
    if ptid and KEY:
        try:
            req_lib.post(DASHSCOPE_BASE + '/api/v1/tasks/' + ptid + '/cancel',
                         headers={'Authorization': 'Bearer ' + KEY}, timeout=10)
        except Exception:
            pass  # 上游取消失败不影响本地标记；本地轮询照常结束
    with TASKS_LOCK:
        if t['status'] == 'running':
            t['status'] = 'canceled'
            t['stage'] = '已取消'
    return jsonify({'ok': True, 'status': t['status']})


# ============================================================
# AI 图片生成（万相文生图）
# ============================================================
T2I_SIZES = {'1:1': '1024*1024', '16:9': '1280*720', '9:16': '720*1280', '4:3': '1024*768', '3:4': '768*1024'}

@app.route('/api/ai/image', methods=['POST'])
def create_image_task():
    print('>>> [T2I] 收到图片生成请求 <<<', flush=True)
    try:
        data = request.get_json(force=True, silent=True) or {}
        prompt = (data.get('prompt') or '').strip()
        ratio = data.get('ratio') or '1:1'
        if not prompt:
            return jsonify({'error': 'prompt 不能为空'}), 400
        if not KEY:
            return jsonify({'error': '未配置 DASHSCOPE_API_KEY'}), 501
        size = T2I_SIZES.get(ratio, '1024*1024')

        url = DASHSCOPE_BASE + '/api/v1/services/aigc/text2image/image-synthesis'
        headers = {
            'Authorization': 'Bearer ' + KEY,
            'Content-Type': 'application/json',
            'X-DashScope-Async': 'enable'
        }
        payload = {'model': WANX_T2I_MODEL, 'input': {'prompt': prompt},
                   'parameters': {'size': size, 'n': 1}}
        resp = req_lib.post(url, headers=headers, json=payload, timeout=30)
        print(f'>>> [T2I] 阿里云返回: {resp.status_code} <<<', flush=True)
        if resp.status_code != 200:
            human = _translate_upstream_error(resp.text)
            return jsonify({'error': human or f'阿里云返回 {resp.status_code}: {resp.text[:200]}'}), 502
        task_id = (resp.json().get('output') or {}).get('task_id')
        if not task_id:
            return jsonify({'error': '未返回 task_id'}), 502

        local_id = str(uuid.uuid4())
        with TASKS_LOCK:
            TASKS[local_id] = {'id': local_id, 'status': 'running', 'progress': 0.1,
                               'stage': '生成中', 'provider_task_id': task_id,
                               'video_file': None, 'error': None}

        def poll():
            try:
                for _ in range(60):  # 图片生成一般 <2 分钟
                    time.sleep(3)
                    pr = req_lib.get(DASHSCOPE_BASE + '/api/v1/tasks/' + task_id,
                                     headers={'Authorization': 'Bearer ' + KEY}, timeout=15)
                    if pr.status_code != 200:
                        continue
                    out = (pr.json().get('output') or {})
                    status = out.get('task_status', '')
                    with TASKS_LOCK:
                        t = TASKS.get(local_id)
                        if not t or t['status'] == 'canceled':
                            return
                        t['progress'] = 0.7 if status == 'RUNNING' else t['progress']
                    if status == 'SUCCEEDED':
                        results = out.get('results') or []
                        img_url = (results[0] or {}).get('url') if results else None
                        if img_url:
                            ir = req_lib.get(img_url, timeout=60)
                            if ir.status_code == 200:
                                fname = str(uuid.uuid4()) + '.png'
                                with open(os.path.join(TMP, fname), 'wb') as f:
                                    f.write(ir.content)
                                with TASKS_LOCK:
                                    t = TASKS.get(local_id)
                                    if t:
                                        t['video_file'] = fname  # 复用 video_file 字段存产物文件名
                                        t['status'] = 'done'
                                        t['progress'] = 1.0
                                return
                        with TASKS_LOCK:
                            t = TASKS.get(local_id)
                            if t:
                                t['status'] = 'failed'
                                t['error'] = '结果下载失败'
                        return
                    elif status == 'FAILED':
                        with TASKS_LOCK:
                            t = TASKS.get(local_id)
                            if t:
                                t['status'] = 'failed'
                                t['error'] = f"生成失败: {out.get('message', '未知原因')}"
                        return
                with TASKS_LOCK:
                    t = TASKS.get(local_id)
                    if t and t['status'] == 'running':
                        t['status'] = 'failed'
                        t['error'] = '生成超时'
            except Exception as e:
                with TASKS_LOCK:
                    t = TASKS.get(local_id)
                    if t:
                        t['status'] = 'failed'
                        t['error'] = str(e)

        u = _auth_user()
        if u and not _spend_credits(u['id'], CREDIT_COST['image']):
            return jsonify({'error': f'AI 积分不足（本次需 {CREDIT_COST["image"]}，余额 {u["ai_credits"]}）'}), 402
        threading.Thread(target=poll, daemon=True).start()
        return jsonify({'taskId': local_id})

    except Exception as e:
        print(f'!!!! [T2I] 异常: {e} !!!!', flush=True)
        return jsonify({'error': str(e)}), 500


# ============================================================
# AI 创意辅助（Qwen 提示词润色，同步返回）
# ============================================================
@app.route('/api/ai/prompt', methods=['POST'])
def enhance_prompt():
    print('>>> [QWEN] 收到提示词润色请求 <<<', flush=True)
    try:
        data = request.get_json(force=True, silent=True) or {}
        prompt = (data.get('prompt') or '').strip()
        kind = data.get('kind') or 'video'  # video | image
        if not prompt:
            return jsonify({'error': 'prompt 不能为空'}), 400
        if not KEY:
            return jsonify({'error': '未配置 DASHSCOPE_API_KEY'}), 501

        sysmsg = ('你是短视频与AI绘画提示词专家。把用户的简短想法扩写成一段中文视频生成提示词：'
                  '包含主体、场景、镜头运动、光线、氛围、风格。只输出提示词本身，80字以内。') \
            if kind == 'video' else \
            ('你是AI绘画提示词专家。把用户的简短想法扩写成一段中文图片生成提示词：'
             '包含主体细节、构图、光线、色调、艺术风格。只输出提示词本身，60字以内。')
        url = DASHSCOPE_BASE + '/api/v1/services/aigc/text-generation/generation'
        headers = {'Authorization': 'Bearer ' + KEY, 'Content-Type': 'application/json'}
        payload = {
            'model': QWEN_MODEL,
            'input': {'messages': [
                {'role': 'system', 'content': sysmsg},
                {'role': 'user', 'content': prompt}
            ]},
            'parameters': {'result_format': 'message', 'max_tokens': 200}
        }
        resp = req_lib.post(url, headers=headers, json=payload, timeout=60)
        if resp.status_code != 200:
            human = _translate_upstream_error(resp.text)
            return jsonify({'error': human or f'阿里云返回 {resp.status_code}: {resp.text[:200]}'}), 502
        txt = (((resp.json().get('output') or {}).get('choices') or [{}])[0].get('message') or {}).get('content')
        if not txt:
            return jsonify({'error': '模型未返回内容'}), 502
        return jsonify({'prompt': txt.strip()})
    except Exception as e:
        print(f'!!!! [QWEN] 异常: {e} !!!!', flush=True)
        return jsonify({'error': str(e)}), 500


# ============================================================
# AI 策划分镜（Qwen → 结构化分镜 JSON）
# ============================================================
@app.route('/api/ai/storyboard', methods=['POST'])
def create_storyboard():
    print('>>> [STORYBOARD] 收到分镜策划请求 <<<', flush=True)
    try:
        data = request.get_json(force=True, silent=True) or {}
        brief = (data.get('brief') or '').strip()
        shot_n = data.get('shots') or 5
        try:
            shot_n = max(3, min(8, int(shot_n)))
        except Exception:
            shot_n = 5
        if not brief:
            return jsonify({'error': 'brief 不能为空'}), 400
        if not KEY:
            return jsonify({'error': '未配置 DASHSCOPE_API_KEY'}), 501

        sysmsg = ('你是专业短视频导演与AI视频提示词专家。根据用户主题策划分镜脚本，'
                  '严格只输出如下 JSON（不要 markdown 代码块、不要解释文字）：'
                  '{"title":"视频标题","shots":[{"dur":4,"desc":"画面内容一句话",'
                  '"visualPrompt":"用于AI视频生成的中文提示词，含主体/场景/运镜/光线/氛围，40字以内",'
                  '"narration":"该镜头旁白台词，20字以内，可为空字符串"}]}。'
                  f'共 {shot_n} 个分镜，每个 dur 取 3-5。')
        url = DASHSCOPE_BASE + '/api/v1/services/aigc/text-generation/generation'
        headers = {'Authorization': 'Bearer ' + KEY, 'Content-Type': 'application/json'}
        payload = {
            'model': QWEN_MODEL,
            'input': {'messages': [
                {'role': 'system', 'content': sysmsg},
                {'role': 'user', 'content': brief[:500]}
            ]},
            'parameters': {'result_format': 'message', 'max_tokens': 900, 'temperature': 0.7}
        }
        resp = req_lib.post(url, headers=headers, json=payload, timeout=90)
        if resp.status_code != 200:
            human = _translate_upstream_error(resp.text)
            return jsonify({'error': human or f'阿里云返回 {resp.status_code}: {resp.text[:200]}'}), 502
        txt = (((resp.json().get('output') or {}).get('choices') or [{}])[0].get('message') or {}).get('content') or ''
        # 解析：剥掉可能的 markdown 包裹 / 前后杂质
        m = None
        import re as _re
        mm = _re.search(r'\{[\s\S]*\}', txt)
        if mm:
            m = mm.group(0)
        if not m:
            return jsonify({'error': '模型未返回 JSON：' + txt[:120]}), 502
        try:
            sb = json.loads(m)
        except Exception:
            return jsonify({'error': '分镜 JSON 解析失败：' + m[:160]}), 502
        shots = sb.get('shots') or []
        shots = [sh for sh in shots if isinstance(sh, dict) and sh.get('visualPrompt')]
        if not shots:
            return jsonify({'error': '分镜内容为空'}), 502
        for i, sh in enumerate(shots):
            try:
                sh['dur'] = max(2, min(6, float(sh.get('dur') or 4)))
            except Exception:
                sh['dur'] = 4
            sh['narration'] = (sh.get('narration') or '').strip()
            sh['desc'] = (sh.get('desc') or '').strip()
            sh['visualPrompt'] = (sh.get('visualPrompt') or '').strip()
        print(f'>>> [STORYBOARD] 生成 {len(shots)} 个分镜 <<<', flush=True)
        return jsonify({'title': (sb.get('title') or 'AI 分镜方案').strip()[:40], 'shots': shots})

    except Exception as e:
        print(f'!!!! [STORYBOARD] 异常: {e} !!!!', flush=True)
        return jsonify({'error': str(e)}), 500



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
    # basename 已经防了目录穿越，再加 realpath 双保险
    safe_name = os.path.basename(filename)
    if not safe_name or safe_name.startswith('.'):
        return jsonify({'error': '文件不存在'}), 404
    fpath = os.path.realpath(os.path.join(TMP, safe_name))
    if not fpath.startswith(os.path.realpath(TMP) + os.sep) or not os.path.isfile(fpath):
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
                u = _auth_user()
                if u:  # 登录用户 → SQLite 套餐
                    con = _db()
                    base = max(time.time(), u['plan_expires'])
                    con.execute('UPDATE users SET plan=?, plan_expires=? WHERE id=?',
                                ('pro', base + days * 86400, u['id']))
                    con.commit(); con.close()
                else:
                    ent = db['entitlements'].get(token) or {}
                    base = max(time.time(), ent.get('expiresAt', 0))
                    db['entitlements'][token] = {'plan': 'pro', 'expiresAt': base + days * 86400}
            _db_save(db)
    return jsonify({'orderId': o['id'], 'status': o['status']})


@app.route('/api/me/entitlements', methods=['GET'])
def get_entitlements():
    token = request.headers.get('X-User-Token', '')
    u = _auth_user()
    if u:
        return jsonify({'plan': _user_plan(u), 'email': u['email'],
                        'aiCredits': u['ai_credits'], 'limits': _user_limits(u)})
    with _db_lock:
        db = _db_load()
    ent = db['entitlements'].get(token) if token else None
    if ent and ent.get('expiresAt', 0) > time.time():
        return jsonify({'plan': 'pro', 'expiresAt': ent['expiresAt'],
                        'limits': {'maxTimelines': 50, 'exportResolution': 2160, 'watermark': False}})
    return jsonify({'plan': 'free',
                    'limits': {'maxTimelines': 1, 'exportResolution': 720, 'watermark': True}})


# ============================================================
# 用户体系（v1.0）—— SQLite + PBKDF2 + AI 积分 + 云数据按用户隔离
# 旧式匿名 token（无 session 记录）→ 兼容走原文件哈希目录，老用户无感
# ============================================================
import sqlite3
import secrets as _secrets
import hashlib as _hashlib

DB_PATH = os.path.join(DATA_DIR, 'webcut.db')

def _db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.execute('PRAGMA journal_mode=WAL')
    return con

def _db_init():
    os.makedirs(DATA_DIR, exist_ok=True)
    con = _db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      email TEXT UNIQUE NOT NULL,
      pw_salt TEXT NOT NULL, pw_hash TEXT NOT NULL,
      plan TEXT DEFAULT 'free', plan_expires REAL DEFAULT 0,
      ai_credits INTEGER DEFAULT 100,
      created_at REAL
    );
    CREATE TABLE IF NOT EXISTS sessions(
      token TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
      created_at REAL, last_seen REAL
    );
    CREATE TABLE IF NOT EXISTS cloud_projects(
      user_id INTEGER NOT NULL, project_key TEXT NOT NULL DEFAULT 'main',
      name TEXT, data TEXT, updated_at REAL,
      PRIMARY KEY(user_id, project_key)
    );
    """)
    con.commit(); con.close()

_db_init()

def _pw_hash(pw, salt):
    return _hashlib.pbkdf2_hmac('sha256', pw.encode('utf-8'), bytes.fromhex(salt), 100000).hex()

def _auth_user():
    """X-User-Token → session 用户；无 session 返回 None（匿名兼容模式）"""
    token = request.headers.get('X-User-Token', '')
    if not token:
        return None
    con = _db()
    row = con.execute('SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?',
                      (token,)).fetchone()
    con.close()
    if not row:
        return None
    return {'id': row[0], 'email': row[1], 'plan': row[4],
            'plan_expires': row[5], 'ai_credits': row[6]}

def _user_plan(u):
    if not u:
        return 'free'
    return u['plan'] if u['plan_expires'] > time.time() else 'free'

def _user_limits(u):
    plan = _user_plan(u)
    if plan == 'pro':
        return {'maxTimelines': 50, 'exportResolution': 2160, 'watermark': False}
    return {'maxTimelines': 1, 'exportResolution': 720, 'watermark': True}

def _spend_credits(user_id, amount):
    """AI 积分扣减（余额不足返回 False）"""
    con = _db()
    row = con.execute('SELECT ai_credits FROM users WHERE id=?', (user_id,)).fetchone()
    if not row or row[0] < amount:
        con.close()
        return False
    con.execute('UPDATE users SET ai_credits=ai_credits-? WHERE id=?', (amount, user_id))
    con.commit(); con.close()
    return True

CREDIT_COST = {'video': 20, 'image': 5, 'tts': 2, 'storyboard': 1}

@app.route('/api/auth/register', methods=['POST'])
def auth_register():
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    pw = data.get('password') or ''
    if not _re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        return jsonify({'error': '邮箱格式不正确'}), 400
    if len(pw) < 6:
        return jsonify({'error': '密码至少 6 位'}), 400
    con = _db()
    if con.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone():
        con.close()
        return jsonify({'error': '该邮箱已注册'}), 409
    salt = _secrets.token_hex(16)
    con.execute('INSERT INTO users(email,pw_salt,pw_hash,created_at) VALUES(?,?,?,?)',
                (email, salt, _pw_hash(pw, salt), time.time()))
    uid = con.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone()[0]
    token = _secrets.token_hex(32)
    con.execute('INSERT INTO sessions(token,user_id,created_at,last_seen) VALUES(?,?,?,?)',
                (token, uid, time.time(), time.time()))
    con.commit(); con.close()
    print(f'[auth] 注册: {email}', flush=True)
    return jsonify({'token': token, 'user': {'email': email, 'plan': 'free', 'aiCredits': 100}})

@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    pw = data.get('password') or ''
    con = _db()
    row = con.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
    if not row:
        con.close()
        return jsonify({'error': '账号或密码错误'}), 401
    if _pw_hash(pw, row[2]) != row[3]:
        con.close()
        return jsonify({'error': '账号或密码错误'}), 401
    token = _secrets.token_hex(32)
    con.execute('INSERT INTO sessions(token,user_id,created_at,last_seen) VALUES(?,?,?,?)',
                (token, row[0], time.time(), time.time()))
    con.commit(); con.close()
    plan = row[4] if row[5] > time.time() else 'free'
    print(f'[auth] 登录: {email}', flush=True)
    return jsonify({'token': token, 'user': {'email': email, 'plan': plan, 'aiCredits': row[6]}})

@app.route('/api/auth/logout', methods=['POST'])
def auth_logout():
    token = request.headers.get('X-User-Token', '')
    con = _db()
    con.execute('DELETE FROM sessions WHERE token=?', (token,))
    con.commit(); con.close()
    return jsonify({'ok': True})

@app.route('/api/auth/me', methods=['GET'])
def auth_me():
    u = _auth_user()
    if not u:
        return jsonify({'error': '未登录'}), 401
    return jsonify({'user': {'email': u['email'], 'plan': _user_plan(u),
                             'aiCredits': u['ai_credits'], 'limits': _user_limits(u)}})

@app.route('/api/status', methods=['GET'])
def svc_status():
    tasks_running = 0
    with TASKS_LOCK:
        tasks_running = sum(1 for t in TASKS.values() if t['status'] == 'running')
    db_size = 0
    try:
        db_size = os.path.getsize(DB_PATH)
    except Exception:
        pass
    return jsonify({'version': '1.0.0', 'uptime': round(time.time() - _STARTED, 1),
                    'tasksRunning': tasks_running, 'dbSize': db_size})

# ============================================================
# 云同步
# ============================================================
@app.route('/api/cloud/status', methods=['GET'])
def cloud_status():
    token = request.headers.get('X-User-Token', '')
    if not token:
        return jsonify({'error': '缺少 Token'}), 401
    u = _auth_user()
    if u:  # 登录用户：云端工程存 SQLite（按 user_id 隔离）
        con = _db()
        row = con.execute('SELECT name,data,updated_at FROM cloud_projects WHERE user_id=? AND project_key=?',
                          (u['id'], 'main')).fetchone()
        con.close()
        if not row:
            return jsonify({'exists': False})
        try:
            rec = json.loads(row[1])
            return jsonify({'exists': True, 'updatedAt': rec.get('updatedAt', row[2]),
                            'clipCount': len(rec.get('clips', [])), 'name': row[0] or rec.get('name')})
        except Exception:
            return jsonify({'exists': False})
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
def cloud_project_user_aware():
    u = _auth_user()
    if u:
        con = _db()
        if request.method == 'GET':
            row = con.execute('SELECT data FROM cloud_projects WHERE user_id=? AND project_key=?',
                              (u['id'], 'main')).fetchone()
            con.close()
            if not row:
                return jsonify({'error': '云端暂无备份'}), 404
            return app.response_class(row[0], mimetype='application/json')
        raw = request.get_data()
        json.loads(raw.decode('utf-8'))
        rec = json.loads(raw.decode('utf-8'))
        con.execute('INSERT INTO cloud_projects(user_id,project_key,name,data,updated_at) VALUES(?,?,?,?,?) '
                    'ON CONFLICT(user_id,project_key) DO UPDATE SET name=excluded.name,data=excluded.data,updated_at=excluded.updated_at',
                    (u['id'], 'main', rec.get('name'), raw.decode('utf-8'), time.time()))
        con.commit(); con.close()
        print(f'[cloud] 用户工程已保存 ({len(raw)//1024}KB)', flush=True)
        return jsonify({'ok': True})
    return cloud_project()

def cloud_project_legacy():
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
    out = {'used': used, 'quota': 2 * 1024 * 1024 * 1024}
    u = _auth_user()
    if u:
        con = _db()
        row = con.execute('SELECT data FROM cloud_projects WHERE user_id=? AND project_key=?',
                          (u['id'], 'main')).fetchone()
        con.close()
        out['used'] = len(row[0].encode('utf-8')) if row else 0
        out['plan'] = _user_plan(u)
        out['aiCredits'] = u['ai_credits']
        out['email'] = u['email']
    return jsonify(out)


# ============================================================
# 静态文件（编辑器前端）—— 含路径遍历防护
# ============================================================
# 静态服务敏感路径黑名单：后端配置/数据/代码/临时目录绝不通过静态服务暴露
_STATIC_DENY = ('ai-backend', '.git', '.zcode', 'node_modules')
_STATIC_DENY_EXT = ('.env', '.py', '.json', '.md', '.tar.gz', '.yml', '.yaml',
                    '.toml', '.db', '.sqlite', '.log', '.bat', '.sh', '.ps1')

@app.route('/', defaults={'path': 'index.html'})
@app.route('/<path:path>')
def serve_static(path):
    # 路径遍历防护：拒绝 .. 、反斜杠、空字节；解析后必须在 ROOT 内
    if '..' in path or '\\' in path or '\x00' in path:
        return jsonify({'error': 'not found'}), 404
    norm = path.replace('\\', '/')
    parts = [p for p in norm.split('/') if p]
    # 首段目录黑名单（ai-backend/.git 等）与敏感扩展名黑名单
    if parts and parts[0].lower() in _STATIC_DENY:
        return jsonify({'error': 'not found'}), 404
    low = norm.lower()
    if any(low.endswith(ext) for ext in _STATIC_DENY_EXT):
        return jsonify({'error': 'not found'}), 404
    fpath = os.path.abspath(os.path.join(ROOT, path))
    # 双重校验：abspath 前缀 + realpath（防符号链接逃逸）
    real_root = os.path.realpath(ROOT)
    real_path = os.path.realpath(fpath)
    if not real_path.startswith(real_root + os.sep) or not os.path.isfile(real_path):
        return jsonify({'error': 'not found'}), 404
    return send_file(fpath)


# ============================================================
# 启动
# ============================================================
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
