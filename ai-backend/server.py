# ============================================================
# WebCut AI 后端骨架（阶段二 · 一句话生成视频）—— Python 版
# 与 ai-backend/server.js（Node 版）路由/行为一一对应，二选一运行。
# 零第三方依赖：仅 Python 3.9+ 标准库。
#
# 启动：
#   set DASHSCOPE_API_KEY=sk-xxx   （Windows CMD）
#   python server.py               → http://127.0.0.1:8787
# （同时静态托管 ../video-editor，编辑器与本 API 同源，无 CORS 问题）
#
# 对接的第三方：阿里云百炼
#   - 文生视频：通义万相 wanx2.1-t2v（异步任务：创建→轮询→下载）
#   - 语音合成：CosyVoice cosyvoice-v2（同步 HTTP，返回音频 URL）
#   即梦 / 可灵同理，只需替换 create_wanx_task / poll_wanx_task。
#
# 路由：
#   GET  /api/ai/health        能力位 {available, video, tts, provider}
#   POST /api/ai/video         创建文生视频任务 {prompt, duration}
#   GET  /api/ai/video/:id     轮询任务（内部代轮询+下载到临时目录）
#   POST /api/ai/tts           CosyVoice 语音合成 {text, voice?}
#                              （未配置 Key 时返回本地合成演示音，链路仍可测）
#   GET  /ai-media/:file       提供临时目录中的 MP4 / WAV
#
# TODO(支付接入): 生成属于付费能力时，创建任务前走 /api/orders 校验权益。
# ============================================================
import json
import os
import struct
import base64
import hashlib
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
import io
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:  # 二维码渲染（纯 Python PNG，无 PIL 依赖）
    import qrcode as _qrcode
    import qrcode.image.pure as _qr_pure
except Exception:
    _qrcode = None

try:  # 支付宝 RSA2 签名
    from cryptography.hazmat.primitives import hashes as _hashes, serialization as _ser
    from cryptography.hazmat.primitives.asymmetric import padding as _padding
    _CRYPTO = True
except Exception:
    _CRYPTO = False

PORT = int(os.environ.get('PORT', '8787'))


def _load_env_file():
    """读取 ai-backend/.env（KEY=VALUE 每行一条），返回 dict。
    系统环境变量优先于 .env；.env 方式可绕开 Windows 环境变量继承问题。"""
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


_ENV_FILE = _load_env_file()


def cfg(name, default=''):
    """配置读取：系统环境变量 > .env 文件 > 默认值"""
    return os.environ.get(name, '') or _ENV_FILE.get(name, '') or default


KEY = cfg('DASHSCOPE_API_KEY')

# DashScope API 基础地址：
#   默认业务空间 Key（sk-ws-xxx 在默认空间生成）→ https://dashscope.aliyuncs.com
#   专属业务空间 Key → https://{workspace-id}.{region}.maas.aliyuncs.com
# 在 .env 中配置 DASHSCOPE_BASE_URL 可覆盖
DASHSCOPE_BASE = cfg('DASHSCOPE_BASE_URL', 'https://dashscope.aliyuncs.com')
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))  # ai-backend 的上一级就是 video-editor
TMP = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'tmp')
os.makedirs(TMP, exist_ok=True)

TASKS = {}
MIME = {'.html': 'text/html', '.js': 'text/javascript', '.mp4': 'video/mp4',
        '.wav': 'audio/wav', '.mp3': 'audio/mpeg', '.png': 'image/png', '.jpg': 'image/jpeg'}


# ---------- 带重试的 HTTP 请求（指数退避，最多 3 次；5xx 才重试） ----------
def fetch_with_retry(url, data=None, headers=None, retries=3, timeout=30):
    last_err = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers or {})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            if e.code >= 500 and i < retries - 1:
                last_err = e
                time.sleep(0.8 * (2 ** i))
                continue
            return e.code, e.read()
        except Exception as e:  # 网络错误/超时
            last_err = e
            time.sleep(0.8 * (2 ** i))
    raise last_err


def read_json(raw):
    try:
        return json.loads(raw.decode('utf-8'))
    except Exception:
        return {}


# ---------- 通义万相：创建任务 ----------
# 模型默认 wanx2.1-t2v-plus（用户已确认该模型有免费额度 200 次）；
# 可通过环境变量 AI_VIDEO_MODEL 覆盖（如 wanx2.1-t2v-turbo）
WANX_MODEL = os.environ.get('AI_VIDEO_MODEL', 'wanx2.1-t2v-plus')


