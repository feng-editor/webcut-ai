/* ============================================================
   WebCut Dispatch Core (js/dispatch.js)
   —— 唯一写入口：所有对 Project 的修改必须走 App.dispatch(op)
   Op 采用阶段 4 完整形态（协作就绪），本阶段只做本地应用。

   红线：
   · 禁止原地 mutate —— 不可变更新 + structural sharing
   · 禁止 deepClone 整个 Project —— 只复制受影响路径
   · 瞬态状态（playhead/选中/悬停/滚动）不走 dispatch
   ============================================================ */
(function(){
'use strict';

// ============================================================
// Op Schema（阶段 4 完整形态，一次到位）
// ============================================================
/**
 * @typedef {Object} Op
 * @property {string} opId        - UUID，全局唯一
 * @property {string} projectId   - 项目标识
 * @property {string} actorId     - 本阶段 'local'，阶段4 由 sync 填充
 * @property {number} lamport     - 本阶段 0，阶段4 填充
 * @property {string[]} deps      - 本阶段 []，阶段4 填充
 * @property {number} baseVersion - 本阶段 0，阶段4 填充
 * @property {string} path        - JSON Pointer（RFC 6901），如 "/tracks/0/clips/2/props/x"
 * @property {'set'|'insert'|'delete'|'move'} action
 * @property {*} value            - 必须可 JSON.stringify
 * @property {{migration?:boolean,transient?:boolean}} [meta]
 */

// ============================================================
// JSON Pointer 工具（RFC 6901）
// ============================================================
const Pointer = {
  /** 解析 "/tracks/0/clips" → ["tracks", 0, "clips"] */
  parse(path) {
    if (!path || path === '/') return [];
    return path.split('/').slice(1).map(seg => {
      // URL decode + ~1→/ + ~0→~
      const dec = decodeURIComponent(seg).replace(/~1/g, '/').replace(/~0/g, '~');
      // 纯数字 → 数组索引
      return /^\d+$/.test(dec) ? parseInt(dec, 10) : dec;
    });
  },

  /** ["tracks", 0, "clips"] → "/tracks/0/clips" */
  stringify(parts) {
    return '/' + parts.map(p => {
      const s = String(p).replace(/~/g, '~0').replace(/\//g, '~1');
      return encodeURIComponent(s);
    }).join('/');
  },

  /** 读取值 */
  get(obj, path) {
    const parts = Pointer.parse(path);
    let cur = obj;
    for (const p of parts) {
      if (cur == null) return undefined;
      cur = cur[p];
    }
    return cur;
  },

  /** 不可变设置：只复制受影响路径，共享未变分支 */
  set(obj, path, value) {
    const parts = Pointer.parse(path);
    if (parts.length === 0) return value; // 根替换

    function helper(node, depth) {
      const key = parts[depth];
      const isLast = depth === parts.length - 1;

      if (Array.isArray(node)) {
        const next = node.slice(); // 浅拷贝数组（structural sharing）
        if (isLast) {
          next[key] = value;
        } else {
          next[key] = helper(node[key] || {}, depth + 1);
        }
        return next;
      } else {
        const next = Object.assign({}, node); // 浅拷贝对象
        if (isLast) {
          if (value === undefined) delete next[key];
          else next[key] = value;
        } else {
          next[key] = helper(node[key] || {}, depth + 1);
        }
        return next;
      }
    }

    return helper(obj, 0);
  },

  /** 不可变插入：path 指向数组，在 index 处插入 value */
  insert(obj, path, index, value) {
    const arr = Pointer.get(obj, path);
    if (!Array.isArray(arr)) throw new Error(`insert: ${path} 不是数组`);
    const next = arr.slice();
    next.splice(index, next.length, value); // append 或指定位置
    // 如果 index 在中间，用 splice(index, 0, value)
    if (index < arr.length) {
      next.length = arr.length;
      next.splice(index, 0, value);
    }
    return Pointer.set(obj, path, next);
  },

  /** 不可变删除 */
  delete(obj, path) {
    const parts = Pointer.parse(path);
    const parentPath = Pointer.stringify(parts.slice(0, -1));
    const key = parts[parts.length - 1];
    const parent = Pointer.get(obj, parentPath);
    if (parent == null) return obj;

    if (Array.isArray(parent)) {
      const next = parent.slice();
      next.splice(key, 1);
      return Pointer.set(obj, parentPath, next);
    } else {
      const next = Object.assign({}, parent);
      delete next[key];
      return Pointer.set(obj, parentPath, next);
    }
  }
};

// ============================================================
// Op 校验
// ============================================================
function validateOp(op) {
  if (!op || typeof op !== 'object') throw new Error('op 必须是对象');
  if (!op.opId || typeof op.opId !== 'string') throw new Error('op.opId 缺失');
  if (!op.path || typeof op.path !== 'string' || !op.path.startsWith('/')) throw new Error(`op.path 必须是 JSON Pointer（以 / 开头），得到: ${op.path}`);
  if (!['set', 'insert', 'delete', 'move'].includes(op.action)) throw new Error(`op.action 无效: ${op.action}`);
  // value 必须可序列化（set/insert 必须有 value，delete 不需要）
  if (op.action !== 'delete' && op.action !== 'move') {
    try {
      JSON.stringify(op.value);
    } catch (e) {
      throw new Error(`op.value 不可序列化: ${e.message}`);
    }
    // 禁止函数/DOM/循环引用
    if (op.value instanceof Function || op.value instanceof Node) {
      throw new Error('op.value 禁止包含函数或 DOM 节点');
    }
  }
  // move 需要 fromPath
  if (op.action === 'move' && !op.meta?.fromPath) {
    throw new Error('move 操作必须在 meta.fromPath 指定源路径');
  }
  return true;
}

// ============================================================
// Op 应用（不可变）
// ============================================================
function applyOp(project, op) {
  switch (op.action) {
    case 'set':
      return Pointer.set(project, op.path, op.value);

    case 'insert': {
      // op.path 指向数组，op.value 是要插入的元素，op.meta.index 是位置（默认 append）
      const index = op.meta?.index;
      const arr = Pointer.get(project, op.path);
      if (!Array.isArray(arr)) throw new Error(`insert: ${op.path} 不是数组`);
      const next = arr.slice();
        if (typeof index === 'number' && index >= 0 && index < next.length) {
        next.splice(index, 0, op.value);
      } else {
        next.push(op.value);
      }
      return Pointer.set(project, op.path, next);
    }

    case 'delete':
      return Pointer.delete(project, op.path);

    case 'move': {
      const fromPath = op.meta.fromPath;
      const value = Pointer.get(project, fromPath);
      if (value === undefined) throw new Error(`move: 源路径 ${fromPath} 无值`);
      // 先删源，再设目标
      const afterDelete = Pointer.delete(project, fromPath);
      return Pointer.set(afterDelete, op.path, op.value !== undefined ? op.value : value);
    }

    default:
      throw new Error(`未知 action: ${op.action}`);
  }
}

// ============================================================
// 逆操作生成（用于撤销）
// ============================================================
function invertOp(project, op) {
  const current = Pointer.get(project, op.path);
  switch (op.action) {
    case 'set':
      return { ...op, opId: crypto.randomUUID(), value: current, meta: { ...op.meta, inverse: true } };
    case 'insert': {
      const inverse = { ...op, opId: crypto.randomUUID(), action: 'delete', meta: { ...op.meta, inverse: true } };
      // insert 的逆操作是 delete 同一路径
      // 注意：如果 insert 有 index，delete 路径要指向那个 index
      if (typeof op.meta?.index === 'number') {
        inverse.path = op.path + '/' + op.meta.index;
      } else {
        // append 的逆操作是 delete 最后一个元素
        const arr = Pointer.get(project, op.path);
        inverse.path = op.path + '/' + (arr ? arr.length - 1 : 0);
      }
      return inverse;
    }
    case 'delete':
      return { ...op, opId: crypto.randomUUID(), action: 'insert', value: current, meta: { ...op.meta, inverse: true } };
    case 'move':
      return { ...op, opId: crypto.randomUUID(), path: op.meta.fromPath, meta: { ...op.meta, fromPath: op.path, inverse: true } };
    default:
      throw new Error(`无法生成逆操作: ${op.action}`);
  }
}

// ============================================================
// Dispatch 核心
// ============================================================
class DispatchCore {
  constructor(initialProject, options = {}) {
    this.project = initialProject;
    this.projectId = options.projectId || 'default';
    this.opLog = [];          // 本阶段只存储，不发网络（阶段4直接消费）
    this.undoStack = [];       // 存 [op, inverseOp] 对
    this.redoStack = [];
    this.subscribers = [];     // [{prefix, callback, id}]
    this.assertMode = options.assertMode !== false; // 开发期断言
    this.dispatchCount = 0;
    this.lastDispatchMs = 0;

    // 开发期：代理检测非 dispatch 写入
    if (this.assertMode && typeof Proxy !== 'undefined') {
      this._installWriteGuard();
    }
  }

  /**
   * 唯一写入口
   * @param {Op} op
   * @returns {{applied: boolean, nextProject: Object, inverse: Op|null}}
   */
  dispatch(op) {
    const t0 = performance.now();
    validateOp(op);

    // 补全默认字段
    const fullOp = {
      opId: op.opId || crypto.randomUUID(),
      projectId: op.projectId || this.projectId,
      actorId: op.actorId || 'local',
      lamport: op.lamport || 0,
      deps: op.deps || [],
      baseVersion: op.baseVersion || 0,
      path: op.path,
      action: op.action,
      value: op.value,
      meta: op.meta || {}
    };

    // 生成逆操作（撤销用）
    const inverse = invertOp(this.project, fullOp);

    // 不可变应用
    const nextProject = applyOp(this.project, fullOp);

    // 更新
    this.project = nextProject;
    this.opLog.push(fullOp);
    this.dispatchCount++;
    this.lastDispatchMs = performance.now() - t0;

    // 撤销栈（transient 操作不入栈）
    if (!fullOp.meta?.transient) {
      this.undoStack.push({ op: fullOp, inverse });
      if (this.undoStack.length > 100) this.undoStack.shift();
      this.redoStack = []; // 新操作清空重做栈
    }

    // 通知订阅者（按 path 前缀过滤）
    this._notify(fullOp);

    return { applied: true, nextProject, inverse };
  }

  /**
   * 撤销 = dispatch(逆操作)
   */
  undo() {
    const entry = this.undoStack.pop();
    if (!entry) return null;
    this.redoStack.push(entry);
    // 撤销也走 dispatch（但标记 transient 不入撤销栈）
    return this.dispatch({ ...entry.inverse, meta: { ...entry.inverse.meta, transient: true } });
  }

  /**
   * 重做 = dispatch(原操作)
   */
  redo() {
    const entry = this.redoStack.pop();
    if (!entry) return null;
    this.undoStack.push(entry);
    return this.dispatch({ ...entry.op, meta: { ...entry.op.meta, transient: true } });
  }

  /**
   * 订阅变更（path 前缀过滤）
   * @param {string} prefix - 如 "/tracks" 只通知 tracks 变更
   * @param {Function} callback - (op, project) => void
   * @returns {Function} 取消订阅
   */
  subscribe(prefix, callback) {
    const id = crypto.randomUUID();
    this.subscribers.push({ id, prefix, callback });
    return () => {
      this.subscribers = this.subscribers.filter(s => s.id !== id);
    };
  }

  _notify(op) {
    for (const sub of this.subscribers) {
      // path 前缀匹配：op.path 以 sub.prefix 开头，或 sub.prefix 以 op.path 的父路径开头
      if (op.path.startsWith(sub.prefix) || sub.prefix.startsWith(op.path.split('/').slice(0, -1).join('/'))) {
        try {
          sub.callback(op, this.project);
        } catch (e) {
          console.error('[dispatch] subscriber error:', e);
        }
      }
    }
  }

  /**
   * 开发期写保护：检测非 dispatch 的直接赋值
   */
  _installWriteGuard() {
    const self = this;
    const handler = {
      set(target, key, value) {
        if (!self._dispatching) {
          console.error(
            `[dispatch] 红线违规：直接写入 store.${String(key)}，必须走 App.dispatch()`,
            new Error('stack').stack
          );
        }
        target[key] = value;
        return true;
      }
    };
    this._guarded = new Proxy(this.project, handler);
  }

  /**
   * 性能测试
   */
  perfTest(durationMs = 5000) {
    const start = this.dispatchCount;
    return new Promise(resolve => {
      setTimeout(() => {
        resolve({
          dispatches: this.dispatchCount - start,
          avgMs: Math.round(this.lastDispatchMs * 1000) / 1000,
          opLogSize: this.opLog.length
        });
      }, durationMs);
    });
  }

  /**
   * 获取调度统计
   */
  stats() {
    return {
      dispatchCount: this.dispatchCount,
      lastDispatchMs: Math.round(this.lastDispatchMs * 1000) / 1000,
      undoDepth: this.undoStack.length,
      redoDepth: this.redoStack.length,
      opLogSize: this.opLog.length
    };
  }
}

// ============================================================
// 导出
// ============================================================
window.DispatchCore = DispatchCore;
window.Pointer = Pointer;
window.validateOp = validateOp;
window.invertOp = invertOp;
window.applyOp = applyOp;

// 便捷工厂：创建标准 op
window.createOp = function(path, action, value, meta) {
  return {
    opId: crypto.randomUUID(),
    projectId: 'default',
    actorId: 'local',
    lamport: 0,
    deps: [],
    baseVersion: 0,
    path,
    action,
    value,
    meta: meta || {}
  };
};

})();
