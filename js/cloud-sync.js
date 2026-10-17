/* ============================================================
   WebCut 云同步模块（js/cloud-sync.js）
   —— 工程云端备份/恢复：自动重试 + 上传进度 + 用量统计
   依赖注入：var CloudSync = WebCutCloud({PayClient, ProjectStore, toast});
   （经典脚本，file:// 与 http:// 部署均可加载）

   行为约定：
   · push(onProgress)：媒体本体按源只传一次（纯音频源以 WAV 主体上传，
     视频源的 AI 附加音轨走 /audio 端点），结构 JSON 每次全量；
     每个上传带 2 次退避重试（断网/超时场景），自动备份失败提示节流 5 分钟。
   · pull()：走 ProjectStore.applyRecord 统一恢复管线，成功后回写 IndexedDB。
   · status()/usage()：云端备份信息与容量占用。
   ============================================================ */
(function(){
'use strict';

/* —— AudioBuffer → 16bit PCM WAV Blob（云同步音频传输格式） —— */
function audioBufferToWav(buf){
  var ch=buf.numberOfChannels,len=buf.length,sr=buf.sampleRate;
  var ab=new ArrayBuffer(44+len*ch*2),dv=new DataView(ab);
  function ws(o,s){for(var i=0;i<s.length;i++)dv.setUint8(o+i,s.charCodeAt(i));}
  ws(0,'RIFF');dv.setUint32(4,36+len*ch*2,true);ws(8,'WAVE');ws(12,'fmt ');
  dv.setUint32(16,16,true);dv.setUint16(20,1,true);dv.setUint16(22,ch,true);
  dv.setUint32(24,sr,true);dv.setUint32(28,sr*ch*2,true);dv.setUint16(32,ch*2,true);dv.setUint16(34,16,true);
  ws(36,'data');dv.setUint32(40,len*ch*2,true);
  var chans=[],c;
  for(c=0;c<ch;c++)chans.push(buf.getChannelData(c));
  var off=44;
  for(var i=0;i<len;i++)for(c=0;c<ch;c++){
    var v=Math.max(-1,Math.min(1,chans[c][i]));
    dv.setInt16(off,v<0?v*32768:v*32767,true);off+=2;
  }
  return new Blob([ab],{type:'audio/wav'});
}

window.WebCutCloud=function(deps){
  var PayClient=deps.PayClient,ProjectStore=deps.ProjectStore,toast=deps.toast;
  var online=null,cloudSaved={},pushing=false,lastFailToast=0;

  function hdrs(){return {'X-User-Token':PayClient.token()};}
  function available(){
    if(online!=null)return Promise.resolve(online);
    return PayClient.health().then(function(ok){online=!!ok;return online;});
  }
  /* 失败重试：retries 次线性退避（网络抖动/瞬时超时） */
  function withRetry(fn,retries,gap){
    function go(i){
      return fn().catch(function(){return false;}).then(function(ok){
        if(ok)return true;
        if(i>=retries)return false;
        return new Promise(function(res){setTimeout(function(){res(go(i+1));},gap*(i+1));});
      });
    }
    return go(0);
  }
  /* XHR 上传（带进度回调；fetch 无法取上传进度） */
  function uploadBlob(sid,blob,name,mime,audio,onProgress){
    return new Promise(function(resolve){
      try{
        var xhr=new XMLHttpRequest();
        xhr.open('PUT','/api/cloud/media/'+sid+(audio?'/audio':''));
        var hs=hdrs();
        Object.keys(hs).forEach(function(k){xhr.setRequestHeader(k,hs[k]);});
        xhr.setRequestHeader('X-Media-Name',encodeURIComponent(name||sid));
        xhr.setRequestHeader('X-Media-Mime',mime||blob.type||'application/octet-stream');
        xhr.timeout=600000;
        if(onProgress)xhr.upload.onprogress=function(ev){
          if(ev.lengthComputable)onProgress(ev.loaded,ev.total);
        };
        xhr.onload=function(){resolve(xhr.status>=200&&xhr.status<300);};
        xhr.onerror=function(){resolve(false);};
        xhr.ontimeout=function(){resolve(false);};
        xhr.send(blob);
      }catch(e){resolve(false);}
    });
  }
  function pushStructure(rec){
    return fetch('/api/cloud/project',{method:'PUT',
      headers:Object.assign({'Content-Type':'application/json'},hdrs()),
      body:JSON.stringify(rec),signal:AbortSignal.timeout(20000)})
      .then(function(r){return r.ok;}).catch(function(){return false;});
  }
  function failToast(msg){ // 自动备份失败提示节流（5 分钟最多一次，避免编辑时刷屏）
    var now=Date.now();
    if(now-lastFailToast<300000)return;
    lastFailToast=now;
    toast(msg);
  }
  function push(onProgress){
    if(pushing)return Promise.resolve({ok:false,busy:true});
    return available().then(function(ok){
      if(!ok)return {ok:false,offline:true};
      pushing=true;
      var snap=ProjectStore.snapshot();
      var tasks=[];
      Object.keys(snap.media).forEach(function(sid){
        if(cloudSaved[sid]||!snap.media[sid])return;
        var m=snap.media[sid];
        if(m.blob)tasks.push({sid:sid,blob:m.blob,name:m.name,mime:m.mime,audio:false});
        if(m.audioPcm){ // 纯音频源→WAV 主体；视频源的附加音轨→/audio 端点
          var wav=audioBufferToWav(ProjectStore.pcmToBuffer(m));
          tasks.push({sid:sid,blob:wav,name:(m.name||sid)+'.wav',mime:'audio/wav',audio:!!m.blob});
        }
      });
      var total=0;tasks.forEach(function(t){total+=t.blob.size;});
      var done=0,chain=Promise.resolve(true);
      tasks.forEach(function(t){
        chain=chain.then(function(acc){
          return withRetry(function(){
            return uploadBlob(t.sid,t.blob,t.name,t.mime,t.audio,function(l,tot){
              if(onProgress&&total>0)onProgress(Math.min(1,(done+Math.min(l,tot))/total),'正在上传素材');
            });
          },2,1500).then(function(ok){
            done+=t.blob.size;
            if(ok)cloudSaved[t.sid]=1;
            return acc&&ok;
          });
        });
      });
      return chain.then(function(mediaOk){
        return withRetry(function(){return pushStructure(snap.rec);},2,1500).then(function(stOk){
          pushing=false;
          var okAll=mediaOk&&stOk;
          if(okAll){
            ProjectStore.setCloudAt(Date.now());
            if(onProgress)onProgress(1,'备份完成');
          }else if(!stOk){
            failToast('云备份失败（网络异常），本地保存未受影响；可稍后在个人中心手动重试');
          }
          return {ok:okAll,media:mediaOk,structure:stOk};
        });
      });
    }).catch(function(e){pushing=false;return {ok:false,err:(e&&e.message)||String(e)};});
  }
  function status(){
    return available().then(function(ok){
      if(!ok)return null;
      return fetch('/api/cloud/status',{headers:hdrs(),signal:AbortSignal.timeout(5000)})
        .then(function(r){return r.json();}).catch(function(){return null;});
    });
  }
  function usage(){
    return available().then(function(ok){
      if(!ok)return null;
      return fetch('/api/cloud/usage',{headers:hdrs(),signal:AbortSignal.timeout(5000)})
        .then(function(r){return r.json();}).catch(function(){return null;});
    });
  }
  function pull(){
    return available().then(function(ok){
      if(!ok)return false;
      return fetch('/api/cloud/project',{headers:hdrs(),signal:AbortSignal.timeout(15000)})
        .then(function(r){
          if(!r.ok)throw new Error('云端暂无备份');
          return r.json();
        })
        .then(function(rec){
          return ProjectStore.applyRecord(rec,function(sid){
            var meta=(rec.mediaMeta||{})[sid]||{};
            return fetch('/api/cloud/media/'+sid,{headers:hdrs(),signal:AbortSignal.timeout(600000)})
              .then(function(r){return r.ok?r.blob():null;})
              .then(function(blob){
                if(!blob)return null;
                var m={blob:blob,name:meta.name,mime:meta.mime};
                if(meta.audio){ // 视频源的 AI 附加音轨
                  return fetch('/api/cloud/media/'+sid+'/audio',{headers:hdrs(),signal:AbortSignal.timeout(120000)})
                    .then(function(r2){return r2.ok?r2.blob():null;})
                    .then(function(wav){if(wav)m.audioWav=wav;return m;});
                }
                return m;
              }).catch(function(){return null;});
          }).then(function(applied){
            if(applied){
              cloudSaved={};
              Object.keys(rec.mediaMeta||{}).forEach(function(s){cloudSaved[s]=1;}); // 云端已有，不再上传
              ProjectStore.saveNow(); // 云端副本回写 IndexedDB
            }
            return applied;
          });
        });
    }).catch(function(e){toast('云端恢复失败：'+((e&&e.message)||e));return false;});
  }
  return {push:push,status:status,usage:usage,pull:pull};
};
})();