def create_wanx_task(prompt, duration):
    payload = json.dumps({
        'model': WANX_MODEL,
        'input': {'prompt': prompt},
        'parameters': {'size': '960*960', 'duration': duration or 4}
    }, ensure_ascii=False).encode('utf-8')
    code, raw = fetch_with_retry(
        DASHSCOPE_BASE + '/api/v1/services/aigc/video-generation/video-synthesis',
        data=payload,
        headers={'Authorization': 'Bearer ' + KEY,
                 'Content-Type': 'application/json; charset=utf-8',
                 'X-DashScope-Async': 'enable'})
    j = read_json(raw)
    task_id = (j.get('output') or {}).get('task_id')
    if not task_id:
        raise RuntimeError('创建任务失败：' + json.dumps(j, ensure_ascii=False)[:200])
    return task_id


def poll_wanx_task(task_id):
    code, raw = fetch_with_retry(
        DASHSCOPE_BASE + '/api/v1/tasks/' + task_id,
        headers={'Authorization': 'Bearer ' + KEY})
    j = read_json(raw)
    out = j.get('output') or {}
    st = out.get('task_status', '')
    return {
        'status': 'done' if st == 'SUCCEEDED' else ('failed' if st == 'FAILED' else 'running'),
        'progress': 1.0 if st == 'SUCCEEDED' else (0.7 if st == 'RUNNING' else 0.3),
        'video_url': out.get('video_url'),
        # 失败原因原样透传（额度/审核/参数等），联调与用户提示都依赖它
        'err_code': out.get('code'),
        'err_msg': out.get('message'),
    }


def download_to_tmp(url, ext='mp4'):
    code, data = fetch_with_retry(url, retries=2, timeout=120)
    name = str(uuid.uuid4()) + '.' + ext
    with open(os.path.join(TMP, name), 'wb') as f:
        f.write(data)
    return name


# ---------- CosyVoice 语音合成（返回音频 URL 后下载） ----------
# 端点（2026-10 联调实测）：/api/v1/services/audio/tts/SpeechSynthesizer，
# 旧 multimodal-generation 端点对 cosyvoice-v2 已失效（url error）。
# 常用音色：longxiaochun_v2 / longwan_v2 / longcheng_v2 / loongbella_v2 …
def create_cosyvoice_speech(text, voice):
    payload = json.dumps({
        'model': 'cosyvoice-v2',
        'input': {'text': text[:2000], 'voice': voice or 'longxiaochun_v2', 'format': 'mp3'}
    }, ensure_ascii=False).encode('utf-8')
    code, raw = fetch_with_retry(
        DASHSCOPE_BASE + '/api/v1/services/audio/tts/SpeechSynthesizer',
        data=payload,
        headers={'Authorization': 'Bearer ' + KEY,
                 'Content-Type': 'application/json; charset=utf-8'})
    j = read_json(raw)
    if j.get('code'):
        raise RuntimeError(str(j.get('code')) + ' ' + str(j.get('message', ''))[:160])  # 鉴权失败等
    url = ((j.get('output') or {}).get('audio') or {}).get('url')
    if not url:
        raise RuntimeError('CosyVoice 未返回音频 URL')
    return url


# ---------- 本地演示旁白：合成 16-bit WAV（无 Key 时的链路测试音） ----------
# 按字符生成带包络的音节（基频随字符码变化），时长 ≈ 0.16s/字 + 0.6s 尾静音，
# 足以验证整条 TTS 链路（HTTP→下载→前端解码→挂载→音画同步）。
def synth_wav(text):
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
            env = 1 - abs(1 - 2 * k / n)  # 三角形音节包络
            v = (0.75 * ((2 * 3.141592653589793 * f0 * t) % 1.0 - 0.5) * 2 +
                 0.25 * ((2 * 3.141592653589793 * f0 * 2.02 * t) % 1.0 - 0.5) * 2) * 0.4 * env
            s = int(max(-1.0, min(1.0, v)) * 32767)
            struct.pack_into('<h', pcm, (start + k) * 2, s)
    return wav_bytes(pcm, sr, 1)


def wav_bytes(pcm, sr, channels):
    header = b'RIFF' + struct.pack('<I', 36 + len(pcm)) + b'WAVE' + b'fmt ' + \
        struct.pack('<IHHIIHH', 16, 1, channels, sr, sr * channels * 2, channels * 2, 16) + \
        b'data' + struct.pack('<I', len(pcm))
    return header + bytes(pcm)


def save_tmp(data, ext):
    name = str(uuid.uuid4()) + '.' + ext
    with open(os.path.join(TMP, name), 'wb') as f:
        f.write(data)
    return name


