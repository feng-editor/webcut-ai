/*
 * entitlements-config.js —— 权益配置（经典脚本，挂载到 window）
 *
 * 消费方：
 *   1. paywall-test.html —— 付费墙逻辑独立测试页
 *   2. index.html（主编辑器）—— 通过 <script src> 引入，驱动特效/贴纸面板的门控与 👑 角标；
 *      若本文件缺失，编辑器降级为全量可用（见 index.html 中的 ENT 判空）。
 *
 * 为什么不用 ES Module：file:// 场景下 ES Module 会被 CORS 拦截，经典脚本则可以。
 * 阶段二接入本地服务后，可删掉末尾挂载段、恢复 export，整体迁回 ES Module。
 *
 * 唯一事实源：EFFECTS（tier 决定档位，shipped 标记是否已实现）。
 * FEATURES / PLANS 均由其派生。
 * 注意：effect_* / sticker_* 的 name 与主编辑器素材库显示名保持一致；
 *      effect_ai_portrait / effect_3d_text 为目录预留项，编辑器暂无对应素材。
 *
 * TODO(支付接入): 正式接入后 EFFECTS/PLANS/LIMITS 应由后端权益接口下发，
 * 本文件降级为「后端不可达时的 UI 兜底配置」，判定结果仅供 UI 呈现。
 * —— 2026-10 已接入：后端在位时 /api/me/entitlements 下发的 plan/limits
 *    优先于本文件（见 index.html PayClient.applyEnt 与 State.serverLimits）。
 */
(function (global) {
  'use strict';

  var EFFECTS = {
    // —— 特效：主编辑器「特效」Tab 的 6 项 + 2 项目录预留 ——
    effect_dynamic_beat:    { id: 'effect_dynamic_beat',    name: '动感律动', tier: 'free', shipped: true },
    effect_basic_filter:    { id: 'effect_basic_filter',    name: '胶片颗粒', tier: 'free', shipped: true },
    effect_starlight:       { id: 'effect_starlight',       name: '星光闪耀', tier: 'pro',  shipped: true },
    effect_advanced_glitch: { id: 'effect_advanced_glitch', name: '故障艺术', tier: 'pro',  shipped: true },
    effect_basic_blur:      { id: 'effect_basic_blur',      name: '梦幻模糊', tier: 'free', shipped: true },
    effect_neon_stroke:     { id: 'effect_neon_stroke',     name: '霓虹描边', tier: 'pro',  shipped: true },
    effect_ai_portrait:     { id: 'effect_ai_portrait',     name: 'AI 人像',  tier: 'pro',  shipped: true },
    effect_3d_text:         { id: 'effect_3d_text',         name: '3D 文字',  tier: 'pro',  shipped: false }, // 功能表标注“暂不追”，仅预留
    // —— 贴纸：主编辑器「贴纸」Tab 的 8 项 ——
    sticker_like:   { id: 'sticker_like',   name: '点赞', tier: 'free', shipped: true },
    sticker_heart:  { id: 'sticker_heart',  name: '爱心', tier: 'free', shipped: true },
    sticker_star:   { id: 'sticker_star',   name: '星星', tier: 'free', shipped: true },
    sticker_arrow:  { id: 'sticker_arrow',  name: '箭头', tier: 'free', shipped: true },
    sticker_boom:   { id: 'sticker_boom',   name: '爆炸', tier: 'pro',  shipped: true },
    sticker_laugh:  { id: 'sticker_laugh',  name: '笑哭', tier: 'free', shipped: true },
    sticker_ribbon: { id: 'sticker_ribbon', name: '彩带', tier: 'pro',  shipped: true },
    sticker_fire:   { id: 'sticker_fire',   name: '火焰', tier: 'pro',  shipped: true }
  };
  for (var k in EFFECTS) EFFECTS[k].isPremium = EFFECTS[k].tier === 'pro';

  // 数值配额与特效权限分离：导出分辨率单位为 P，watermark 表示是否强制水印
  var LIMITS = {
    free: { maxTimelines: 1,  exportResolution: 720,  watermark: true },
    pro:  { maxTimelines: 50, exportResolution: 2160, watermark: false }
  };

  function effectsOfTier(tier) {
    var out = [];
    for (var key in EFFECTS) if (EFFECTS[key].tier === tier) out.push(key);
    return out;
  }

  var PLANS = {
    free: { name: '免费版', effects: effectsOfTier('free'), limits: LIMITS.free },
    pro:  { name: '专业版', effects: Object.keys(EFFECTS),  limits: LIMITS.pro }
  };

  // 保留最初版本的公开形状（由 EFFECTS 派生），旧调用方不受影响
  var FEATURES = {
    FREE_EFFECTS: effectsOfTier('free'),
    PREMIUM_EFFECTS: effectsOfTier('pro')
  };

  // 统一判定入口：未上线（shipped=false）的特效对任何档位都返回 false
  function hasEffectPermission(planKey, effectId) {
    var effect = EFFECTS[effectId];
    if (!effect || effect.shipped === false) return false;
    var plan = PLANS[planKey];
    return !!plan && plan.effects.indexOf(effectId) >= 0;
  }

  function getPlanLimit(planKey, key) {
    var plan = PLANS[planKey];
    return plan && plan.limits ? plan.limits[key] : undefined;
  }

  // 冻结配置，防止运行时被意外改写
  function deepFreeze(obj) {
    Object.freeze(obj);
    Object.getOwnPropertyNames(obj).forEach(function (name) {
      var v = obj[name];
      if (v && typeof v === 'object' && !Object.isFrozen(v)) deepFreeze(v);
    });
    return obj;
  }
  deepFreeze(EFFECTS); deepFreeze(LIMITS); deepFreeze(PLANS); deepFreeze(FEATURES);

  global.EFFECTS = EFFECTS;
  global.PLANS = PLANS;
  global.FEATURES = FEATURES;
  global.hasEffectPermission = hasEffectPermission;
  global.getPlanLimit = getPlanLimit;
})(window);
