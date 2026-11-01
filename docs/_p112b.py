import io

with io.open('index.html', 'r', encoding='utf-8', newline='') as f:
    h = f.read()

# ============ 替换 AudioFX 为正确的湿/干分离实现 ============
old = """var AudioFX=(function(){
  function has(p){return (p.eqLow&&p.eqLow!==0)||(p.eqMid&&p.eqMid!==0)||(p.eqHigh&&p.eqHigh!==0)||p.reverb&&p.reverb!=='none'||p.denoise;}
  // 合成混响脉冲响应（指数衰减噪声，立体声）
  function makeIR(ctx,seconds,decay){
    var sr=ctx.sampleRate,len=Math.max(1,Math.floor(sr*seconds));
    var ir=ctx.createBuffer(2,len,sr);
    for(var ch=0;ch<2;ch++){
      var d=ir.getChannelData(ch);
      for(var i=0;i<len;i++)d[i]=(Math.random()*2-1)*Math.pow(1-i/len,decay);
    }
    return ir;
  }
  var REVERB_PRESETS={'room':{dur:0.6,decay:3},'hall':{dur:1.8,decay:2.2},'cave':{dur:3.2,decay:1.5}};
  /* 构建 FX 链：返回链尾节点（input→...→output）；无滤镜时返回 null（直连） */
  function buildChain(ctx,p){
    if(!has(p))return null;
    var first=null,last=null;
    function link(node){if(!first)first=node;else last.connect(node);last=node;}
    // 降噪：高通 80Hz 去低频隆隆 + 低通 12kHz 去高频嘶声
    if(p.denoise){
      var hp=ctx.createBiquadFilter();hp.type='highpass';hp.frequency.value=80;hp.Q.value=0.7;
      var lp=ctx.createBiquadFilter();lp.type='lowpass';lp.frequency.value=12000;lp.Q.value=0.7;
      link(hp);link(lp);
    }
    // 3 段 EQ
    if(p.eqLow||p.eqMid||p.eqHigh){
      var lo=ctx.createBiquadFilter();lo.type='lowshelf';lo.frequency.value=250;lo.gain.value=clamp(p.eqLow||0,-12,12);
      var mid=ctx.createBiquadFilter();mid.type='peaking';mid.frequency.value=1200;mid.Q.value=1;mid.gain.value=clamp(p.eqMid||0,-12,12);
      var hi=ctx.createBiquadFilter();hi.type='highshelf';hi.frequency.value=4800;hi.gain.value=clamp(p.eqHigh||0,-12,12);
      link(lo);link(mid);link(hi);
    }
    // 混响（ConvolverNode + 合成 IR，干湿混合）
    if(p.reverb&&p.reverb!=='none'&&REVERB_PRESETS[p.reverb]){
      var pre=REVERB_PRESETS[p.reverb];
      var conv=ctx.createConvolver();conv.buffer=makeIR(ctx,pre.dur,pre.decay);
      var wet=ctx.createGain();wet.gain.value=0.3;
      var dry=ctx.createGain();dry.gain.value=0.75;
      var split=ctx.createGain();
      link(split);
      split.connect(conv);conv.connect(wet);
      if(!last)last=split;
      wet.connect(ctx.destination===null?null:(function(){return null;})()||null);
      // wet 需要单独接到最终输出——由调用方在 return 后连接
      return {input:first,wet:wet,dry:last,hasReverb:true};
    }
    return {input:first,output:last,hasReverb:false};
  }
  return {has:has,buildChain:buildChain};
})();"""
new = """var AudioFX=(function(){
  function has(p){return (p.eqLow&&p.eqLow!==0)||(p.eqMid&&p.eqMid!==0)||(p.eqHigh&&p.eqHigh!==0)||(p.reverb&&p.reverb!=='none')||p.denoise;}
  // 合成混响脉冲响应（指数衰减噪声，立体声）
  function makeIR(ctx,seconds,decay){
    var sr=ctx.sampleRate,len=Math.max(1,Math.floor(sr*seconds));
    var ir=ctx.createBuffer(2,len,sr);
    for(var ch=0;ch<2;ch++){
      var d=ir.getChannelData(ch);
      for(var i=0;i<len;i++)d[i]=(Math.random()*2-1)*Math.pow(1-i/len,decay);
    }
    return ir;
  }
  var REVERB_PRESETS={'room':{dur:0.6,decay:3},'hall':{dur:1.8,decay:2.2},'cave':{dur:3.2,decay:1.5}};
  /* buildChain：返回 {input, output} —— 调用方 source.connect(input)，output.connect(后续)。
     无滤镜返回 null（调用方直连）。混响通过内部干湿混合实现。 */
  function buildChain(ctx,p){
    if(!has(p))return null;
    var nodes=[];
    if(p.denoise){
      var hp=ctx.createBiquadFilter();hp.type='highpass';hp.frequency.value=80;hp.Q.value=0.7;nodes.push(hp);
      var lp=ctx.createBiquadFilter();lp.type='lowpass';lp.frequency.value=12000;lp.Q.value=0.7;nodes.push(lp);
    }
    if(p.eqLow||p.eqMid||p.eqHigh){
      var lo=ctx.createBiquadFilter();lo.type='lowshelf';lo.frequency.value=250;lo.gain.value=clamp(p.eqLow||0,-12,12);nodes.push(lo);
      var mid=ctx.createBiquadFilter();mid.type='peaking';mid.frequency.value=1200;mid.Q.value=1;mid.gain.value=clamp(p.eqMid||0,-12,12);nodes.push(mid);
      var hi=ctx.createBiquadFilter();hi.type='highshelf';hi.frequency.value=4800;hi.gain.value=clamp(p.eqHigh||0,-12,12);nodes.push(hi);
    }
    if(p.reverb&&p.reverb!=='none'&&REVERB_PRESETS[p.reverb]){
      var pre=REVERB_PRESETS[p.reverb];
      var conv=ctx.createConvolver();conv.buffer=makeIR(ctx,pre.dur,pre.decay);
      // 干湿分离：EQ 输出 → dry(0.75) + conv(1.0) → wet(0.3) → 汇聚
      var last=nodes.length?nodes[nodes.length-1]:null;
      var dry=ctx.createGain();dry.gain.value=0.75;
      var wetG=ctx.createGain();wetG.gain.value=0.3;
      if(last){last.connect(dry);last.connect(conv);}
      conv.connect(wetG);
      var merge=ctx.createGain();
      dry.connect(merge);wetG.connect(merge);
      // 用 merge 作为 output 尾节点
      for(var i=0;i<nodes.length;i++){if(i<nodes.length-1)nodes[i].connect(nodes[i+1]);}
      return {input:nodes[0]||merge,output:merge};
    }
    for(var i=0;i<nodes.length-1;i++)nodes[i].connect(nodes[i+1]);
    return {input:nodes[0],output:nodes[nodes.length-1]};
  }
  return {has:has,buildChain:buildChain};
})();"""
assert old in h, 'audiofx v2'
h = h.replace(old, new, 1)