# ---------- 服务端代为轮询：每 3s 查一次万相，成功后下载到本地（10 分钟上限） ----------
def poll_loop(task_id, provider_task_id):
    t = TASKS[task_id]
    deadline = time.time() + 600
    print('[ai] provider task:', provider_task_id)  # 联调：落日志便于追查供应商任务

    def tick():
        if t['status'] not in ('running',):
            return
        if time.time() > deadline:
            t['status'] = 'failed'
            t['error'] = '生成超时（10 分钟），供应商任务可能仍在排队，请稍后重试'
            print('[ai] task timeout:', task_id)
            return
        try:
            r = poll_wanx_task(provider_task_id)
            t['progress'] = r['progress']
            t['stage'] = '下载中' if r['status'] == 'done' else '生成中'
            if r['status'] == 'done':
                t['video_file'] = download_to_tmp(r['video_url'], 'mp4')
                t['progress'] = 1.0
                t['status'] = 'done'
                print('[ai] task done ->', t['video_file'])
            elif r['status'] == 'failed':
                t['status'] = 'failed'
                t['error'] = '供应商生成失败：{} {}'.format(r.get('err_code') or '', r.get('err_msg') or '').strip()
            else:
                threading.Timer(3.0, tick).start()
        except Exception as e:  # 轮询失败不立刻判死，前端超时兜底
            print('[ai] poll error:', e)
            threading.Timer(3.0, tick).start()
    tick()


# ============================================================
# 支付与权益（真实订单架构）
#   订单：POST /api/orders {plan,cycle}          → {orderId,payUrl,expireAt,demo,amount}
#         GET  /api/orders/{id}/status            → {status: pending|paid|expired}
#         GET  /api/orders/{id}/qr.png            → 真二维码 PNG（渲染 payUrl）
#         POST /api/orders/{id}/mock-pay          → 演示通道专用（真实通道下拒绝）
#   权益：GET  /api/me/entitlements（X-User-Token）→ {plan, limits, expiresAt}
#   通道 Provider：alipay（当面付 precreate/query，配好 .env 三项自动启用）
#                  demo（无商户凭证：同一套订单/权益流程，付款动作用 mock-pay 替代）
#   持久化：ai-backend/data/payments.json（订单 + 按浏览器 Token 的权益，重启不丢）
# ============================================================
ALIPAY_APP_ID = cfg('ALIPAY_APP_ID')
ALIPAY_PRIVATE_KEY = cfg('ALIPAY_PRIVATE_KEY')
ALIPAY_PUBLIC_KEY = cfg('ALIPAY_PUBLIC_KEY')
ALIPAY_GATEWAY = 'https://openapi.alipay.com/gateway.do'

DATA_DIR = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'data')
PAY_FILE = os.path.join(DATA_DIR, 'payments.json')
_db_lock = threading.Lock()

PRICES = {
    'monthly': {'amount': '18.00', 'days': 31, 'subject': 'WebCut 专业版月度会员'},
    'yearly': {'amount': '128.00', 'days': 366, 'subject': 'WebCut 专业版年度会员'},
}
ORDER_TTL = 15 * 60  # 订单 15 分钟有效
LIMITS_FREE = {'maxTimelines': 1, 'exportResolution': 720, 'watermark': True}
LIMITS_PRO = {'maxTimelines': 50, 'exportResolution': 2160, 'watermark': False}


def alipay_enabled():
    return bool(ALIPAY_APP_ID and ALIPAY_PRIVATE_KEY and _CRYPTO)


def _load_alipay_private_key():
    """兼容 PEM 与支付宝密钥工具的裸 base64（自动尝试 PKCS8 / PKCS1 头）"""
    raw = ALIPAY_PRIVATE_KEY.strip()
    if '-----BEGIN' in raw:
        return _ser.load_pem_private_key(raw.encode(), password=None)
    for head in ('PRIVATE KEY', 'RSA PRIVATE KEY'):
        body = '\n'.join(raw[i:i + 64] for i in range(0, len(raw), 64))
        pem = '-----BEGIN {h}-----\n{b}\n-----END {h}-----'.format(h=head, b=body)
        try:
            return _ser.load_pem_private_key(pem.encode(), password=None)
        except Exception:
            continue
    raise RuntimeError('ALIPAY_PRIVATE_KEY 格式无法识别（支持 PEM 或密钥工具裸 base64）')


def _alipay_sign_content(params):
    """待签名串：除 sign/sign_type 外、值非空的参数按 key 字典序 k=v& 连接（不 urlencode）"""
    items = sorted((k, v) for k, v in params.items()
                   if k not in ('sign', 'sign_type') and v not in ('', None))
    return '&'.join('{}={}'.format(k, v) for k, v in items)


