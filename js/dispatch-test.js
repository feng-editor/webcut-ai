/* ============================================================
   Dispatch Core 自动化测试 (js/dispatch-test.js)
   在浏览器 Console 执行：node test/dispatch.test.mjs 或引入后运行
   ============================================================ */
(function(){
'use strict';

const tests = [];
let passed = 0, failed = 0;

function test(name, fn) {
  tests.push({ name, fn });
}

function assert(cond, msg) {
  if (!cond) throw new Error(msg || 'assertion failed');
}

function assertDeepEqual(a, b, msg) {
  const ja = JSON.stringify(a);
  const jb = JSON.stringify(b);
  if (ja !== jb) throw new Error(`${msg || 'not equal'}:\n  a=${ja}\n  b=${jb}`);
}

// ============================================================
// 测试用例
// ============================================================

test('Pointer.parse 基础', () => {
  const parts = Pointer.parse('/tracks/0/clips/2/props/x');
  assertDeepEqual(parts, ['tracks', 0, 'clips', 2, 'props', 'x']);
});

test('Pointer.get 读取嵌套值', () => {
  const obj = { a: { b: [{ c: 42 }] } };
  assert(Pointer.get(obj, '/a/b/0/c') === 42, '应读到 42');
});

test('Pointer.set 不可变更新', () => {
  const obj = { a: { b: 1, c: 2 }, d: { e: 3 } };
  const next = Pointer.set(obj, '/a/b', 99);
  assert(next.a.b === 99, '新值正确');
  assert(obj.a.b === 1, '原对象未变');
  assert(next.a.c === 2, '兄弟属性保留');
  assert(next.d === obj.d, '未变分支共享引用');
});

test('Pointer.set 深层嵌套不可变', () => {
  const obj = { x: { y: { z: { w: 1 } } } };
  const next = Pointer.set(obj, '/x/y/z/w', 2);
  assert(next.x.y.z.w === 2);
  assert(obj.x.y.z.w === 1);
  assert(next.x !== obj.x, '中间层已复制');
});

test('Pointer.delete 数组元素', () => {
  const obj = { list: [1, 2, 3] };
  const next = Pointer.delete(obj, '/list/1');
  assertDeepEqual(next.list, [1, 3]);
  assertDeepEqual(obj.list, [1, 2, 3]);
});

test('Pointer.delete 对象属性', () => {
  const obj = { a: 1, b: 2 };
  const next = Pointer.delete(obj, '/a');
  assert(!('a' in next));
  assert(next.b === 2);
  assert(obj.a === 1);
});

test('validateOp 拒绝无效 op', () => {
  try { validateOp(null); assert(false); } catch(e) { assert(e.message.includes('对象')); }
  try { validateOp({ opId: 'x' }); assert(false); } catch(e) { assert(e.message.includes('path')); }
  try { validateOp({ opId: 'x', path: '/a', action: 'bad' }); assert(false); } catch(e) { assert(e.message.includes('action')); }
});

test('validateOp 拒绝函数 value', () => {
  try {
    validateOp({ opId: 'x', path: '/a', action: 'set', value: () => {} });
    assert(false);
  } catch(e) { assert(e.message.includes('序列化') || e.message.includes('函数')); }
});

test('dispatch set 操作', () => {
  const dc = new DispatchCore({ count: 0 });
  dc.dispatch(createOp('/count', 'set', 42));
  assert(dc.project.count === 42);
});

test('dispatch insert 操作', () => {
  const dc = new DispatchCore({ items: ['a'] });
  dc.dispatch(createOp('/items', 'insert', 'b'));
  assertDeepEqual(dc.project.items, ['a', 'b']);
});

test('dispatch delete 操作', () => {
  const dc = new DispatchCore({ items: ['a', 'b', 'c'] });
  dc.dispatch(createOp('/items/1', 'delete'));
  assertDeepEqual(dc.project.items, ['a', 'c']);
});

test('dispatch 不可变性', () => {
  const initial = { tracks: [{ clips: [{ id: 'c1', x: 0 }] }] };
  const dc = new DispatchCore(JSON.parse(JSON.stringify(initial)));
  dc.dispatch(createOp('/tracks/0/clips/0/x', 'set', 100));
  assert(dc.project.tracks[0].clips[0].x === 100);
  assert(initial.tracks[0].clips[0].x === 0, '初始对象未变');
});

test('undo 走 dispatch', () => {
  const dc = new DispatchCore({ v: 0 });
  dc.dispatch(createOp('/v', 'set', 1));
  dc.dispatch(createOp('/v', 'set', 2));
  assert(dc.project.v === 2);

  dc.undo();
  assert(dc.project.v === 1, `undo 后 v 应为 1，得到 ${dc.project.v}`);

  dc.undo();
  assert(dc.project.v === 0, `undo 后 v 应为 0，得到 ${dc.project.v}`);
});

test('redo 走 dispatch', () => {
  const dc = new DispatchCore({ v: 0 });
  dc.dispatch(createOp('/v', 'set', 5));
  dc.undo();
  assert(dc.project.v === 0);
  dc.redo();
  assert(dc.project.v === 5);
});

test('op log 记录', () => {
  const dc = new DispatchCore({});
  dc.dispatch(createOp('/a', 'set', 1));
  dc.dispatch(createOp('/b', 'set', 2));
  assert(dc.opLog.length === 2);
  assert(dc.opLog[0].path === '/a');
  assert(dc.opLog[1].path === '/b');
});

test('subscribe 路径前缀过滤', () => {
  const dc = new DispatchCore({ tracks: [], ui: {} });
  let tracksNotified = false;
  let uiNotified = false;
  dc.subscribe('/tracks', () => tracksNotified = true);
  dc.subscribe('/ui', () => uiNotified = true);

  dc.dispatch(createOp('/tracks/0', 'set', { id: 't1' }));
  assert(tracksNotified, '/tracks 订阅应被通知');
  assert(!uiNotified, '/ui 订阅不应被通知');
});

test('structural sharing：未变分支共享', () => {
  const project = {
    trackA: { clips: [{ x: 1 }] },
    trackB: { clips: [{ x: 2 }] }
  };
  const dc = new DispatchCore(JSON.parse(JSON.stringify(project)));
  const before = dc.project;
  dc.dispatch(createOp('/trackA/clips/0/x', 'set', 99));
  assert(dc.project.trackB === before.trackB, 'trackB 应共享引用');
  assert(dc.project.trackA !== before.trackA, 'trackA 应为新对象');
});

test('连续 dispatch 性能', () => {
  const dc = new DispatchCore({ arr: new Array(100).fill(0).map((_, i) => ({ id: i, v: 0 })) });
  const t0 = performance.now();
  for (let i = 0; i < 100; i++) {
    dc.dispatch(createOp(`/arr/${i}/v`, 'set', i));
  }
  const elapsed = performance.now() - t0;
  assert(elapsed < 100, `100 次 dispatch 耗时 ${elapsed.toFixed(1)}ms，应 < 100ms`);
});

// ============================================================
// 运行测试
// ============================================================
function run() {
  console.log('=== Dispatch Core 测试 ===\n');
  for (const { name, fn } of tests) {
    try {
      fn();
      console.log(`  ✓ ${name}`);
      passed++;
    } catch (e) {
      console.error(`  ✗ ${name}: ${e.message}`);
      failed++;
    }
  }
  console.log(`\n=== 结果: ${passed} 通过, ${failed} 失败, 共 ${tests.length} ===`);
  return { passed, failed, total: tests.length };
}

// 自动运行（引入即执行）
const result = run();
window.__dispatchTestResult = result;

})();
