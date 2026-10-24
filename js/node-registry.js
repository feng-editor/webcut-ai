/* ============================================================
   WebCut Node Registry (js/node-registry.js)
   —— 特效节点注册表：NodeDef → 自动生成属性面板控件
   —— 渲染端按 type 查表求值，禁止 if/switch 分支
   —— GLSL 参数走 uniform，禁止字符串拼接 shader

   第一批：transform + color（完整机制验证）
   第二批：blur + mask + chromaKey（待机制稳定后）
   ============================================================ */
(function(){
'use strict';

// ============================================================
// NodeDef 类型定义
// ============================================================
/**
 * @typedef {Object} ParamDef
 * @property {*} default            - 默认值
 * @property {number} [min]         - 滑块最小值
 * @property {number} [max]         - 滑块最大值
 * @property {number} [step]        - 滑块步长
 * @property {string} ui            - 控件类型: 'slider'|'color'|'select'|'checkbox'
 * @property {Array} [options]      - select 的选项 [{value,label}]
 * @property {string} group         - 分组: '变换'|'调色'|'蒙版'|'音频'
 * @property {string} interpolate   - 插值语义: 'linear'|'rgba'|'step'
 */

/**
 * @typedef {Object} NodeDef
 * @property {string} type              - 节点类型标识
 * @property {string} label             - 中文显示名
 * @property {Object<string, ParamDef>} params - 参数定义字典
 * @property {Function} [evaluateFn]    - Worker 内求值函数（接收采样后参数，返回 uniform 值）
 * @property {Array<string>} [inputs]   - 输入端口数量
 */

// ============================================================
// 内置节点定义（第一批：transform + color）
// ============================================================
const BUILTINS = {

  transform: {
    type: 'transform',
    label: '变换',
    group: '变换',
    params: {
      x:       { default: 0,   min: -100, max: 100, step: 1,    ui: 'slider', group: '变换', interpolate: 'linear' },
      y:       { default: 0,   min: -100, max: 100, step: 1,    ui: 'slider', group: '变换', interpolate: 'linear' },
      scale:   { default: 100, min: 10,   max: 300, step: 1,    ui: 'slider', group: '变换', interpolate: 'linear' },
      rotation:{ default: 0,   min: -180, max: 180, step: 1,    ui: 'slider', group: '变换', interpolate: 'linear' },
      opacity: { default: 100, min: 0,    max: 100, step: 1,    ui: 'slider', group: '变换', interpolate: 'linear' },
      anchor:  { default: 'center',
                 ui: 'select', group: '变换', interpolate: 'step',
                 options: [
                   { value: 'center', label: '中心' },
                   { value: 'topLeft', label: '左上' },
                   { value: 'topRight', label: '右上' },
                   { value: 'bottomLeft', label: '左下' },
                   { value: 'bottomRight', label: '右下' }
                 ]}
    },

    // Worker 内求值：接收采样后的参数，输出 uniform 值
    evaluateFn(params) {
      return {
        uTransform: buildMat4(params),
        uOpacity: (params.opacity ?? 100) / 100
      };
    }
  },

  color: {
    type: 'color',
    label: '调色',
    group: '调色',
    params: {
      brightness: { default: 0,  min: -100, max: 100, step: 1, ui: 'slider', group: '调色', interpolate: 'linear' },
      contrast:   { default: 0,  min: -100, max: 100, step: 1, ui: 'slider', group: '调色', interpolate: 'linear' },
      saturation: { default: 0,  min: -100, max: 100, step: 1, ui: 'slider', group: '调色', interpolate: 'linear' },
      temperature:{ default: 0,  min: -100, max: 100, step: 1, ui: 'slider', group: '调色', interpolate: 'linear' },
      tint:       { default: 0,  min: -100, max: 100, step: 1, ui: 'slider', group: '调色', interpolate: 'linear' },
      hueRotate:  { default: 0,  min: -180, max: 180, step: 1, ui: 'slider', group: '调色', interpolate: 'linear' },
      filterPreset: { default: 'none',
                 ui: 'select', group: '调色', interpolate: 'step',
                 options: [
                   { value: 'none', label: '无' },
                   { value: 'warm', label: '暖色' },
                   { value: 'cool', label: '冷色' },
                   { value: 'mono', label: '黑白' },
                   { value: 'vintage', label: '怀旧' },
                   { value: 'cyberpunk', label: '赛博朋克' }
                 ]}
    },

    evaluateFn(params) {
      // 预设叠加：如果选了预设，在手动参数基础上叠加预设偏移
      let preset = { brightness: 0, contrast: 0, saturation: 0, temperature: 0, hueRotate: 0 };
      switch (params.filterPreset) {
        case 'warm':     preset = { brightness: 5,  contrast: 5,  saturation: 10, temperature: 20, hueRotate: 0 }; break;
        case 'cool':     preset = { brightness: 0,  contrast: 5,  saturation: -5, temperature: -20, hueRotate: 0 }; break;
        case 'mono':     preset = { brightness: 0,  contrast: 10, saturation: -100, temperature: 0, hueRotate: 0 }; break;
        case 'vintage':  preset = { brightness: -5, contrast: 15, saturation: -30, temperature: 15, hueRotate: 5 }; break;
        case 'cyberpunk':preset = { brightness: 5,  contrast: 20, saturation: 40, temperature: -10, hueRotate: -15 }; break;
      }
      return {
        uColor: [
          clamp(params.brightness + preset.brightness, -100, 100),
          clamp(params.contrast + preset.contrast, -100, 100),
          clamp(params.saturation + preset.saturation, -100, 100),
          clamp(params.hueRotate + preset.hueRotate, -180, 180)
        ]
      };
    }
  }
};

// ============================================================
// 工具函数
// ============================================================
function clamp(v, min, max) { return Math.min(max, Math.max(min, v)); }

function buildMat4(params) {
  const scale = (params.scale ?? 100) / 100;
  const rot = (params.rotation ?? 0) * Math.PI / 180;
  const tx = (params.x ?? 0) / 100;
  const ty = -(params.y ?? 0) / 100;
  const cos = Math.cos(rot) * scale, sin = Math.sin(rot) * scale;
  return new Float32Array([cos, -sin, 0, 0, sin, cos, 0, 0, 0, 0, 1, 0, tx, ty, 0, 1]);
}

// ============================================================
// 注册表
// ============================================================
class NodeRegistry {
  constructor() {
    this.defs = new Map();
    // 注册内置节点
    for (const [key, def] of Object.entries(BUILTINS)) {
      this.defs.set(key, def);
    }
  }

  /**
   * 注册新节点类型
   * @param {string} type - 节点类型标识
   * @param {NodeDef} def - 节点定义
   */
  register(type, def) {
    if (this.defs.has(type)) {
      console.warn(`[NodeRegistry] 节点类型 "${type}" 已存在，将被覆盖`);
    }
    def.type = type;
    this.defs.set(type, def);
  }

  /**
   * 获取节点定义
   * @param {string} type
   * @returns {NodeDef|null}
   */
  get(type) {
    return this.defs.get(type) || null;
  }

  /**
   * 获取所有已注册类型
   */
  types() {
    return Array.from(this.defs.keys());
  }

  /**
   * 用默认值创建节点实例
   * @param {string} type
   * @param {string} [id]
   * @param {string[]} [inputs]
   */
  createNode(type, id, inputs) {
    const def = this.get(type);
    if (!def) throw new Error(`未注册的节点类型: ${type}`);
    const params = {};
    for (const [key, paramDef] of Object.entries(def.params)) {
      params[key] = paramDef.default;
    }
    return {
      id: id || `node_${type}_${Date.now().toString(36)}`,
      type,
      params,
      inputs: inputs || []
    };
  }

  /**
   * 校验并修正节点参数（导入预设时用，越界 clamp 并 warn）
   * @param {Object} node - RenderGraph 节点
   * @returns {Object} 修正后的节点（新对象）
   */
  validateNode(node) {
    const def = this.get(node.type);
    if (!def) {
      console.warn(`[NodeRegistry] 未知节点类型: ${node.type}，跳过`);
      return node;
    }
    const params = {};
    for (const [key, paramDef] of Object.entries(def.params)) {
      let v = node.params?.[key];
      if (v === undefined) {
        params[key] = paramDef.default;
      } else if (paramDef.min !== undefined && paramDef.max !== undefined) {
        if (v < paramDef.min || v > paramDef.max) {
          console.warn(`[NodeRegistry] ${node.type}.${key}=${v} 超出 [${paramDef.min}, ${paramDef.max}]，已 clamp`);
          v = clamp(v, paramDef.min, paramDef.max);
        }
        params[key] = v;
      } else {
        params[key] = v;
      }
    }
    return { ...node, params }; // 新对象，不修改原节点
  }

  /**
   * Schema 驱动 UI：生成属性面板控件描述
   * @param {string} type - 节点类型
   * @returns {Array<{key, label, ui, min, max, step, value, options, group}>}
   */
  uiSchema(type, currentParams) {
    const def = this.get(type);
    if (!def) return [];
    const controls = [];
    for (const [key, paramDef] of Object.entries(def.params)) {
      controls.push({
        key,
        label: key, // 可扩展为中文映射
        ui: paramDef.ui || 'slider',
        min: paramDef.min,
        max: paramDef.max,
        step: paramDef.step || 1,
        value: currentParams?.[key] ?? paramDef.default,
        options: paramDef.options,
        group: paramDef.group || def.group,
        interpolate: paramDef.interpolate || 'linear'
      });
    }
    return controls;
  }
}

// ============================================================
// 预设系统（RenderGraph JSON 序列化/反序列化）
// ============================================================
const PRESETS = {
  'none': {
    name: '无效果',
    schemaVersion: '1.0.0',
    nodes: [],
    output: null
  },
  'vintage': {
    name: '怀旧',
    schemaVersion: '1.0.0',
    nodes: [
      { id: 'n_color', type: 'color', inputs: [],
        params: { brightness: -5, contrast: 15, saturation: -30, temperature: 15, tint: 0, hueRotate: 5, filterPreset: 'vintage' } }
    ],
    output: 'n_color'
  },
  'cyberpunk': {
    name: '赛博朋克',
    schemaVersion: '1.0.0',
    nodes: [
      { id: 'n_color', type: 'color', inputs: [],
        params: { brightness: 5, contrast: 20, saturation: 40, temperature: -10, tint: 0, hueRotate: -15, filterPreset: 'cyberpunk' } }
    ],
    output: 'n_color'
  }
};

/**
 * 导出当前 RenderGraph 为预设 JSON
 */
function exportPreset(graph) {
  return JSON.stringify({
    schemaVersion: '1.0.0',
    exportedAt: new Date().toISOString(),
    nodes: graph.nodes.map(n => registry.validateNode(n)), // 校验后导出
    output: graph.output
  }, null, 2);
}

/**
 * 导入预设 JSON（校验 + clamp）
 */
function importPreset(json) {
  const data = JSON.parse(json);
  if (!data.schemaVersion) throw new Error('缺少 schemaVersion');
  if (!Array.isArray(data.nodes)) throw new Error('缺少 nodes 数组');
  return {
    version: data.schemaVersion,
    nodes: data.nodes.map(n => registry.validateNode(n)),
    output: data.output || (data.nodes.length ? data.nodes[data.nodes.length - 1].id : '')
  };
}

// ============================================================
// 导出
// ============================================================
const registry = new NodeRegistry();

window.NodeRegistry = {
  registry,
  NodeRegistry,
  BUILTINS,
  PRESETS,
  exportPreset,
  importPreset,
  clamp
};

})();