def alipay_call(method, biz_content):
    """调用支付宝开放网关（RSA2 签名），返回 method 对应的 response 节点"""
    if not alipay_enabled():
        raise RuntimeError('支付宝未配置（缺 ALIPAY_APP_ID / ALIPAY_PRIVATE_KEY 或未安装 cryptography）')
    params = {
        'app_id': ALIPAY_APP_ID,
        'method': method,
        'format': 'JSON',
        'charset': 'utf-8',
        'sign_type': 'RSA2',
        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
        'version': '1.0',
        'biz_content': json.dumps(biz_content, ensure_ascii=False, separators=(',', ':')),
    }
    key = _load_alipay_private_key()
    sig = key.sign(_alipay_sign_content(params).encode('utf-8'),
                   _padding.PKCS1v15(), _hashes.SHA256())
    params['sign'] = base64.b64encode(sig).decode()
    data = urllib.parse.urlencode(params).encode('utf-8')
    code, raw = fetch_with_retry(ALIPAY_GATEWAY, data=data,
                                 headers={'Content-Type': 'application/x-www-form-urlencoded;charset=utf-8'})
    j = read_json(raw)
    resp = j.get(method.replace('.', '_') + '_response') or {}
    if resp.get('code') != '10000':
        raise RuntimeError('支付宝 {} 失败：{} {}'.format(
            method, resp.get('sub_code') or resp.get('code'),
            str(resp.get('sub_msg') or resp.get('msg'))[:160]))
    return resp


def db_load():
    try:
        with open(PAY_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {'orders': {}, 'entitlements': {}}


def db_save(db):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = PAY_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(db, f, ensure_ascii=False)
    os.replace(tmp, PAY_FILE)


def mark_paid(o, db):
    """订单置为已支付，并给下单浏览器 Token 发放对应时长的专业版权益（续费叠加）"""
    if o.get('status') == 'paid':
        return
    o['status'] = 'paid'
    o['paidAt'] = time.time()
    token = o.get('token') or ''
    if token:
        days = PRICES[o['cycle']]['days']
        ent = db['entitlements'].get(token) or {}
        base = max(time.time(), ent.get('expiresAt', 0))
        db['entitlements'][token] = {'plan': 'pro', 'expiresAt': base + days * 86400,
                                     'orderId': o['id'], 'cycle': o['cycle']}


def create_order(plan, cycle, token, host):
    if plan != 'pro' or cycle not in PRICES:
        return None, '参数错误：plan 须为 pro，cycle 须为 monthly / yearly'
    order_id = 'wc' + uuid.uuid4().hex[:20]
    o = {'id': order_id, 'plan': 'pro', 'cycle': cycle, 'amount': PRICES[cycle]['amount'],
         'status': 'pending', 'provider': 'alipay' if alipay_enabled() else 'demo',
         'token': token or '', 'createdAt': time.time(),
         'expireAt': time.time() + ORDER_TTL, 'payUrl': None, '_lastQuery': 0}
    if o['provider'] == 'alipay':
        resp = alipay_call('alipay.trade.precreate', {
            'out_trade_no': order_id, 'total_amount': o['amount'],
            'subject': PRICES[cycle]['subject'], 'timeout_express': '15m'})
        o['payUrl'] = resp.get('qr_code')
        if not o['payUrl']:
            return None, '支付宝未返回二维码'
    else:
        # 演示通道：二维码指向本机演示支付页（订单/权益流程与真实通道完全一致）
        o['payUrl'] = 'http://{}/pay/{}'.format(host or ('127.0.0.1:' + str(PORT)), order_id)
    return o, None


def order_status(order_id):
    """查询订单；真实通道向支付宝轮询 trade.query（≥3s 节流），演示通道本地判定"""
    with _db_lock:
        db = db_load()
    o = db['orders'].get(order_id)
    if not o:
        return None
    now = time.time()
    changed = False
    if o['status'] == 'pending':
        if now > o['expireAt']:
            o['status'] = 'expired'
            changed = True
        elif o['provider'] == 'alipay' and now - o.get('_lastQuery', 0) >= 3:
            o['_lastQuery'] = now
            changed = True
            try:
                resp = alipay_call('alipay.trade.query', {'out_trade_no': order_id})
                ts = resp.get('trade_status', '')
                if ts in ('TRADE_SUCCESS', 'TRADE_FINISHED'):
                    mark_paid(o, db)
                elif ts == 'TRADE_CLOSED':
                    o['status'] = 'expired'
            except Exception as e:
                print('[pay] alipay query error:', e)
    if changed:
        with _db_lock:
            db_save(db)
    return o


def mock_pay(order_id):
    """演示通道的付款动作：仅 demo 订单可用，真实支付宝订单一律拒绝"""
    with _db_lock:
        db = db_load()
        o = db['orders'].get(order_id)
        if not o:
            return None, '订单不存在'
        if o['provider'] != 'demo':
            return None, '真实支付订单不可模拟'
        if o['status'] == 'paid':
            db_save(db)
            return o, None
        if time.time() > o['expireAt']:
            o['status'] = 'expired'
            db_save(db)
            return None, '订单已过期，请重新下单'
        mark_paid(o, db)
        db_save(db)
        return o, None


def db_entitlements(token):
    with _db_lock:
        db = db_load()
    ent = db['entitlements'].get(token) if token else None
    if ent and ent.get('expiresAt', 0) > time.time():
        return {'plan': 'pro', 'expiresAt': ent['expiresAt'], 'limits': LIMITS_PRO}
    return {'plan': 'free', 'limits': LIMITS_FREE}


def order_qr_png(pay_url):
    if not _qrcode:
        return None
    img = _qrcode.make(pay_url, image_factory=_qr_pure.PyPNGImage)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue()


def demo_pay_page(order_id):
    """演示通道支付页（二维码落地页）：手机扫码也会到达这里，点击即完成演示支付"""
    return ('<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" '
            'content="width=device-width,initial-scale=1"><title>WebCut 演示支付</title>'
            '<style>body{font-family:system-ui;background:#121a33;color:#fff;display:flex;'
            'align-items:center;justify-content:center;height:100vh;margin:0}'
            'div{text-align:center;background:#16213e;padding:36px 34px;border-radius:12px;'
            'max-width:86vw}p{color:#8f9ab8;font-size:14px;line-height:1.8}'
            'button{margin-top:14px;padding:12px 30px;font-size:15px;border:none;border-radius:8px;'
            'background:#e94560;color:#fff;cursor:pointer}</style></head><body><div>'
            '<h2>WebCut 演示支付</h2><p>当前未配置支付宝商户凭证，<br>收款通道运行在演示模式。</p>'
            '<button onclick="fetch(\'/api/orders/' + order_id + '/mock-pay\',{method:\'POST\'})'
            '.then(function(){document.body.innerHTML=\'<div style="color:#7fd0bd;font-size:22px">'
            '支付成功 ✓<br><span style="font-size:13px;color:#8f9ab8">请回到编辑器页面</span></div>\'})">'
            '模拟支付成功</button></div></body></html>').encode('utf-8')


# ============================================================
# 云同步（工程云端备份/恢复）
#   存储：ai-backend/cloud/{sha256(token)[:32]}/project.json + media/{sid}.bin(+.audio.wav)
#   身份沿用支付的浏览器 Token（X-User-Token）；换设备=换浏览器 Token，MVP 无账号体系。
#   端点：GET  /api/cloud/status           → {exists, updatedAt, clipCount, name}
#         GET/PUT /api/cloud/project       → 结构 JSON（PUT 全量覆盖）
#         PUT /api/cloud/media/{sid}       → 媒体主体二进制（X-Media-Name/X-Media-Mime 头）
#         GET  /api/cloud/media/{sid}      → 媒体主体字节
#         PUT/GET /api/cloud/media/{sid}/audio → 附加音轨（AI 旁白/配乐 WAV）
# ============================================================
CLOUD_DIR = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'cloud')
CLOUD_MAX_PROJECT = 20 * 1024 * 1024    # 结构 JSON 上限
CLOUD_MAX_MEDIA = 1024 * 1024 * 1024    # 单媒体上限 1GB
CLOUD_QUOTA = 2 * 1024 * 1024 * 1024    # 每用户云端配额 2GB


