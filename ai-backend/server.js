/* ============================================================
 * WebCut AI 后端骨架（阶段二 · 一句话生成视频）
 * 零依赖：Node 18+（使用内置 http 与全局 fetch）
 *
 * 启动：
 *   DASHSCOPE_API_KEY=sk-xxx node server.js
 *   → http://127.0.0.1:8787  （同时静态托管 ../video-editor，
 *     编辑器页面与本 API 同源，前端无需处理 CORS）
 *
 * 对接的第三方：阿里云百炼 / 通义万相文生视频（wanx2.1-t2v）
 *   文档：https://help.aliyun.com/zh/model-studio/text-to-video-api-reference
 *   即梦 / 可灵同理：都是「创建异步任务 → 轮询 → 拿 URL → 下载」，
 *   只需替换 createTask/pollTask 两个函数。
 *
 * 路由：
 *   GET  /api/ai/health          能力探测（是否配置了 API Key）
 *   POST /api/ai/video           创建文生视频任务  {prompt, duration}
 *   GET  /api/ai/video/:id       轮询任务（内部代为轮询+下载，返回本地 URL）
 *   POST /api/ai/tts             文本转语音        {text, voice?}
 *   GET  /ai-media/:file         提供下载到临时目录的 MP4 / TTS 音频
 *
 * TODO(支付接入): 生成属于付费能力时，创建任务前走 /api/orders 校验权益。
 * ============================================================ */
'use strict';
const http = require('http');
const fs = require('fs');
const path = require('path');
const os = require('os');
const crypto = require('crypto');

const PORT = process.env.PORT || 8787;
/* Key 来源：系统环境变量优先，其次 ai-backend/.env 文件（DASHSCOPE_API_KEY=sk-xxx），
   可绕开 Windows 环境变量继承问题 */
function loadKey() {
  if (process.env.DASHSCOPE_API_KEY) return process.env.DASHSCOPE_API_KEY;
  try {
    const env = fs.readFileSync(path.join(__dirname, '.env'), 'utf-8');
    const m = env.match(/^\s*DASHSCOPE_API_KEY\s*=\s*['"]?([^'"\r\n]+)/m);
    if (m) return m[1].trim();
  } catch (e) { /* 无 .env 文件 */ }
  return '';
}
const KEY = loadKey();
const ROOT = path.join(__dirname, '..');   // ai-backend 的上一级就是 video-editor（静态托管目录）
const TMP = path.join(os.tmpdir(), 'webcut-ai');           // 生成视频的本地临时目录
if (!fs.existsSync(TMP)) fs.mkdirSync(TMP, { recursive: true });

const tasks = {}; // taskId → {id,status,progress,stage,prompt,videoFile,error}

/* ---------- 带重试的 fetch（指数退避，最多 3 次） ---------- */
async function fetchWithRetry(url, opt = {}, retries = 3) {
  let lastErr;
  for (let i = 0; i < retries; i++) {
    try {
      const r = await fetch(url, { ...opt, signal: AbortSignal.timeout(30000) });
      if (r.status >= 500 && i < retries - 1) throw new Error('HTTP ' + r.status);
      return r;
    } catch (e) {
      lastErr = e;
      await new Promise(res => setTimeout(res, 800 * Math.pow(2, i)));
    }
  }
  throw lastErr;
}

/* ---------- 通义万相：创建任务 ---------- */
const WANX_MODEL = process.env.AI_VIDEO_MODEL || 'wanx2.1-t2v-plus'; // 用户已确认该模型有免费额度
async function createWanxTask(prompt, duration) {
  const r = await fetchWithRetry(
    'https://dashscope.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis',
    {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer ' + KEY,
        'Content-Type': 'application/json',
        'X-DashScope-Async': 'enable'
      },
      body: JSON.stringify({
        model: WANX_MODEL,
        input: { prompt },
        parameters: { size: '960*960', duration: duration || 4 }
      })
    });
  const j = await r.json();
  if (!j.output || !j.output.task_id) {
    throw new Error('创建任务失败：' + JSON.stringify(j).slice(0, 200));
  }
  return j.output.task_id;
}

/* ---------- 通义万相：轮询任务状态 ---------- */
async function pollWanxTask(id) {
  const r = await fetchWithRetry(
    'https://dashscope.aliyuncs.com/api/v1/tasks/' + id,
    { headers: { 'Authorization': 'Bearer ' + KEY } });
  const j = await r.json();
  const st = j.output && j.output.task_status;
  return {
    status: st === 'SUCCEEDED' ? 'done' : st === 'FAILED' ? 'failed' : 'running',
    // 任务进度：万相不提供百分比，用阶段近似（排队 0.3 / 生成 0.7）
    progress: st === 'SUCCEEDED' ? 1 : st === 'RUNNING' ? 0.7 : 0.3,
    videoUrl: j.output && j.output.video_url
  };
}

/* ---------- 下载视频到本地临时目录 ---------- */
function downloadToTmp(url, ext) {
  const name = crypto.randomUUID() + '.' + (ext || 'mp4');
  const file = path.join(TMP, name);
  return fetchWithRetry(url).then(r => new Promise((resolve, reject) => {
    const ws = fs.createWriteStream(file);
    r.body.pipe(ws);
    r.body.on('error', reject);
    ws.on('finish', () => resolve(name));
    ws.on('error', reject);
  }));
}

/* ---------- CosyVoice 语音合成（同步 HTTP，返回音频 URL） ----------
 * 文档：https://help.aliyun.com/zh/model-studio/cosyvoice-python-sdk
 * 常用音色：longxiaochun_v2 / longwan_v2 / longcheng_v2 / loongbella_v2 …
 * 鉴权失败（401）、限流等错误原样抛出，由路由包装成 502 交给前端提示。 */
async function createCosyVoiceSpeech(text, voice) {
  const r = await fetchWithRetry(
    'https://dashscope.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer',
    {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + KEY, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: 'cosyvoice-v2',
        input: { text: text.slice(0, 2000), voice: voice || 'longxiaochun_v2', format: 'mp3' }
      })
    });
  const j = await r.json();
  if (j.code) throw new Error(j.code + ' ' + (j.message || '').slice(0, 160)); // 如 InvalidApiKey 鉴权失败
  const url = j.output && j.output.audio && j.output.audio.url;
  if (!url) throw new Error('CosyVoice 未返回音频 URL');
  return url;
}

