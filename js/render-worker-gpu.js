/* ============================================================
   WebCut GPU Render Worker (js/render-worker-gpu.js)
   —— 第三步架构升级：WebGL2/WebGPU GPU 加速渲染

   渲染后端优先级：WebGPU → WebGL2 → Canvas2D（自动检测降级）

   核心设计：
   · 源画面上传为 GPU 纹理（一次上传，多效果零开销叠加）
   · 变换（位置/缩放/旋转）在顶点着色器完成
   · 调色/模糊/色度键/锐化在片段着色器完成
   · 关键帧插值仍在 Worker CPU 侧（结果作为 uniform 传入）
   · 最终帧 transferToImageBitmap 零拷贝回主线程
   ============================================================ */

// ============================================================
// DAG 求值器（与 dag-engine.js 一致）
// ============================================================
const Easings = {
  linear: t => t, easeIn: t => t * t, easeOut: t => t * (2 - t),
  easeInOut: t => t < .5 ? 2 * t * t : -1 + (4 - 2 * t) * t, step: t => t < 1 ? 0 : 1,
  easeInCubic: t => t ** 3, easeOutCubic: t => 1 - (1 - t) ** 3,
  easeOutBounce: t => { const n = 7.5625, d = 2.75;
    if (t < 1 / d) return n * t * t;
    if (t < 2 / d) return n * (t -= 1.5 / d) * t + .75;
    if (t < 2.5 / d) return n * (t -= 2.25 / d) * t + .9375;
    return n * (t -= 2.625 / d) * t + .984375; }
};

function sampleParam(value, timeMs) {
  if (!Array.isArray(value) || !value.length || typeof value[0] !== 'object' || !('time' in value[0])) return value;
  const kfs = value;
  if (timeMs <= kfs[0].time) return kfs[0].value;
  if (timeMs >= kfs[kfs.length - 1].time) return kfs[kfs.length - 1].value;
  for (let i = 0; i < kfs.length - 1; i++) {
    const p = kfs[i], n = kfs[i + 1];
    if (timeMs >= p.time && timeMs < n.time) {
      const dur = n.time - p.time; if (!dur) return p.value;
      const t = (timeMs - p.time) / dur;
      const e = (Easings[p.easing] || Easings.linear)(t);
      if (typeof p.value === 'number' && typeof n.value === 'number')
        return p.value + (n.value - p.value) * e;
      if (Array.isArray(p.value))
        return p.value.map((v, j) => v + (n.value[j] - v) * e);
      return e < .5 ? p.value : n.value;
    }
  }
  return kfs[kfs.length - 1].value;
}

function sampleNodeParams(node, timeMs) {
  const out = {};
  for (const [k, v] of Object.entries(node.params || {})) out[k] = sampleParam(v, timeMs);
  return out;
}

function topoSort(nodes) {
  const inDeg = {}, adj = {}, map = {};
  for (const n of nodes) { map[n.id] = n; inDeg[n.id] = 0; adj[n.id] = []; }
  for (const n of nodes) for (const f of (n.inputs || [])) if (map[f]) { adj[f].push(n.id); inDeg[n.id]++; }
  const q = Object.keys(inDeg).filter(id => !inDeg[id]), sorted = [];
  while (q.length) { const id = q.shift(); sorted.push(map[id]);
    for (const nx of adj[id]) if (!--inDeg[nx]) q.push(nx); }
  return sorted.length === nodes.length ? sorted : null;
}

// ============================================================
// WebGL2 渲染器
// ============================================================
const VERT_SHADER = `#version 300 es
in vec2 aPos;
in vec2 aUV;
uniform mat4 uTransform;
out vec2 vUV;
void main() {
  vUV = aUV;
  gl_Position = uTransform * vec4(aPos, 0.0, 1.0);
}`;

