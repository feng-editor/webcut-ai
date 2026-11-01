import io

with io.open('index.html', 'r', encoding='utf-8', newline='') as f:
    h = f.read()

# ============ 1. AudioFX 模块：构建 EQ/混响/降噪节点链（播放与导出共用） ============
old = """/* ============================================================
   AudioEnvelope —— 片段音量包络"""
new = """/* ============================================================
   AudioFX —— 音频滤镜：3 段 EQ + 混响 + 基础降噪（播放与导出共用节点构建）
   props: eqLow/eqMid/eqHigh (-12..12 dB), reverb ('none'|'room'|'hall'|'cave'), denoise (bool)
   ============================================================ */
var AudioFX=(function(){
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
})();

/* ============================================================
   AudioEnvelope —— 片段音量包络"""
assert old in h, 'audiofx'
h = h.replace(old, new, 1)

with io.open('index.html', 'w', encoding='utf-8', newline='') as f:
    f.write(h)
print('AudioFX module ok')