def cloud_usage(token):
    used = 0
    for dirpath, _dirs, files in os.walk(_cloud_root(token)):
        for fn in files:
            if fn.endswith('.tmp'):
                continue
            try:
                used += os.path.getsize(os.path.join(dirpath, fn))
            except Exception:
                pass
    return {'used': used, 'quota': CLOUD_QUOTA}


def _cloud_root(token):
    h = hashlib.sha256(token.encode('utf-8')).hexdigest()[:32]
    return os.path.join(CLOUD_DIR, h)


def cloud_status(token):
    proot = os.path.join(_cloud_root(token), 'project.json')
    try:
        with open(proot, encoding='utf-8') as f:
            rec = json.load(f)
        return {'exists': True, 'updatedAt': rec.get('updatedAt'),
                'clipCount': len(rec.get('clips', [])), 'name': rec.get('name')}
    except Exception:
        return {'exists': False}


def cloud_put_project(token, raw):
    try:
        json.loads(raw.decode('utf-8'))  # 合法性校验，防垃圾覆盖
    except Exception:
        return False, '工程数据不合法'
    root = _cloud_root(token)
    os.makedirs(root, exist_ok=True)
    proot = os.path.join(root, 'project.json')
    tmp = proot + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(raw)
    os.replace(tmp, proot)
    print('[cloud] project saved:', os.path.basename(root))
    return True, None