const FRAG_SHADER = `#version 300 es
precision highp float;
in vec2 vUV;
uniform sampler2D uSource;
uniform vec4 uColor;         // brightness, contrast, saturation, hueRotate (度)
uniform vec2 uBlur;          // blur radius (px), 0
uniform vec3 uChromaKey;     // keyR, keyG, keyB (0=off)
uniform float uChromaTol;    // tolerance
uniform float uSharpen;      // sharpen amount
uniform float uOpacity;
uniform vec2 uTexelSize;     // 1/width, 1/height
out vec4 fragColor;

vec3 rgb2hsv(vec3 c) {
  vec4 K = vec4(0.0, -1.0/3.0, 2.0/3.0, -1.0);
  vec4 p = mix(vec4(c.bg, K.wz), vec4(c.gb, K.xy), step(c.b, c.g));
  vec4 q = mix(vec4(p.xyw, c.r), vec4(c.r, p.yzx), step(p.x, c.r));
  float d = q.x - min(q.w, q.y);
  float e = 1.0e-10;
  return vec3(abs(q.z + (q.w - q.y) / (6.0 * d + e)), d / (q.x + e), q.x);
}

vec3 hsv2rgb(vec3 c) {
  vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);
  vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
  return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}

void main() {
  vec2 uv = vUV;
  vec4 color = texture(uSource, uv);

  // === 色度键（绿幕） ===
  if (uChromaKey.r > 0.0 || uChromaKey.g > 0.0 || uChromaKey.b > 0.0) {
    float dist = distance(color.rgb, uChromaKey);
    if (dist < uChromaTol) {
      fragColor = vec4(0.0);
      return;
    }
  }

  // === 模糊（简易高斯：9 次采样） ===
  if (uBlur.x > 0.0) {
    vec3 blur = vec3(0.0);
    float total = 0.0;
    for (int x = -2; x <= 2; x++) {
      for (int y = -2; y <= 2; y++) {
        vec2 off = vec2(float(x), float(y)) * uBlur.x * uTexelSize;
        float w = exp(-float(x*x + y*y) / 8.0);
        blur += texture(uSource, uv + off).rgb * w;
        total += w;
      }
    }
    color.rgb = blur / total;
  }

  // === 锐化（Unsharp Mask） ===
  if (uSharpen > 0.0) {
    vec3 neighbor = (
      texture(uSource, uv + vec2(uTexelSize.x, 0.0)).rgb +
      texture(uSource, uv - vec2(uTexelSize.x, 0.0)).rgb +
      texture(uSource, uv + vec2(0.0, uTexelSize.y)).rgb +
      texture(uSource, uv - vec2(0.0, uTexelSize.y)).rgb
    ) * 0.25;
    color.rgb += (color.rgb - neighbor) * uSharpen;
  }

  // === 调色 ===
  float brightness = uColor.x;
  float contrast = uColor.y;
  float saturation = uColor.z;
  float hueRotate = uColor.w;

  // 亮度
  color.rgb *= (1.0 + brightness / 100.0);
  // 对比度
  color.rgb = (color.rgb - 0.5) * (1.0 + contrast / 100.0) + 0.5;
  // 饱和度
  float lum = dot(color.rgb, vec3(0.299, 0.587, 0.114));
  color.rgb = mix(vec3(lum), color.rgb, 1.0 + saturation / 100.0);
  // 色相旋转
  if (abs(hueRotate) > 0.5) {
    vec3 hsv = rgb2hsv(clamp(color.rgb, 0.0, 1.0));
    hsv.x = fract(hsv.x + hueRotate / 360.0);
    color.rgb = hsv2rgb(hsv);
  }

  // === 不透明度 ===
  color.a *= uOpacity;

  fragColor = clamp(color, 0.0, 1.0);
}`;

class GLRenderer {
  constructor(canvas) {
    this.canvas = canvas;
    this.gl = canvas.getContext('webgl2', { premultipliedAlpha: false, antialias: false });
    if (!this.gl) throw new Error('WebGL2 not available');
    this.prog = this._createProgram(VERT_SHADER, FRAG_SHADER);
    this._initBuffers();
    this._initUniforms();
    this.texture = null;
  }

