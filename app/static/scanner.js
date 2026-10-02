(() => {
  const video = document.getElementById('video');
  const result = document.getElementById('result');
  const startBtn = document.getElementById('start-btn');
  const manual = document.getElementById('manual-token');
  const manualBtn = document.getElementById('manual-btn');
  const net = document.getElementById('net-state');
  let detector = null, stream = null, busy = false, activeToken = null;

  function setNet(){
    const online = navigator.onLine;
    net.className = 'net-state ' + (online ? 'online':'offline');
    net.textContent = online ? '● Серверная проверка доступна при соединении' : '● Нет сети: QR можно считать, но погасить билет нельзя';
  }
  setNet(); addEventListener('online',setNet); addEventListener('offline',setNet);

  function tokenFrom(raw){
    raw=(raw||'').trim();
    try { const u=new URL(raw); const parts=u.pathname.split('/').filter(Boolean); return parts[parts.length-1] || raw; } catch(e) { return raw.split('/').filter(Boolean).pop() || raw; }
  }

  async function verify(raw){
    const token=tokenFrom(raw); if(!token || busy) return; busy=true; activeToken=token;
    try{
      const r=await fetch('/api/tickets/verify/'+encodeURIComponent(token),{credentials:'same-origin'});
      const d=await r.json();
      if(!r.ok || !d.ok){show('bad','БИЛЕТ НЕ НАЙДЕН',d.message||'Не удалось проверить'); return;}
      if(d.status==='ACTIVE') show('good','БИЛЕТ ДЕЙСТВИТЕЛЕН',`${d.guest_name} · ${d.number}`,true);
      else if(d.status==='USED') show('bad','БИЛЕТ УЖЕ ИСПОЛЬЗОВАН',`${d.guest_name} · ${d.number}<br>Проход: ${d.used_at||'—'}<br>Контролёр: ${d.used_by||'—'}`);
      else show('bad','БИЛЕТ АННУЛИРОВАН',`${d.guest_name} · ${d.number}`);
    }catch(e){show('warn','НЕТ СВЯЗИ С СЕРВЕРОМ','QR считан, но проверить подлинность и отметить вход сейчас нельзя.');}
    finally{setTimeout(()=>busy=false,1200);}
  }

  function show(kind,title,body,allow=false){
    result.innerHTML=`<div class="scan-result ${kind}"><h2>${title}</h2><div class="scan-name">${body}</div>${allow?'<button id="use-btn" class="btn primary big">ПРОПУСТИТЬ</button>':''}</div>`;
    if(allow) document.getElementById('use-btn').onclick=useTicket;
  }

  async function useTicket(){
    if(!navigator.onLine){show('warn','НЕТ СЕТИ','Без сервера билет не гасим.');return;}
    const btn=document.getElementById('use-btn'); if(btn){btn.disabled=true;btn.textContent='Проверяем…';}
    try{
      const r=await fetch('/api/tickets/use/'+encodeURIComponent(activeToken),{method:'POST',credentials:'same-origin'});
      const d=await r.json();
      if(r.ok&&d.ok) show('good','ПРОХОД ОТМЕЧЕН',`${d.guest_name} · ${d.number}`);
      else show('bad','НЕ УДАЛОСЬ ПРОПУСТИТЬ',d.message||'Статус билета изменился');
    }catch(e){show('warn','НЕТ СВЯЗИ','Проход не был отмечен.');}
  }

  async function start(){
    if(!('BarcodeDetector' in window)){show('warn','КАМЕРА-СКАНЕР НЕ ПОДДЕРЖИВАЕТСЯ','Этот браузер не даёт нативный BarcodeDetector. Используй Chrome/Android или вставь ссылку/токен вручную.');return;}
    try{
      detector=new BarcodeDetector({formats:['qr_code']});
      stream=await navigator.mediaDevices.getUserMedia({video:{facingMode:{ideal:'environment'}},audio:false});
      video.srcObject=stream; await video.play(); startBtn.textContent='Камера включена'; startBtn.disabled=true; scanLoop();
    }catch(e){show('bad','НЕ УДАЛОСЬ ОТКРЫТЬ КАМЕРУ','Проверь разрешение камеры и HTTPS.');}
  }
  async function scanLoop(){
    if(detector && video.readyState>=2 && !busy){try{const codes=await detector.detect(video); if(codes.length) verify(codes[0].rawValue);}catch(e){}}
    requestAnimationFrame(scanLoop);
  }
  startBtn.onclick=start; manualBtn.onclick=()=>verify(manual.value); manual.addEventListener('keydown',e=>{if(e.key==='Enter')verify(manual.value);});
})();
