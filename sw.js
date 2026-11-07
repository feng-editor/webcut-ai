/* WebCut Service Worker v1.7.0
   策略：静态资源 cache-first；API/媒体 network-only（不缓存动态内容） */
var CACHE='webcut-v1.7.0';
var ASSETS=[
  '/', '/index.html', '/manifest.json',
  '/js/dispatch.js', '/js/dag-engine.js', '/js/render-coordinator.js',
  '/js/render-worker-gpu.js', '/js/render-worker.js', '/js/node-registry.js',
  '/js/cloud-sync.js', '/entitlements-config.js',
  '/icons/icon-192.png', '/icons/icon-512.png'
];
self.addEventListener('install', function(e){
  e.waitUntil(caches.open(CACHE).then(function(c){ return c.addAll(ASSETS); }).then(function(){ return self.skipWaiting(); }));
});
self.addEventListener('activate', function(e){
  e.waitUntil(caches.keys().then(function(keys){
    return Promise.all(keys.filter(function(k){return k!==CACHE;}).map(function(k){return caches.delete(k);}));
  }).then(function(){ return self.clients.claim(); }));
});
self.addEventListener('fetch', function(e){
  var url = new URL(e.request.url);
  if (url.pathname.indexOf('/api/') >= 0 || url.pathname.indexOf('/ai-media/') >= 0) return; // API/媒体不缓存
  if (e.request.method !== 'GET') return;
  e.respondWith(
    caches.match(e.request).then(function(hit){
      return hit || fetch(e.request).then(function(res){
        if (res.ok && url.origin === location.origin) {
          var clone = res.clone();
          caches.open(CACHE).then(function(c){ c.put(e.request, clone); });
        }
        return res;
      });
    }).catch(function(){ return caches.match('/index.html'); })
  );
});