  _compile(type, src) {
    const gl = this.gl;
    const sh = gl.createShader(type);
    gl.shaderSource(sh, src);
    gl.compileShader(sh);
    if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS))
      throw new Error('Shader: ' + gl.getShaderInfoLog(sh));
    return sh;
  }

  _createProgram(vsSrc, fsSrc) {
    const gl = this.gl;
    const prog = gl.createProgram();
    gl.attachShader(prog, this._compile(gl.VERTEX_SHADER, vsSrc));
    gl.attachShader(prog, this._compile(gl.FRAGMENT_SHADER, fsSrc));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS))
      throw new Error('Link: ' + gl.getProgramInfoLog(prog));
    return prog;
  }

  _initBuffers() {
    const gl = this.gl;
    // 全屏四边形 [x, y, u, v]
    const verts = new Float32Array([
      -1, -1, 0, 1,   1, -1, 1, 1,   1, 1, 1, 0,
      -1, -1, 0, 1,   1, 1, 1, 0,   -1, 1, 0, 0
    ]);
    this.vao = gl.createVertexArray();
    gl.bindVertexArray(this.vao);
    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, verts, gl.STATIC_DRAW);

    const stride = 4 * 4;
    const aPos = gl.getAttribLocation(this.prog, 'aPos');
    gl.enableVertexAttribArray(aPos);
    gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, stride, 0);
    const aUV = gl.getAttribLocation(this.prog, 'aUV');
    gl.enableVertexAttribArray(aUV);
    gl.vertexAttribPointer(aUV, 2, gl.FLOAT, false, stride, 2 * 4);
    gl.bindVertexArray(null);
  }

  _initUniforms() {
    const gl = this.gl;
    this.u = {
      transform: gl.getUniformLocation(this.prog, 'uTransform'),
      source: gl.getUniformLocation(this.prog, 'uSource'),
      color: gl.getUniformLocation(this.prog, 'uColor'),
      blur: gl.getUniformLocation(this.prog, 'uBlur'),
      chromaKey: gl.getUniformLocation(this.prog, 'uChromaKey'),
      chromaTol: gl.getUniformLocation(this.prog, 'uChromaTol'),
      sharpen: gl.getUniformLocation(this.prog, 'uSharpen'),
      opacity: gl.getUniformLocation(this.prog, 'uOpacity'),
      texelSize: gl.getUniformLocation(this.prog, 'uTexelSize')
    };
  }

  uploadSource(bitmap) {
    const gl = this.gl;
    if (this.texture) gl.deleteTexture(this.texture);
    this.texture = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, this.texture);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, bitmap);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    this.texW = bitmap.width;
    this.texH = bitmap.height;
  }

  // 构建变换矩阵 (translate * rotate * scale)
  _mat4(params, width, height) {
    const scale = (params.scale || 100) / 100;
    const rot = (params.rotation || 0) * Math.PI / 180;
    const tx = (params.x || 0) / 100;
    const ty = -(params.y || 0) / 100;
    const cos = Math.cos(rot) * scale, sin = Math.sin(rot) * scale;
    // 列主序 mat4
    return new Float32Array([
      cos, -sin, 0, 0,
      sin,  cos, 0, 0,
      0, 0, 1, 0,
      tx, ty, 0, 1
    ]);
  }

  render(graph, timeMs) {
    const gl = this.gl;
    const w = this.canvas.width, h = this.canvas.height;
    gl.viewport(0, 0, w, h);
    gl.clearColor(0, 0, 0, 1);
    gl.clear(gl.COLOR_BUFFER_BIT);

    if (!this.texture) return;
    gl.useProgram(this.prog);
    gl.bindVertexArray(this.vao);

    // 采样所有节点参数
    const sorted = topoSort(graph.nodes);
    if (!sorted) throw new Error('DAG has cycles');

    let transformParams = {}, colorParams = {}, blurParams = {}, chromaParams = {}, sharpenParams = 0, opacity = 1;
    for (const node of sorted) {
      const p = sampleNodeParams(node, timeMs);
      switch (node.type) {
        case 'transform':
          transformParams = p;
          opacity = (p.opacity != null ? p.opacity : 100) / 100;
          break;
        case 'color':
          colorParams = p;
          break;
        case 'blur':
          blurParams = p;
          break;
        case 'chromaKey':
          chromaParams = p;
          break;
        case 'sharpen':
          sharpenParams = (p.amount || 0) / 100;
          break;
      }
    }

    // 绑定纹理
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.texture);
    gl.uniform1i(this.u.source, 0);

    // 变换矩阵
    gl.uniformMatrix4fv(this.u.transform, false, this._mat4(transformParams, w, h));

    // 调色 uniform
    gl.uniform4f(this.u.color,
      colorParams.brightness || 0,
      colorParams.contrast || 0,
      colorParams.saturation || 0,
      colorParams.hueRotate || colorParams.tint || 0
    );

    // 模糊
    gl.uniform2f(this.u.blur, (blurParams.radius || 0) / 10, 0);

    // 色度键
    if (chromaParams.enabled) {
      const keyColor = chromaParams.keyColor || '#00ff00';
      const r = parseInt(keyColor.slice(1, 3), 16) / 255;
      const g = parseInt(keyColor.slice(3, 5), 16) / 255;
      const b = parseInt(keyColor.slice(5, 7), 16) / 255;
      gl.uniform3f(this.u.chromaKey, r, g, b);
      gl.uniform1f(this.u.chromaTol, (chromaParams.tolerance || 30) / 100);
    } else {
      gl.uniform3f(this.u.chromaKey, 0, 0, 0);
      gl.uniform1f(this.u.chromaTol, 0);
    }

    // 锐化
    gl.uniform1f(this.u.sharpen, sharpenParams);

    // 不透明度
    gl.uniform1f(this.u.opacity, opacity);

    // 纹素大小
    gl.uniform2f(this.u.texelSize, 1 / this.texW, 1 / this.texH);

    // 绘制
    gl.drawArrays(gl.TRIANGLES, 0, 6);
    gl.bindVertexArray(null);
  }

  toBitmap() {
    return this.canvas.transferToImageBitmap();
  }

  destroy() {
    if (this.texture) this.gl.deleteTexture(this.texture);
    this.gl.getExtension('WEBGL_lose_context')?.loseContext();
  }
}