/* ---------- 本地演示旁白：纯 Node 合成 16-bit WAV（无 Key 时的链路测试音） ----------
 * 按字符生成带包络的音节（基频随字符码变化），听感为机器人式"哔哔"旁白，
 * 时长 ≈ 0.16s/字 + 0.6s 尾静音 —— 足以验证整条 TTS 链路与音画同步逻辑。 */
function synthWav(text) {
  const sr = 22050;
  const chars = (text.replace(/\s+/g, '') || '你好');
  const total = Math.ceil((chars.length * 0.16 + 0.6) * sr);
  const pcm = Buffer.alloc(total * 2);
  for (let i = 0; i < chars.length; i++) {
    const f0 = 140 + (chars.charCodeAt(i) % 120);
    const n = Math.floor(0.16 * sr * 0.8);
    const start = Math.floor(i * 0.16 * sr);
    for (let k = 0; k < n && start + k < total; k++) {
      const t = k / sr;
      const env = Math.sin(Math.PI * k / n); // 音节包络
      const v = (Math.sin(2 * Math.PI * f0 * t) + 0.6 * Math.sin(2 * Math.PI * f0 * 2.02 * t)) * 0.4 * env;
      pcm.writeInt16LE(Math.round(Math.max(-1, Math.min(1, v)) * 32767), (start + k) * 2);
    }
  }
  const header = Buffer.alloc(44);
  header.write('RIFF', 0); header.writeUInt32LE(36 + pcm.length, 4); header.write('WAVE', 8);
  header.write('fmt ', 12); header.writeUInt32LE(16, 16); header.writeUInt16LE(1, 20);
  header.writeUInt16LE(1, 22); header.writeUInt32LE(sr, 24); header.writeUInt32LE(sr * 2, 28);
  header.writeUInt16LE(2, 32); header.writeUInt16LE(16, 34);
  header.write('data', 36); header.writeUInt32LE(pcm.length, 40);
  return Buffer.concat([header, pcm]);
}
function saveTmp(buffer, ext) {
  const name = crypto.randomUUID() + '.' + ext;
  return new Promise((resolve, reject) => {
    fs.writeFile(path.join(TMP, name), buffer, e => e ? reject(e) : resolve(name));
  });
}

