/* ============================================================
   WebCut DAG 渲染引擎 + 关键帧系统 (js/dag-engine.js)
   —— 渐进式架构升级第一步：数据结构与求值器（不改变现有渲染）

   设计：
   · 每个 Clip 可挂一个 RenderGraph（有向无环图）
   · 节点类型：source / transform / color / filter / mask / blur / composite
   · 参数支持静态值或关键帧数组（含缓动函数）
   · 求值器按拓扑序执行，输出采样后的参数供渲染层使用
   · 第二步将接入 Worker（OffscreenCanvas），第三步换 WebGPU

   依赖注入：DAGEngine(Engine) —— 与主编辑器解耦
   ============================================================ */
(function(){
'use strict';

/* ---------- 缓动函数库 ---------- */
var Easings={
  linear:function(t){return t;},
  easeIn:function(t){return t*t;},
  easeOut:function(t){return t*(2-t);},
  easeInOut:function(t){return t<0.5?2*t*t:-1+(4-2*t)*t;},
  step:function(t){return t<1?0:1;},
  // 扩展缓动
  easeInCubic:function(t){return t*t*t;},
  easeOutCubic:function(t){return 1-Math.pow(1-t,3);},
  easeOutBounce:function(t){
    var n1=7.5625,d1=2.75;
    if(t<1/d1)return n1*t*t;
    else if(t<2/d1)return n1*(t-=1.5/d1)*t+.75;
    else if(t<2.5/d1)return n1*(t-=2.25/d1)*t+.9375;
    else return n1*(t-=2.625/d1)*t+.984375;
  }
};

/* ---------- 节点类型定义 ---------- */
var NodeType={
  SOURCE:'source',       // 源画面（视频帧/图片）
  TRANSFORM:'transform', // 变换（位置/缩放/旋转/不透明度）
  COLOR:'color',         // 调色（亮度/对比度/饱和度/色温/色调）
  FILTER:'filter',       // 滤镜（模糊/锐化/发光等）
  MASK:'mask',           // 蒙版（线性/圆形/矩形/钢笔）
  BLUR:'blur',           // 模糊（高斯/运动/径向）
  CHROMA_KEY:'chromaKey',// 色度键（绿幕抠像）
  COMPOSITE:'composite', // 合成（叠加/混合模式）
  LUT:'lut',             // LUT 查找表
  SHARPEN:'sharpen'      // 锐化
};

/* ---------- 关键帧插值 ---------- */

/**
 * 对参数值在指定时间点采样
 * @param {*} value - 静态值或 Keyframe 数组 [{time, value, easing}]
 * @param {number} timeMs - 当前时间（毫秒）
 * @returns {*} 采样后的确定值
 */
function sampleParam(value,timeMs){
  // 非关键帧数组 → 直接返回
  if(!Array.isArray(value)||value.length===0)return value;
  if(typeof value[0]!=='object'||value[0]===null||!('time' in value[0]))return value;

  var kfs=value;
  // 边界：时间在第一个/最后一个关键帧之前/之后
  if(timeMs<=kfs[0].time)return kfs[0].value;
  if(timeMs>=kfs[kfs.length-1].time)return kfs[kfs.length-1].value;

  // 找到区间并插值
  for(var i=0;i<kfs.length-1;i++){
    var prev=kfs[i],next=kfs[i+1];
    if(timeMs>=prev.time&&timeMs<next.time){
      var dur=next.time-prev.time;
      if(dur===0)return prev.value;
      var localT=(timeMs-prev.time)/dur;
      var easing=Easings[prev.easing]||Easings.linear;
      var easedT=easing(localT);

      // 数值插值
      if(typeof prev.value==='number'&&typeof next.value==='number'){
        return prev.value+(next.value-prev.value)*easedT;
      }
      // 数组插值（颜色/坐标等）
      if(Array.isArray(prev.value)&&Array.isArray(next.value)){
        var out=[];
        for(var j=0;j<prev.value.length&&j<next.value.length;j++){
          out.push(prev.value[j]+(next.value[j]-prev.value[j])*easedT);
        }
        return out;
      }
      // 非数值：阶跃
      return easedT<0.5?prev.value:next.value;
    }
  }
  return kfs[kfs.length-1].value;
}

/**
 * 对节点所有参数在指定时间采样
 * @param {RenderNode} node - DAG 节点
 * @param {number} timeMs - 当前时间
 * @returns {Object} 采样后的参数字典
 */
function sampleNodeParams(node,timeMs){
  var out={};
  var keys=Object.keys(node.params||{});
  for(var i=0;i<keys.length;i++){
    out[keys[i]]=sampleParam(node.params[keys[i]],timeMs);
  }
  return out;
}

/* ---------- DAG 拓扑排序 ---------- */

/**
 * Kahn 算法拓扑排序
 * @param {RenderNode[]} nodes
 * @returns {RenderNode[]|null} 排序后的节点数组，有环返回 null
 */
function topoSort(nodes){
  var inDeg={},adj={},idMap={},i;
  for(i=0;i<nodes.length;i++){
    var n=nodes[i];
    idMap[n.id]=n;
    inDeg[n.id]=0;
    adj[n.id]=[];
  }
  for(i=0;i<nodes.length;i++){
    var node=nodes[i];
    var inputs=node.inputs||[];
    for(var j=0;j<inputs.length;j++){
      var from=inputs[j];
      if(idMap[from]){
        adj[from].push(node.id);
        inDeg[node.id]++;
      }
    }
  }
  var queue=[];
  var ids=Object.keys(inDeg);
  for(i=0;i<ids.length;i++){
    if(inDeg[ids[i]]===0)queue.push(ids[i]);
  }
  var sorted=[];
  while(queue.length>0){
    var id=queue.shift();
    sorted.push(idMap[id]);
    var nexts=adj[id];
    for(i=0;i<nexts.length;i++){
      inDeg[nexts[i]]--;
      if(inDeg[nexts[i]]===0)queue.push(nexts[i]);
    }
  }
  if(sorted.length!==nodes.length)return null; // 有环
  return sorted;
}

/* ---------- DAG 求值器 ---------- */

/**
 * 执行整张渲染图：拓扑排序 → 按序采样参数 → 输出最终参数集
 * 渲染层（Canvas/WebGPU）拿这个结果去绘制
 *
 * @param {RenderGraph} graph
 * @param {number} timeMs - 播放头绝对时间
 * @returns {Object} { outputId, outputParams, allParams } 或 null
 */
function evaluateGraph(graph,timeMs){
  if(!graph||!graph.nodes||!graph.nodes.length)return null;
  var sorted=topoSort(graph.nodes);
  if(!sorted)return null;

  var allParams={};
  for(var i=0;i<sorted.length;i++){
    var node=sorted[i];
    allParams[node.id]=sampleNodeParams(node,timeMs);
  }

  var outputId=graph.output||sorted[sorted.length-1].id;
  return {
    outputId:outputId,
    outputParams:allParams[outputId]||{},
    allParams:allParams,
    nodeOrder:sorted.map(function(n){return n.id;})
  };
}

/* ---------- RenderGraph 构建工具 ---------- */

/**
 * 创建默认渲染图（source → transform 链）
 * @param {Object} clipProps - 现有 clip 的 props（用于初始化参数）
 * @returns {RenderGraph}
 */
function createDefaultGraph(clipProps){
  clipProps=clipProps||{};
  return {
    version:'1.0.0',
    output:'node_transform',
    nodes:[
      {
        id:'node_source',
        type:NodeType.SOURCE,
        params:{},
        inputs:[]
      },
      {
        id:'node_transform',
        type:NodeType.TRANSFORM,
        params:{
          x:clipProps.x||0,
          y:clipProps.y||0,
          scale:clipProps.scale||100,
          rotation:clipProps.rotation||0,
          opacity:clipProps.opacity!=null?clipProps.opacity:100
        },
        inputs:['node_source']
      }
    ]
  };
}

/**
 * 在图末尾添加节点
 * @param {RenderGraph} graph
 * @param {string} nodeType
 * @param {Object} params
 * @returns {string} 新节点 ID
 */
function appendNode(graph,nodeType,params){
  var nodeId='node_'+nodeType+'_'+Date.now().toString(36);
  graph.nodes.push({
    id:nodeId,
    type:nodeType,
    params:params||{},
    inputs:[graph.output]
  });
  graph.output=nodeId;
  return nodeId;
}

/**
 * 为节点参数添加关键帧
 * @param {RenderGraph} graph
 * @param {string} nodeId
 * @param {string} paramKey
 * @param {number} timeMs
 * @param {*} value
 * @param {string} easing
 */
function addKeyframe(graph,nodeId,paramKey,timeMs,value,easing){
  var node=null;
  for(var i=0;i<graph.nodes.length;i++){
    if(graph.nodes[i].id===nodeId){node=graph.nodes[i];break;}
  }
  if(!node)return;
  if(!node.params)node.params={};

  var current=node.params[paramKey];
  var kfs;

  // 已是关键帧数组
  if(Array.isArray(current)&&current.length>0&&typeof current[0]==='object'&&'time' in current[0]){
    kfs=current;
  }else if(current!==undefined){
    // 静态值 → 转为第一个关键帧
    kfs=[{time:0,value:current,easing:'linear'}];
  }else{
    kfs=[];
  }

  // 替换同时间的帧或追加
  var replaced=false;
  for(var j=0;j<kfs.length;j++){
    if(Math.abs(kfs[j].time-timeMs)<1){
      kfs[j]={time:timeMs,value:value,easing:easing||'linear'};
      replaced=true;break;
    }
  }
  if(!replaced){
    kfs.push({time:timeMs,value:value,easing:easing||'linear'});
  }

  kfs.sort(function(a,b){return a.time-b.time;});
  node.params[paramKey]=kfs;
}

/* ---------- 导出 ---------- */
window.DAGEngine={
  Easings:Easings,
  NodeType:NodeType,
  sampleParam:sampleParam,
  sampleNodeParams:sampleNodeParams,
  topoSort:topoSort,
  evaluateGraph:evaluateGraph,
  createDefaultGraph:createDefaultGraph,
  appendNode:appendNode,
  addKeyframe:addKeyframe
};
})();
