const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));
let lastStatus = null;

function buzz(){ try { navigator.vibrate?.(18); } catch (_) {} }
function showTab(name){ $$('.tab').forEach(x => x.classList.remove('active')); $('#tab-' + name)?.classList.add('active'); $$('.bottomnav button').forEach(x => x.classList.toggle('active', x.dataset.tab === name)); }
$$('.bottomnav button').forEach(b => b.addEventListener('click', () => { buzz(); showTab(b.dataset.tab); }));

function nativeRequest(method, path, body = {}){
  if (!window.PrzewijakAndroid?.request) throw new Error('Brak natywnego mostu Android.');
  const data = JSON.parse(window.PrzewijakAndroid.request(method, path, JSON.stringify(body || {})) || '{}');
  if (!data.ok) throw new Error(data.error || 'Błąd Android bridge');
  return data;
}
const esc=s=>String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));

function defaultProfiles(){ return [
  {id:'fortuna',label:'FORTUNA',parser:'fortuna',url:'',enabled:false},
  {id:'superbet',label:'SUPERBET',parser:'superbet',url:'',enabled:false},
  {id:'betcris',label:'BETCRIS / BETSPORT',parser:'betcris_pl',url:'',enabled:false}
]; }

function renderProfiles(profiles){
  const list = $('#profileList'); if(!list) return;
  const rows = (profiles?.length ? profiles : defaultProfiles());
  list.innerHTML = rows.map((p,i)=>`<div class="profilecard" data-id="${esc(p.id)}">
    <div class="profilehead"><b>${esc(p.label)}</b><label class="inlinecheck"><input class="p-enabled" type="checkbox" ${p.enabled?'checked':''}> AKTYWNE</label></div>
    <label>Adres strony LIVE / listy eSoccer</label><input class="input p-url" type="url" value="${esc(p.url||'')}" placeholder="https://...">
    <div class="profilegrid"><div><label>Parser</label><select class="input p-parser">${['auto','fortuna','superbet','betcris_pl','betcris'].map(x=>`<option value="${x}" ${x===p.parser?'selected':''}>${x}</option>`).join('')}</select></div>
    <div><label>Podgląd strony</label><button class="btn secondary mini p-open" type="button">OTWÓRZ ŹRÓDŁO</button></div></div>
  </div>`).join('');
  $$('.p-open').forEach(btn=>btn.addEventListener('click',()=>{
    const c=btn.closest('.profilecard'); const id=c.dataset.id; try{ saveProfiles(false); nativeRequest('POST','/api/browser/open',{id}); }catch(e){alert(e.message);} }));
}

function readProfiles(){ return $$('.profilecard').map(c=>({
  id:c.dataset.id,
  label:c.querySelector('.profilehead b').textContent.trim(),
  parser:c.querySelector('.p-parser').value,
  url:c.querySelector('.p-url').value.trim(),
  enabled:c.querySelector('.p-enabled').checked
})); }
function saveProfiles(show=true){ const r=nativeRequest('POST','/api/source-profiles',{profiles:readProfiles()}); if(show){$('#profileResult').className='result good';$('#profileResult').textContent='Źródła zapisane.';} return r; }
$('#saveProfiles').addEventListener('click',()=>{buzz();try{saveProfiles(true);}catch(e){$('#profileResult').className='result bad';$('#profileResult').textContent=e.message;}});

function renderSources(status){
  const liveMap = new Map((status.live?.sources||[]).map(x=>[x.source,x]));
  const rt = status.browser?.sources||[];
  $('#sourceRuntime').innerHTML = rt.length ? rt.map(x=>{
    const l=liveMap.get(x.id)||{}; const conn=l.connection||((x.last_bridge_at||0)>0?'online':'offline');
    const cls=conn==='online'?'online':conn==='delayed'?'delayed':'offline';
    return `<div class="sourcecard ${cls}"><span class="sdot"></span><div><b>${esc(x.label)}</b><small>${esc(x.loaded_url||x.url||'brak URL')}</small></div><strong>${esc(conn.toUpperCase())}<br>${Number(l.matches||0)} mecz.</strong></div>`;
  }).join('') : '<div class="sourceempty">Brak skonfigurowanych źródeł.</div>';
}