/* ---------- 路由处理 ---------- */
async function handleAPI(req, res, url) {
  const parts = url.pathname.split('/').filter(Boolean); // ['api','ai',...]

  // GET /api/ai/health —— 能力位：video=文生视频（需 Key），tts=语音合成（有 Key 走
  // CosyVoice，无 Key 用本地合成演示音，链路始终可测）
  if (req.method === 'GET' && parts[2] === 'health') {
    return json(res, 200, {
      available: true,
      video: !!KEY,
      tts: true,
      provider: KEY ? 'dashscope-wanx+cosyvoice' : 'local-demo-tts'
    });
  }

  // POST /api/ai/video {prompt, duration}
  if (req.method === 'POST' && parts[2] === 'video' && parts.length === 3) {
    const body = await readBody(req);
    const prompt = (body.prompt || '').trim();
    if (!prompt) return json(res, 400, { error: 'prompt 不能为空' });
    if (!KEY) return json(res, 501, { error: '未配置 DASHSCOPE_API_KEY' });
    const id = crypto.randomUUID();
    tasks[id] = { id, status: 'running', progress: 0.1, stage: '排队中', prompt, error: null, videoFile: null };
    // 服务端异步推进：创建任务 → 轮询 → 下载
    createWanxTask(prompt, body.duration)
      .then(tid => pollLoop(id, tid))
      .catch(e => { tasks[id].status = 'failed'; tasks[id].error = e.message; });
    return json(res, 200, { taskId: id });
  }

  // GET /api/ai/video/:id
  if (req.method === 'GET' && parts[2] === 'video' && parts[3]) {
    const t = tasks[parts[3]];
    if (!t) return json(res, 404, { error: '任务不存在' });
    return json(res, 200, {
      status: t.status, progress: t.progress, stage: t.stage,
      error: t.error, url: t.videoFile ? '/ai-media/' + t.videoFile : null
    });
  }

  // POST /api/ai/tts {text, voice?}
  // 有 Key：DashScope CosyVoice 语音合成（同步 HTTP，返回音频 URL 后下载到本地）。
  // 无 Key：本地合成一段演示旁白 WAV，保证 TTS 链路（HTTP→下载→前端解码→挂载）
  // 不办 Key 也能端到端测试。前端在 TTS 失败时回退合成配乐。
  if (req.method === 'POST' && parts[2] === 'tts') {
    const body = await readBody(req);
    const text = (body.text || '').trim();
    if (!text) return json(res, 400, { error: 'text 不能为空' });
    try {
      let file, demo = false;
      if (KEY) {
        const audioUrl = await createCosyVoiceSpeech(text, body.voice);
        file = await downloadToTmp(audioUrl, audioUrl.split('?')[0].endsWith('.wav') ? 'wav' : 'mp3');
      } else {
        file = await saveTmp(synthWav(text), 'wav');
        demo = true;
      }
      return json(res, 200, { url: '/ai-media/' + file, demo });
    } catch (e) {
      // 超时 / 鉴权失败 / 供应商错误都原样暴露，前端负责 UI 提示与配乐兜底
      return json(res, 502, { error: 'TTS 失败：' + e.message });
    }
  }

  json(res, 404, { error: 'not found' });
}

/* 服务端代为轮询：每 3s 查一次万相，成功后下载到本地 */
function pollLoop(taskId, providerTaskId) {
  const t = tasks[taskId];
  const timer = setInterval(async () => {
    try {
      const r = await pollWanxTask(providerTaskId);
      t.progress = r.progress;
      t.stage = r.status === 'done' ? '下载中' : '生成中';
      if (r.status === 'done') {
        clearInterval(timer);
        t.stage = '下载中';
        t.videoFile = await downloadToTmp(r.videoUrl, 'mp4');
        t.progress = 1;
        t.status = 'done';
        console.log('[ai] task done →', t.videoFile);
      } else if (r.status === 'failed') {
        clearInterval(timer);
        t.status = 'failed';
        t.error = '供应商生成失败';
      }
    } catch (e) {
      // 轮询失败不立刻判死，连续失败由前端超时兜底
      console.warn('[ai] poll error:', e.message);
    }
  }, 3000);
}

/* ---------- 工具 ---------- */
function json(res, code, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(body);
}
function readBody(req) {
  return new Promise(resolve => {
    let raw = '';
    req.on('data', c => raw += c);
    req.on('end', () => { try { resolve(JSON.parse(raw || '{}')); } catch (e) { resolve({}); } });
  });
}
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.mp4': 'video/mp4', '.png': 'image/png', '.jpg': 'image/jpeg' };

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://x');
  try {
    if (url.pathname.startsWith('/api/ai/')) return await handleAPI(req, res, url);
    if (url.pathname.startsWith('/ai-media/')) {
      // 仅允许访问 TMP 目录内的文件（防路径穿越）
      const name = path.basename(url.pathname);
      const file = path.join(TMP, name);
      if (!fs.existsSync(file)) return json(res, 404, { error: 'not found' });
      res.writeHead(200, { 'Content-Type': 'video/mp4', 'Access-Control-Allow-Origin': '*' });
      return fs.createReadStream(file).pipe(res);
    }
    // 静态托管编辑器（/ → index.html）
    let p = url.pathname === '/' ? '/index.html' : url.pathname;
    p = path.normalize(p).replace(/^(\.\.[\/\\])+/, '');
    const file = path.join(ROOT, p);
    if (!file.startsWith(ROOT) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
      return json(res, 404, { error: 'not found' });
    }
    res.writeHead(200, { 'Content-Type': (MIME[path.extname(file)] || 'application/octet-stream') + '; charset=utf-8' });
    fs.createReadStream(file).pipe(res);
  } catch (e) {
    json(res, 500, { error: e.message });
  }
});

server.listen(PORT, () => {
  console.log(`[WebCut AI] http://127.0.0.1:${PORT}  provider=${KEY ? 'dashscope-wanx' : '未配置 Key（仅静态托管）'}`);
  console.log(`[WebCut AI] 临时目录: ${TMP}`);
});
