/* ============================================================
   WebCut Render Coordinator (js/render-coordinator.js)
   —— 主线程调度器：发送 RenderGraph 快照到 Worker，
      接收 ImageBitmap 零拷贝上屏。

   使用方式：
   const rc = new RenderCoordinator();
   rc.render(graph, timeMs, width, height, sourceBitmap)
     .then(bitmap => canvasCtx.drawImage(bitmap, 0, 0));
   ============================================================ */
(function(){
'use strict';

function RenderCoordinator(){
  this.worker=null;
  this.pending=null;
  this.pendingId=0;
  this.renderCount=0;
  this.lastRenderTime=0;
  this.avgRenderMs=0;
  this.backend='none';
  this.enabled=typeof Worker!=='undefined'&&typeof OffscreenCanvas!=='undefined';

  if(this.enabled){
    try{
      // 优先 GPU Worker（WebGL2 着色器渲染），失败降级 Canvas2D Worker
      this.worker=new Worker('js/render-worker-gpu.js');
      var self=this;
      this.worker.onmessage=function(e){
        var msg=e.data;
        if(msg.type==='FRAME'&&self.pending&&msg.requestId===self.pendingId){
          self.backend=msg.backend||'unknown';
          var resolve=self.pending;
          self.pending=null;
          self.renderCount++;
          var now=performance.now();
          if(self.lastRenderTime>0){
            var dt=now-self.lastRenderTime;
            self.avgRenderMs=self.avgRenderMs===0?dt:self.avgRenderMs*0.9+dt*0.1;
          }
          self.lastRenderTime=now;
          resolve({bitmap:msg.bitmap,renderMs:self.avgRenderMs,backend:self.backend});
        }else if(msg.type==='ERROR'&&self.pending){
          console.warn('[RenderCoordinator] Worker 错误:',msg.error,'→ 降级 Canvas2D');
          // GPU Worker 失败 → 切换到 Canvas2D Worker
          self._fallbackToCanvas2D();
          if(self.pending){
            var reject=self.pendingReject;
            self.pending=null;
            if(reject)reject(new Error(msg.error));
          }
        }else if(msg.type==='PONG'){
          self.workerAlive=true;
          self.backend=msg.backend||'unknown';
        }
      };
      this.worker.onerror=function(e){
        console.error('[RenderCoordinator] Worker error:',e.message);
        self._fallbackToCanvas2D();
      };
      this.worker.postMessage({type:'PING'});
    }catch(e){
      console.warn('[RenderCoordinator] Worker 不可用:',e.message);
      this.enabled=false;
    }
  }
}

RenderCoordinator.prototype._fallbackToCanvas2D=function(){
  if(this.worker)this.worker.terminate();
  try{
    this.worker=new Worker('js/render-worker.js');
    this.backend='canvas2d';
    var self=this;
    this.worker.onmessage=function(e){
      if(e.data.type==='FRAME'&&self.pending&&e.data.requestId===self.pendingId){
        var resolve=self.pending;self.pending=null;self.renderCount++;
        resolve({bitmap:e.data.bitmap,renderMs:0,backend:'canvas2d'});
      }
    };
  }catch(e){
    this.enabled=false;
  }
};

/**
 * 渲染一帧
 * @param {Object} graph - RenderGraph 快照
 * @param {number} timeMs - 当前时间（毫秒）
 * @param {number} width
 * @param {number} height
 * @param {ImageBitmap|null} sourceBitmap - 源画面（可选，视频帧）
 * @returns {Promise<{bitmap:ImageBitmap,renderMs:number}>}
 */
RenderCoordinator.prototype.render=function(graph,timeMs,width,height,sourceBitmap){
  var self=this;
  if(!this.enabled){
    return Promise.reject(new Error('Worker 不可用'));
  }
  if(this.pending){
    // 丢弃过时的渲染请求（只保留最新的）
    this.pending=null;
    if(this.pendingReject)this.pendingReject=null;
  }

  return new Promise(function(resolve,reject){
    self.pendingId++;
    self.pending=resolve;
    self.pendingReject=reject;

    self.worker.postMessage({
      type:'RENDER',
      payload:{
        graph:graph,
        timeMs:timeMs,
        width:width,
        height:height,
        sourceBitmap:sourceBitmap||null,
        requestId:self.pendingId
      }
    },sourceBitmap?[sourceBitmap]:[]); // 转移 sourceBitmap 所有权
  });
};

/**
 * 销毁 Worker
 */
RenderCoordinator.prototype.destroy=function(){
  if(this.worker){
    this.worker.terminate();
    this.worker=null;
    this.enabled=false;
  }
};

/**
 * 获取性能统计
 */
RenderCoordinator.prototype.stats=function(){
  return {
    enabled:this.enabled,
    backend:this.backend,
    renders:this.renderCount,
    avgMs:Math.round(this.avgRenderMs*100)/100
  };
};

window.RenderCoordinator=RenderCoordinator;
})();