function renderMatches(matches){
  const box=$('#matches'); if(!matches?.length){box.innerHTML='<div class="empty">Brak zaakceptowanych meczów.</div>';return;}
  box.innerHTML=matches.slice(0,60).map(m=>`<div class="match"><div class="matchtop"><div class="league">${esc(m.source_label||m.league||m.source)}</div><div class="status">${esc(m.status||'')}</div></div><div class="teams"><div class="team">${esc(m.player1||m.team1||'?')}</div><b>${m.score1??'–'}</b><div class="team">${esc(m.player2||m.team2||'?')}</div><b>${m.score2??'–'}</b></div><div class="muted micro">${esc(m.master_match_uid||'MASTER —')} · linia ${m.line??'—'} · ${esc(m.quality_grade||'')}</div></div>`).join('');
}

function syncWakeUi(enabled){const b=$('#nightMode'),h=$('#nightHint');if(enabled){b.classList.add('check');b.classList.remove('secondary');b.textContent='☀️ EKRAN MAX WŁĄCZONY';h.textContent='Android utrzymuje ekran aktywny dla tej aplikacji.';}else{b.classList.remove('check');b.classList.add('secondary');b.textContent='☀️ EKRAN MAX — NIE WYGASZAJ';h.textContent='Etap 2 jest testem collectora na pierwszym planie.';}}

async function refresh(){ try{
  const s=nativeRequest('GET','/api/status'); lastStatus=s;
  $('#connPill').className='pill '+(s.live?.running?'good':'warn'); $('#connPill').textContent=s.live?.running?'SILNIK DZIAŁA':'SILNIK STOP';
  $('#engineState').textContent=s.live?.running?'SILNIK DZIAŁA':'SILNIK ZATRZYMANY'; $('#engineDot').classList.toggle('on',!!s.live?.running); $('#sessionName').textContent=s.live?.session||'';
  $('#mMatches').textContent=s.live?.matches?.length??0; $('#mGoals').textContent=s.live?.goals??0; $('#mChanges').textContent=s.live?.changes??0; $('#mAnom').textContent=s.live?.anomalies??0;
  $('#browserState').className='collectorhint '+(s.browser?.running?'good':''); $('#browserState').textContent=s.browser?.running?'Collector uruchomiony — aktywne źródła pozostają otwarte w osobnych WebView.':'Collector zatrzymany.';
  $('#outputPath').textContent=s.output_dir||'Pamięć prywatna aplikacji';
  const st=s.storage||{}; $('#autoExport').checked=st.auto_export_on_stop!==false;
  if(st.configured){$('#storageBadge').className='collectorhint good';$('#storageBadge').textContent=(st.is_sd_card?'KARTA SD':'PAMIĘĆ TELEFONU')+' — folder gotowy do eksportu.';$('#exportNow').disabled=false;$('#clearFolder').disabled=false;}
  else{$('#storageBadge').className='collectorhint';$('#storageBadge').textContent='Wybierz folder docelowy. Może to być katalog w pamięci telefonu albo na karcie SD.';$('#exportNow').disabled=true;$('#clearFolder').disabled=true;}
  $('#snapRange').value=s.config?.snapshot_interval??5; $('#snapVal').textContent=s.config?.snapshot_interval??5; $('#oddsToggle').checked=!!s.config?.log_odds_changes; syncWakeUi(!!s.screen_awake); renderSources(s); renderMatches(s.live?.matches||[]);
  if(!$('#profileList').dataset.loaded){renderProfiles(s.source_profiles||[]);$('#profileList').dataset.loaded='1';}
 }catch(e){$('#connPill').className='pill bad';$('#connPill').textContent='BŁĄD';$('#stageState').className='collectorhint bad';$('#stageState').textContent=e.message;}}