// ============================================================
// Worker 消息处理
// ============================================================
let renderer = null;
let backend = 'none';

self.onmessage = function(e) {
  const { type, payload } = e.data;

  if (type === 'RENDER') {
    const { graph, timeMs, width, height, sourceBitmap } = payload;

    try {
      if (!renderer) {
        const offscreen = new OffscreenCanvas(width, height);
        // 尝试 WebGPU → WebGL2 → Canvas2D
        // 当前实现：WebGL2（WebGPU 待浏览器普及后切换）
        renderer = new GLRenderer(offscreen);
        backend = 'webgl2';
      }

      // 更新画布尺寸
      if (renderer.canvas.width !== width) renderer.canvas.width = width;
      if (renderer.canvas.height !== height) renderer.canvas.height = height;

      // 上传源画面为 GPU 纹理
      if (sourceBitmap) {
        renderer.uploadSource(sourceBitmap);
        sourceBitmap.close(); // 释放 CPU 侧位图
      }

      // GPU 渲染
      renderer.render(graph, timeMs);

      // 零拷贝回传
      const bitmap = renderer.toBitmap();
      self.postMessage({ type: 'FRAME', bitmap, requestId: payload.requestId, backend }, [bitmap]);

    } catch (error) {
      // WebGL2 失败 → 通知主线程降级
      self.postMessage({ type: 'ERROR', error: String(error), requestId: payload.requestId, backend });
    }
  }

  if (type === 'PING') {
    self.postMessage({ type: 'PONG', backend });
  }

  if (type === 'DESTROY') {
    if (renderer) { renderer.destroy(); renderer = null; }
    self.close();
  }
};