def cloud_get_project(token):
    proot = os.path.join(_cloud_root(token), 'project.json')
    try:
        with open(proot, 'rb') as f:
            return f.read()
    except Exception:
        return None


def cloud_media_path(token, sid, audio):
    if not sid or '/' in sid or '\\' in sid or sid.startswith('.'):
        return None
    mroot = os.path.join(_cloud_root(token), 'media')
    return os.path.join(mroot, sid + ('.audio.wav' if audio else '.bin'))


def cloud_put_media(token, sid, audio, raw, name, mime):
    fname = cloud_media_path(token, sid, audio)
    if not fname:
        return False, '非法媒体 ID'
    os.makedirs(os.path.dirname(fname), exist_ok=True)
    tmp = fname + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(raw)
    os.replace(tmp, fname)
    with open(fname + '.json', 'w', encoding='utf-8') as f:  # 侧车元数据
        json.dump({'name': name or sid, 'mime': mime or 'application/octet-stream'}, f, ensure_ascii=False)
    print('[cloud] media saved:', sid, '(audio)' if audio else '', len(raw), 'bytes')
    return True, None


def cloud_get_media(token, sid, audio):
    fname = cloud_media_path(token, sid, audio)
    if not fname:
        return None
    try:
        with open(fname, 'rb') as f:
            return f.read()
    except Exception:
        return None


