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
  this.pending=null;       // 当前等待的 Promise resolve
  this.pendingId=0;
  this.renderCount=0;
  this.lastRenderTime=0;
  this.avgRenderMs=0;
  this.enabled=typeof Worker!=='undefined'&&typeof OffscreenCanvas!=='undefined';

  if(this.enabled){
    try{
      this.worker=new Worker('js/render-worker.js');
      var self=this;
      this.worker.onmessage=function(e){
        var msg=e.data;
        if(msg.type==='FRAME'&&self.pending&&msg.requestId===self.pendingId){
          var resolve=self.pending;
          self.pending=null;
          self.renderCount++;
          var now=performance.now();
          if(self.lastRenderTime>0){
            var dt=now-self.lastRenderTime;
            self.avgRenderMs=self.avgRenderMs===0?dt:self.avgRenderMs*0.9+dt*0.1;
          }
          self.lastRenderTime=now;
          resolve({bitmap:msg.bitmap,renderMs:self.avgRenderMs});
        }else if(msg.type==='ERROR'&&self.pending){
          var reject=self.pendingReject;
          self.pending=null;
          self.pendingReject=null;
          reject(new Error(msg.error));
        }else if(msg.type==='PONG'){
          self.workerAlive=true;
        }
      };
      this.worker.onerror=function(e){
        console.error('[RenderCoordinator] Worker error:',e.message);
        self.enabled=false; // 降级到主线程
      };
      // 探活
      this.worker.postMessage({type:'PING'});
    }catch(e){
      console.warn('[RenderCoordinator] Worker 不可用，降级主线程:',e.message);
      this.enabled=false;
    }
  }
}

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
    renders:this.renderCount,
    avgMs:Math.round(this.avgRenderMs*100)/100
  };
};

window.RenderCoordinator=RenderCoordinator;
})();
