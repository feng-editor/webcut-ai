/* ============================================================
   WebCut Render Worker (js/render-worker.js)
   —— 第二步架构升级：OffscreenCanvas + ImageBitmap 零拷贝

   工作流：
   1. 主线程发送 { type: 'RENDER', graph, timeMs, width, height, sourceBitmap }
   2. Worker 在 OffscreenCanvas 上执行 DAG 求值 + 渲染
   3. Worker 通过 postMessage({ type: 'FRAME', bitmap }, [bitmap]) 零拷贝回传
   4. 主线程 drawImage(bitmap) 上屏后 bitmap.close()

   DAG 求值在 Worker 内完成（复用 dag-engine.js 的采样逻辑），
   主线程不参与任何计算——这就是"主线程只发快照、只管上屏"。
   ============================================================ */

// ============================================================
// Worker 内置的 DAG 求值器（与 dag-engine.js 逻辑一致，独立副本避免 import）
// ============================================================
const Easings = {
  linear: t => t,
  easeIn: t => t * t,
  easeOut: t => t * (2 - t),
  easeInOut: t => t < 0.5 ? 2 * t * t : -1 + (4 - 2 * t) * t,
  step: t => t < 1 ? 0 : 1,
  easeInCubic: t => t * t * t,
  easeOutCubic: t => 1 - Math.pow(1 - t, 3),
  easeOutBounce: t => {
    const n1 = 7.5625, d1 = 2.75;
    if (t < 1 / d1) return n1 * t * t;
    else if (t < 2 / d1) return n1 * (t -= 1.5 / d1) * t + .75;
    else if (t < 2.5 / d1) return n1 * (t -= 2.25 / d1) * t + .9375;
    else return n1 * (t -= 2.625 / d1) * t + .984375;
  }
};

function sampleParam(value, timeMs) {
  if (!Array.isArray(value) || value.length === 0) return value;
  if (typeof value[0] !== 'object' || value[0] === null || !('time' in value[0])) return value;
  const kfs = value;
  if (timeMs <= kfs[0].time) return kfs[0].value;
  if (timeMs >= kfs[kfs.length - 1].time) return kfs[kfs.length - 1].value;
  for (let i = 0; i < kfs.length - 1; i++) {
    const prev = kfs[i], next = kfs[i + 1];
    if (timeMs >= prev.time && timeMs < next.time) {
      const dur = next.time - prev.time;
      if (dur === 0) return prev.value;
      const localT = (timeMs - prev.time) / dur;
      const easedT = (Easings[prev.easing] || Easings.linear)(localT);
      if (typeof prev.value === 'number' && typeof next.value === 'number') {
        return prev.value + (next.value - prev.value) * easedT;
      }
      if (Array.isArray(prev.value) && Array.isArray(next.value)) {
        return prev.value.map((v, j) => v + (next.value[j] - v) * easedT);
      }
      return easedT < 0.5 ? prev.value : next.value;
    }
  }
  return kfs[kfs.length - 1].value;
}

function sampleNodeParams(node, timeMs) {
  const out = {};
  for (const [key, value] of Object.entries(node.params || {})) {
    out[key] = sampleParam(value, timeMs);
  }
  return out;
}

function topoSort(nodes) {
  const inDeg = {}, adj = {}, idMap = {};
  for (const n of nodes) {
    idMap[n.id] = n; inDeg[n.id] = 0; adj[n.id] = [];
  }
  for (const node of nodes) {
    for (const from of (node.inputs || [])) {
      if (idMap[from]) { adj[from].push(node.id); inDeg[node.id]++; }
    }
  }
  const queue = Object.keys(inDeg).filter(id => inDeg[id] === 0);
  const sorted = [];
  while (queue.length > 0) {
    const id = queue.shift();
    sorted.push(idMap[id]);
    for (const next of adj[id]) {
      if (--inDeg[next] === 0) queue.push(next);
    }
  }
  return sorted.length === nodes.length ? sorted : null;
}