class Handler(BaseHTTPRequestHandler):
    # CORS：允许 Netlify 前端跨域访问（同源请求不受影响）
    ALLOWED_ORIGINS = [
        'https://webcut-ai-2026.netlify.app',
        'http://webcut-ai-2026.netlify.app',
        'http://localhost:3000',   # 本地开发服务器
        'http://localhost:8899',   # 本地静态服务
    ]

    def _cors_headers(self):
        """为跨域请求添加 CORS 头"""
        origin = self.headers.get('Origin', '')
        if origin and origin in self.ALLOWED_ORIGINS:
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
            self.send_header('Access-Control-Allow-Headers',
                             'Content-Type, X-User-Token, X-Media-Name, X-Media-Mime')
            self.send_header('Access-Control-Max-Age', '86400')

    def do_OPTIONS(self):
        """CORS 预检请求"""
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self._cors_headers()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, code, data, ctype):
        self.send_response(code)
        self._cors_headers()
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        return read_json(self.rfile.read(n)) if n else {}

    def _raw(self, limit):
        """读取原始请求体（云同步媒体上传用），超限返回 None"""
        n = int(self.headers.get('Content-Length') or 0)
        if n <= 0 or n > limit:
            return None
        return self.rfile.read(n)

    def do_GET(self):
        self.route('GET')

    def do_POST(self):
        self.route('POST')

    def do_PUT(self):
        self.route('PUT')

    def route(self, method):
        from urllib.parse import urlparse
        parts = [p for p in urlparse(self.path).path.split('/') if p]  # ['api','ai',...]
        try:
            if parts[:1] == ['api'] and parts[1] in ('ai', 'orders', 'me', 'cloud'):
                return self.api(method, parts)
            # 演示通道支付页（二维码落地页）
            if parts[:1] == ['pay'] and len(parts) == 2 and method == 'GET':
                with _db_lock:
                    db = db_load()
                if parts[1] not in db['orders']:
                    return self._json(404, {'error': '订单不存在'})
                return self._bytes(200, demo_pay_page(parts[1]), 'text/html; charset=utf-8')
            if parts[:1] == ['ai-media']:
                name = os.path.basename(parts[1] if len(parts) > 1 else '')
                file = os.path.join(TMP, name)
                if not os.path.exists(file):
                    return self._json(404, {'error': 'not found'})
                ext = os.path.splitext(name)[1]
                self.send_response(200)
                self._cors_headers()
                self.send_header('Content-Type', MIME.get(ext, 'application/octet-stream'))
                self.end_headers()
                with open(file, 'rb') as f:
                    self.wfile.write(f.read())
                return
            # 静态托管编辑器（/ → index.html）
            rel = urlparse(self.path).path
            rel = 'index.html' if rel == '/' else rel.lstrip('/')
            file = os.path.abspath(os.path.join(ROOT, rel))
            if not file.startswith(os.path.abspath(ROOT)) or not os.path.isfile(file):
                return self._json(404, {'error': 'not found'})
            self.send_response(200)
            self._cors_headers()
            self.send_header('Content-Type', (MIME.get(os.path.splitext(file)[1], 'application/octet-stream')) + '; charset=utf-8')
            self.end_headers()
            with open(file, 'rb') as f:
                self.wfile.write(f.read())
        except Exception as e:
            self._json(500, {'error': str(e)})

    def api(self, method, parts):
        print('[api] %s /%s' % (method, '/'.join(parts)))
        try:
            return self._api_inner(method, parts)
        except Exception as e:
            import traceback
            print('[api] 崩溃: %s\n%s' % (e, traceback.format_exc()))
            try:
                return self._json(500, {'error': '服务器内部错误: ' + str(e)})
            except Exception:
                pass

    def _api_inner(self, method, parts):
        # ================= 支付与权益 =================
        # POST /api/orders {plan,cycle}（Header X-User-Token：浏览器身份）
        if parts[1] == 'orders' and method == 'POST' and len(parts) == 2:
            body = self._body()
            token = self.headers.get('X-User-Token', '')
            host = self.headers.get('Host', '127.0.0.1:' + str(PORT))
            with _db_lock:
                o, err = create_order((body.get('plan') or '').strip(),
                                      (body.get('cycle') or '').strip(), token, host)
                if err:
                    return self._json(400, {'error': err})
                db = db_load()
                db['orders'][o['id']] = o
                db_save(db)
            print('[pay] order created:', o['id'], o['cycle'], o['provider'])
            return self._json(200, {'orderId': o['id'], 'payUrl': o['payUrl'], 'amount': o['amount'],
                                    'cycle': o['cycle'], 'provider': o['provider'],
                                    'demo': o['provider'] == 'demo', 'expireAt': o['expireAt']})

        # GET /api/orders/{id}/status
        if parts[1] == 'orders' and method == 'GET' and len(parts) == 4 and parts[3] == 'status':
            o = order_status(parts[2])
            if not o:
                return self._json(404, {'error': '订单不存在'})
            return self._json(200, {'orderId': o['id'], 'status': o['status'], 'provider': o['provider'],
                                    'amount': o['amount'], 'cycle': o['cycle'],
                                    'expireAt': o['expireAt'], 'paidAt': o.get('paidAt')})

        # GET /api/orders/{id}/qr.png（真二维码，渲染 payUrl）
        if parts[1] == 'orders' and method == 'GET' and len(parts) == 4 and parts[3] == 'qr.png':
            with _db_lock:
                db = db_load()
            o = db['orders'].get(parts[2])
            if not o or not o.get('payUrl'):
                return self._json(404, {'error': '订单不存在'})
            png = order_qr_png(o['payUrl'])
            if not png:
                return self._json(503, {'error': '二维码服务不可用（pip install qrcode）'})
            return self._bytes(200, png, 'image/png')

        # POST /api/orders/{id}/mock-pay（演示通道专用）
        if parts[1] == 'orders' and method == 'POST' and len(parts) == 4 and parts[3] == 'mock-pay':
            o, err = mock_pay(parts[2])
            if err:
                print('[pay] mock-pay rejected:', parts[2], err)
                return self._json(410 if '过期' in err else 400, {'error': err})
            print('[pay] mock-pay ok:', o['id'])
            return self._json(200, {'orderId': o['id'], 'status': o['status']})

        # GET /api/me/entitlements（X-User-Token）—— 档位与限制的唯一事实源
        if parts[1] == 'me' and method == 'GET' and len(parts) == 3 and parts[2] == 'entitlements':
            return self._json(200, db_entitlements(self.headers.get('X-User-Token', '')))

        # ================= 云同步（工程云端备份/恢复） =================
        if parts[1] == 'cloud' and len(parts) >= 3:
            token = self.headers.get('X-User-Token', '')
            if not token:
                return self._json(401, {'error': '缺少 X-User-Token'})
            if parts[2] == 'status' and method == 'GET':
                return self._json(200, cloud_status(token))
            if parts[2] == 'usage' and method == 'GET':
                return self._json(200, cloud_usage(token))
            if parts[2] == 'project' and len(parts) == 3:
                if method == 'GET':
                    data = cloud_get_project(token)
                    if not data:
                        return self._json(404, {'error': '云端暂无备份'})
                    return self._bytes(200, data, 'application/json; charset=utf-8')
                if method in ('PUT', 'POST'):
                    raw = self._raw(CLOUD_MAX_PROJECT)
                    if not raw:
                        return self._json(400, {'error': '工程数据为空或超限'})
                    ok, err = cloud_put_project(token, raw)
                    return self._json(200 if ok else 400, {'ok': ok, 'error': err})
            if parts[2] == 'media' and len(parts) in (4, 5):
                audio = len(parts) == 5 and parts[4] == 'audio'
                sid = parts[3]
                if method in ('PUT', 'POST'):
                    raw = self._raw(CLOUD_MAX_MEDIA)
                    if not raw:
                        return self._json(400, {'error': '媒体为空或超限（单文件上限 1GB）'})
                    ok, err = cloud_put_media(token, sid, audio, raw,
                                              urllib.parse.unquote(self.headers.get('X-Media-Name', '') or ''),
                                              self.headers.get('X-Media-Mime', '') or '')
                    return self._json(200 if ok else 400, {'ok': ok, 'error': err})
                if method == 'GET':
                    data = cloud_get_media(token, sid, audio)
                    if not data:
                        return self._json(404, {'error': '媒体不存在'})
                    return self._bytes(200, data, 'application/octet-stream')

        # ================= AI 能力 =================
        # GET /api/ai/health —— 能力位：video 需 Key；tts 恒可用（有 Key 走 CosyVoice，
        # 无 Key 用本地合成演示音，链路始终可测）
        if method == 'GET' and parts[2] == 'health' and len(parts) == 3:
            return self._json(200, {'available': True, 'video': bool(KEY), 'tts': True,
                                    'provider': 'dashscope-wanx+cosyvoice' if KEY else 'local-demo-tts',
                                    'payments': {'alipay': alipay_enabled(), 'demo': True}})

        # POST /api/ai/video {prompt, duration}
        if method == 'POST' and parts[2] == 'video' and len(parts) == 3:
            print('[ai/video] 收到创建任务请求')
            try:
                body = self._body()
                prompt = (body.get('prompt') or '').strip()
                if not prompt:
                    return self._json(400, {'error': 'prompt 不能为空'})
                if not KEY:
                    return self._json(501, {'error': '未配置 DASHSCOPE_API_KEY'})
                print('[ai/video] prompt=%s, duration=%s' % (prompt[:50], body.get('duration')))
                task_id = str(uuid.uuid4())
                TASKS[task_id] = {'id': task_id, 'status': 'running', 'progress': 0.1,
                                  'stage': '排队中', 'prompt': prompt, 'error': None, 'video_file': None}

                def start():
                    try:
                        print('[ai/video] 开始调用万相 API...')
                        tid = create_wanx_task(prompt, body.get('duration'))
                        print('[ai/video] 万相任务已创建: %s' % tid)
                        poll_loop(task_id, tid)
                    except Exception as e:
                        import traceback
                        print('[ai/video] 生成失败: %s\n%s' % (e, traceback.format_exc()))
                        TASKS[task_id]['status'] = 'failed'
                        TASKS[task_id]['error'] = str(e)
                threading.Thread(target=start, daemon=True).start()
                print('[ai/video] 任务已分配: %s' % task_id)
                return self._json(200, {'taskId': task_id})
            except Exception as e:
                import traceback
                print('[ai/video] 路由崩溃: %s\n%s' % (e, traceback.format_exc()))
                return self._json(500, {'error': '服务器内部错误: ' + str(e)})

        # GET /api/ai/video/:id
        if method == 'GET' and parts[2] == 'video' and len(parts) == 4:
            t = TASKS.get(parts[3])
            if not t:
                return self._json(404, {'error': '任务不存在'})
            return self._json(200, {'status': t['status'], 'progress': t['progress'],
                                    'stage': t['stage'], 'error': t['error'],
                                    'url': ('/ai-media/' + t['video_file']) if t['video_file'] else None})

        # POST /api/ai/tts {text, voice?} —— CosyVoice，无 Key 用本地合成演示音
        if method == 'POST' and parts[2] == 'tts' and len(parts) == 3:
            body = self._body()
            text = (body.get('text') or '').strip()
            if not text:
                return self._json(400, {'error': 'text 不能为空'})
            try:
                if KEY:
                    audio_url = create_cosyvoice_speech(text, body.get('voice'))
                    ext = 'wav' if audio_url.split('?')[0].endswith('.wav') else 'mp3'
                    name = download_to_tmp(audio_url, ext)
                    demo = False
                else:
                    name = save_tmp(synth_wav(text), 'wav')
                    demo = True
                return self._json(200, {'url': '/ai-media/' + name, 'demo': demo})
            except Exception as e:
                # 超时 / 鉴权失败 / 供应商错误原样暴露，前端负责 UI 提示与配乐兜底
                return self._json(502, {'error': 'TTS 失败：' + str(e)})

        self._json(404, {'error': 'not found'})

    def log_message(self, fmt, *args):
        """访问日志：打印每个请求（之前被禁用导致看不到请求到达）"""
        print('[http] %s %s' % (self.command, self.path))


if __name__ == '__main__':
    print('[WebCut AI] http://127.0.0.1:%d  provider=%s' %
          (PORT, 'dashscope-wanx+cosyvoice' if KEY else 'local-demo-tts（未配置 Key，TTS 为演示音）'))
    print('[WebCut AI] 支付通道: %s' %
          ('alipay 当面付（真实收款）' if alipay_enabled() else 'demo（未配置商户凭证，模拟支付）'))
    print('[WebCut AI] 临时目录:', TMP)
    ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