$('#startEngine').addEventListener('click',()=>{buzz();try{nativeRequest('POST','/api/live/start');refresh();}catch(e){alert(e.message);}});
$('#stopEngine').addEventListener('click',()=>{buzz();try{nativeRequest('POST','/api/live/stop');refresh();}catch(e){alert(e.message);}});
$('#startCollector').addEventListener('click',()=>{buzz();try{saveProfiles(false);nativeRequest('POST','/api/live/start');nativeRequest('POST','/api/browser/start');refresh();}catch(e){alert(e.message);}});
$('#stopCollector').addEventListener('click',()=>{buzz();try{nativeRequest('POST','/api/browser/stop');refresh();}catch(e){alert(e.message);}});
$('#reinjectCollector').addEventListener('click',()=>{buzz();try{const r=nativeRequest('POST','/api/browser/reinject');alert('Ponownie wstrzyknięto collector do '+(r.count||0)+' źródeł.');}catch(e){alert(e.message);}});
$('#nightMode').addEventListener('click',()=>{buzz();try{const enabled=!$('#nightMode').classList.contains('check');const r=nativeRequest('POST','/api/screen-awake',{enabled});syncWakeUi(!!r.screen_awake);}catch(e){alert(e.message);}});
$('#openDevSettings').addEventListener('click',()=>{buzz();try{nativeRequest('POST','/api/open-developer-settings');}catch(e){alert(e.message);}});
$('#snapRange').addEventListener('input',e=>{$('#snapVal').textContent=e.target.value;});
$('#saveConfig').addEventListener('click',()=>{buzz();try{nativeRequest('POST','/api/config',{snapshot_interval:Number($('#snapRange').value),log_odds_changes:$('#oddsToggle').checked});$('#settingsResult').className='result good';$('#settingsResult').textContent='Ustawienia zapisane w Androidzie i RDZENIU.';}catch(e){$('#settingsResult').className='result bad';$('#settingsResult').textContent=e.message;}});

refresh(); setInterval(refresh,3000);


window.PrzewijakStorageEvent=function(r){
  const box=$('#storageResult'); if(!box) return;
  if(r?.ok){box.className='result good';box.textContent='Folder zapisany. Uprawnienie Android zostało zachowane także po ponownym uruchomieniu aplikacji.';}
  else if(r?.cancelled){box.className='result muted';box.textContent='Wybór folderu anulowany.';}
  else{box.className='result bad';box.textContent=r?.message||r?.error||'Błąd wyboru folderu.';}
};

$('#chooseFolder').addEventListener('click',()=>{buzz();try{nativeRequest('POST','/api/storage/select-folder');$('#storageResult').className='result muted';$('#storageResult').textContent='Otwieram systemowy wybór folderu Android…';}catch(e){$('#storageResult').className='result bad';$('#storageResult').textContent=e.message;}});
$('#exportNow').addEventListener('click',()=>{buzz();const b=$('#storageResult');try{b.className='result muted';b.textContent='Przygotowuję spójny snapshot SQLite i pliki eksportu…';const r=nativeRequest('POST','/api/storage/export');b.className='result good';b.textContent='Eksport zakończony: '+(r.files||0)+' plików, '+Math.round((r.bytes||0)/1024)+' KB\n'+(r.display_path||'');refresh();}catch(e){b.className='result bad';b.textContent=e.message;}});
$('#clearFolder').addEventListener('click',()=>{buzz();try{nativeRequest('POST','/api/storage/clear');$('#storageResult').className='result muted';$('#storageResult').textContent='Wybrany folder został odłączony. Dane w folderze nie zostały usunięte.';refresh();}catch(e){$('#storageResult').className='result bad';$('#storageResult').textContent=e.message;}});
$('#autoExport').addEventListener('change',e=>{try{nativeRequest('POST','/api/storage/config',{auto_export_on_stop:!!e.target.checked});$('#storageResult').className='result good';$('#storageResult').textContent=e.target.checked?'Automatyczny eksport po STOP jest włączony.':'Automatyczny eksport po STOP jest wyłączony.';}catch(err){$('#storageResult').className='result bad';$('#storageResult').textContent=err.message;}});
