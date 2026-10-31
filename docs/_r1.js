/* 视觉帧统一取帧：图片源直出位图，视频源走解码缓存；代理激活时优先代理帧 */
function visualFrame(sid,srcTime){
  var im=ImageStore.get(sid);
  if(im)return {bitmap:im.bitmap,w:im.w,h:im.h};
  if(ProxyStore.active(sid)){
    var pf=Engine.videoDecoder.getFrame(ProxyStore.proxySid(sid),srcTime);
    if(pf)return pf; // 代理缺帧 → 回落原素材
  }
  return Engine.videoDecoder.getFrame(sid,srcTime);
}
/* ============================================================
   ProxyStore —— 代理剪辑：低分辨率代理加速预览/时间线，导出强制原素材
   proxy: {proxySid, active, progress, done}（会话级；刷新后可按需重新生成）
   ============================================================ */
var ProxyStore=(function(){
  var map={};
  var exporting=false; // 导出/QC 期间强制走原素材
  function active(sid){return !exporting&&!!(map[sid]&&map[sid].active&&map[sid].done);}
  function proxySid(sid){return map[sid]?map[sid].proxySid:null;}
  function get(sid){return map[sid]||null;}
  function mark(sid,rec){map[sid]=rec;}
  function setExporting(v){exporting=v;}
  return {active:active,proxySid:proxySid,get:get,mark:mark,setExporting:setExporting};
})();
/* 代理生成：原文件逐帧解码 → 540p 画布 → 30fps 网格采样 H.264 → MP4 → 注册为镜像解码源 */
function generateProxyFor(sid,onProgress){
  var src=Engine.videoDecoder.__debug.sources[sid];
  if(!src||!src.file)return Promise.reject(new Error('原始素材文件不可用'));
  if(!src.demux||!src.demux.width)return Promise.reject(new Error('视频信息未就绪，稍后再试'));
  var exist=ProxyStore.get(sid);
  if(exist&&exist.done)return Promise.resolve(exist.proxySid);
  var d=src.demux,dur=d.duration,ow=d.width,oh=d.height;
  var H=Math.min(540,oh);if(H%2)H--;var W=Math.round(ow*H/oh/2)*2;
  if(W>=ow)return Promise.reject(new Error('原始分辨率已较低，无需代理'));
  var fps=30,N=Math.max(1,Math.min(3600,Math.round(dur*fps)));
  var rec={proxySid:null,active:false,progress:0,done:false};
  ProxyStore.mark(sid,rec);
  var cv=document.createElement('canvas');cv.width=W;cv.height=H;
  var cx=cv.getContext('2d');
  var bitrate=Math.min(8e6,Math.max(1e6,Math.round(W*H*fps*0.06)));
  var CANDS=[
    {codec:'avc1.42001F',hardwareAcceleration:'prefer-software'},
    {codec:'avc1.42001E'},
    {codec:'avc1.4D401E'},
    {codec:'avc1.42001E',hardwareAcceleration:'prefer-software'}
  ];
  var probe=Promise.resolve(null);
  CANDS.forEach(function(c2){
    probe=probe.then(function(cur){
      if(cur)return cur;
      return VideoEncoder.isConfigSupported({codec:c2.codec,width:W,height:H,bitrate:bitrate,framerate:fps})
        .then(function(r){return r.supported?c2:null;}).catch(function(){return null;});
    });
  });
  return probe.then(function(cfgBase){
    if(!cfgBase)throw new Error('无可用 H.264 编码配置');
    return new Promise(function(resolve,reject){
      var frames=[],desc=null,done=false;
      var enc=new VideoEncoder({
        output:function(chunk,meta){
          if(meta&&meta.decoderConfig&&meta.decoderConfig.description&&!desc)
            desc=new Uint8Array(meta.decoderConfig.description);
          var u=new Uint8Array(chunk.byteLength);chunk.copyTo(u);
          frames.push({data:u});
        },
        error:function(e){if(!done){done=true;reject(new Error('编码错误：'+e.message));}}
      });
      enc.configure({codec:cfgBase.codec,width:W,height:H,bitrate:bitrate,framerate:fps});
      var gridIdx=0;
      function encodeGridUpTo(tsUs){
        var gridUs=1e6/fps;
        while(gridIdx<N&&(gridIdx+1)*gridUs<=tsUs+1){
          var vf=new VideoFrame(cv,{timestamp:Math.round(gridIdx*gridUs),duration:Math.round(gridUs)});
          enc.encode(vf,{keyFrame:gridIdx%60===0});
          vf.close();
          gridIdx++;
          rec.progress=gridIdx/N*0.9;
          if(onProgress)try{onProgress(rec.progress);}catch(e2){}
        }
      }
      Engine.videoDecoder.decodeRange(sid,0,dur,function(src2,frame){
        try{
          cx.drawImage(frame.bitmap,0,0,W,H);
          encodeGridUpTo(frame.timestamp);
        }catch(e3){}
      }).then(function(){
        try{
          encodeGridUpTo(1e9);
          enc.flush().then(function(){
            try{enc.close();}catch(e4){}
            if(!desc){done=true;reject(new Error('编码器未返回描述'));return;}
            var mp4=muxMp4FromChunks(frames,W,H,desc);
            var pf=new File([mp4],'proxy.mp4',{type:'video/mp4'});
            Engine.videoDecoder.probe(pf).then(function(meta){
              if(!meta||!meta.sourceId){done=true;reject(new Error('代理注册失败'));return;}
              rec.proxySid=meta.sourceId;rec.active=true;rec.done=true;rec.progress=1;
              if(onProgress)try{onProgress(1);}catch(e5){}
              done=true;resolve(rec.proxySid);
            }).catch(function(e6){done=true;reject(e6);});
          }).catch(function(e7){if(!done){done=true;reject(new Error('flush 失败：'+(e7&&e7.message||e7)));}});
        }catch(e8){if(!done){done=true;reject(e8);}}
      }).catch(function(e9){if(!done){done=true;reject(e9);}});
    });
  });
}