# ============ 2. scheduleClip：插入 FX 链 ============
old2 = """    node.playbackRate.value=sp;
    // 每节点增益包络：微淡入 + 淡出（片段自身 fadeOut 优先且更长），消除音频首尾的突发截断感
    var g=c.createGain();"""
new2 = """    node.playbackRate.value=sp;
    var fx=AudioFX.buildChain(c,clip.props||{});
    if(fx){node.connect(fx.input);var fxOut=fx.output;}
    else var fxOut=node;
    // 每节点增益包络：微淡入 + 淡出（片段自身 fadeOut 优先且更长），消除音频首尾的突发截断感
    var g=c.createGain();"""
assert old2 in h, 'sched fx'
h = h.replace(old2, new2, 1)
old3 = """    AudioEnvelope.applyTo(g,c,t0,offset,span); // 音量包络
    node.connect(g);g.connect(gain);"""
new3 = """    AudioEnvelope.applyTo(g,c,t0,offset,span); // 音量包络
    fxOut.connect(g);g.connect(gain);"""
assert old3 in h, 'sched connect'
h = h.replace(old3, new3, 1)

# ============ 3. renderAudioWindow：导出链同样插入 FX ============
old4 = """      var src2=oc.createBufferSource();src2.buffer=buf;node2.playbackRate.value=sp;node2.connect(g);"""
# 找 renderAudioWindow 里的 node2 / node 连接——直接搜实际文本
import re
# 定位 renderAudioWindow 里 full-buffer 分支的 node2
old4 = """        var node2=ctx.createBufferSource();
        node2.buffer=buf;node2.connect(g);
        try{node2.start(t0,srcP0,Math.min(srcP1-srcP0,buf.duration-srcP0));}catch(e){}"""
new4 = """        var node2=ctx.createBufferSource();
        node2.buffer=buf;node2.playbackRate.value=sp;
        var fxA=AudioFX.buildChain(ctx,clip.props||{});
        if(fxA){node2.connect(fxA.input);fxA.output.connect(g);}
        else node2.connect(g);
        try{node2.start(t0,srcP0,Math.min(srcP1-srcP0,buf.duration-srcP0));}catch(e){}"""
assert old4 in s if False else True  # renderAudioWindow 在 index.html 不在 server_flask
# renderAudioWindow 在 index.html——这个变量 s 是 server_flask。不需要在这改。
# 回到 index.html 的 renderAudioWindow 做修改
with io.open('index.html', 'r', encoding='utf-8', newline='') as f:
    h2 = f.read()

# 流式分支的 seg 连接也要 FX
old5 = """          var node=ctx.createBufferSource();
          node.buffer=seg;node.playbackRate.value=sp;node.connect(g);
          try{node.start((clip.start+(q0-si)/sp)-w0,q0-s0,q1-q0);}catch(e){}"""
new5 = """          var node=ctx.createBufferSource();
          node.buffer=seg;node.playbackRate.value=sp;
          var fxs=AudioFX.buildChain(ctx,clip.props||{});
          if(fxs){node.connect(fxs.input);fxs.output.connect(g);}
          else node.connect(g);
          try{node.start((clip.start+(q0-si)/sp)-w0,q0-s0,q1-q0);}catch(e){}"""
assert old5 in h2, 'render seg fx'
h2 = h2.replace(old5, new5, 1)

old6 = """        var node2=ctx.createBufferSource();
        node2.buffer=buf;node2.playbackRate.value=sp;node2.connect(g);
        try{node2.start(t0,srcP0,Math.min(srcP1-srcP0,buf.duration-srcP0));}catch(e){}"""
new6 = """        var node2=ctx.createBufferSource();
        node2.buffer=buf;node2.playbackRate.value=sp;
        var fxA=AudioFX.buildChain(ctx,clip.props||{});
        if(fxA){node2.connect(fxA.input);fxA.output.connect(g);}
        else node2.connect(g);
        try{node2.start(t0,srcP0,Math.min(srcP1-srcP0,buf.duration-srcP0));}catch(e){}"""
assert old6 in h2, 'render full fx'
h2 = h2.replace(old6, new6, 1)

with io.open('index.html', 'w', encoding='utf-8', newline='') as f:
    f.write(h2)
print('renderAudioWindow fx ok')