// ============================================================
// 渲染器：Canvas2D（第三步可替换为 WebGPU）
// ============================================================
function renderNode(ctx, node, params, sourceBitmap, width, height) {
  switch (node.type) {
    case 'source':
      // 源画面：绘制视频帧/图片
      if (sourceBitmap) {
        const s = Math.min(width / sourceBitmap.width, height / sourceBitmap.height);
        const dw = sourceBitmap.width * s, dh = sourceBitmap.height * s;
        ctx.drawImage(sourceBitmap, (width - dw) / 2, (height - dh) / 2, dw, dh);
      }
      break;

    case 'transform': {
      ctx.save();
      const cx = width / 2 + (params.x || 0) / 100 * width / 2;
      const cy = height / 2 + (params.y || 0) / 100 * height / 2;
      ctx.translate(cx, cy);
      if (params.rotation) ctx.rotate(params.rotation * Math.PI / 180);
      const sc = (params.scale || 100) / 100;
      ctx.scale(sc, sc);
      ctx.translate(-width / 2, -height / 2);
      ctx.globalAlpha = (params.opacity != null ? params.opacity : 100) / 100;
      break;
    }

    case 'color': {
      // Canvas2D 调色通过 filter 实现
      const filters = [];
      if (params.brightness) filters.push(`brightness(${100 + params.brightness}%)`);
      if (params.contrast) filters.push(`contrast(${100 + params.contrast}%)`);
      if (params.saturation) filters.push(`saturate(${100 + params.saturation}%)`);
      if (params.hueRotate) filters.push(`hue-rotate(${params.hueRotate}deg)`);
      if (params.blur) filters.push(`blur(${params.blur}px)`);
      if (filters.length) ctx.filter = filters.join(' ');
      break;
    }

    case 'filter': {
      if (params.type === 'glow') {
        ctx.shadowColor = params.color || 'rgba(0,229,255,0.5)';
        ctx.shadowBlur = params.intensity || 10;
      }
      break;
    }

    case 'mask': {
      // 简单矩形蒙版（后续可扩展为路径蒙版）
      break;
    }

    case 'blur': {
      ctx.filter = (ctx.filter || '') + ` blur(${params.radius || 0}px)`;
      break;
    }

    case 'chromaKey': {
      // 色度键在 Canvas2D 上用 globalCompositeOperation 模拟
      // 完整实现需要像素级操作，留给 WebGPU 阶段
      break;
    }

    case 'composite': {
      if (params.mode === 'multiply') ctx.globalCompositeOperation = 'multiply';
      else if (params.mode === 'screen') ctx.globalCompositeOperation = 'screen';
      else if (params.mode === 'overlay') ctx.globalCompositeOperation = 'overlay';
      break;
    }
  }
}

// ============================================================
// Worker 消息处理
// ============================================================
self.onmessage = function(e) {
  const { type, payload } = e.data;

  if (type === 'RENDER') {
    const { graph, timeMs, width, height, sourceBitmap } = payload;

    try {
      // 1. 拓扑排序
      const sorted = topoSort(graph.nodes);
      if (!sorted) throw new Error('RenderGraph has cycles');

      // 2. 创建离屏画布
      const offscreen = new OffscreenCanvas(width, height);
      const ctx = offscreen.getContext('2d');
      ctx.fillStyle = '#000';
      ctx.fillRect(0, 0, width, height);

      // 3. 按拓扑序执行节点
      //    source 节点绘制源画面，后续节点应用效果
      let needsRestore = false;
      for (const node of sorted) {
        const params = sampleNodeParams(node, timeMs);
        if (node.type === 'source') {
          renderNode(ctx, node, params, sourceBitmap, width, height);
        } else if (node.type === 'transform') {
          renderNode(ctx, node, params, null, width, height);
          needsRestore = true;
        } else {
          renderNode(ctx, node, params, null, width, height);
        }
      }

      // 如果有 transform 节点，重新绘制源（受变换影响）
      if (needsRestore && sourceBitmap) {
        const s = Math.min(width / sourceBitmap.width, height / sourceBitmap.height);
        const dw = sourceBitmap.width * s, dh = sourceBitmap.height * s;
        ctx.drawImage(sourceBitmap, (width - dw) / 2, (height - dh) / 2, dw, dh);
        ctx.restore();
      }

      // 4. 生成 ImageBitmap 并零拷贝回传
      const bitmap = offscreen.transferToImageBitmap();
      self.postMessage({ type: 'FRAME', bitmap, requestId: payload.requestId }, [bitmap]);

    } catch (error) {
      self.postMessage({
        type: 'ERROR',
        error: String(error),
        requestId: payload.requestId
      });
    }
  }

  if (type === 'PING') {
    self.postMessage({ type: 'PONG' });
  }
};
