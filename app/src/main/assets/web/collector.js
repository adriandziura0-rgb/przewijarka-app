(()=>{
  const COLLECTOR_VERSION='2.9.16';
  const ENDPOINT='__ENDPOINT__';
  const HEARTBEAT_ENDPOINT='__HEARTBEAT_ENDPOINT__';
  const STATUS_ENDPOINT='__STATUS_ENDPOINT__';
  if(window.__PRZEWIJAK_SOURCE_RETIRED__)return;
  if(window.__PRZEWIJAK_COLLECTOR__){
    try{
      if(window.__PRZEWIJAK_COLLECTOR__.version===COLLECTOR_VERSION){window.__PRZEWIJAK_COLLECTOR__.poke?.();return;}
      window.__PRZEWIJAK_COLLECTOR__.stop?.();
    }catch(e){}
    window.__PRZEWIJAK_COLLECTOR__=null;
  }
  const pill=document.createElement('div');
  pill.setAttribute('data-przewijak-overlay','1');
  Object.assign(pill.style,{position:'fixed',right:'8px',bottom:'8px',zIndex:'2147483647',background:'#111827',color:'#fff',padding:'8px 10px',borderRadius:'12px',font:'700 12px system-ui',boxShadow:'0 2px 12px #0008',opacity:'.92'});
  pill.textContent='LIVE → PC: start';
  document.documentElement.appendChild(pill);

  const normBlock=s=>(s||'').replace(/\u00a0/g,' ').replace(/[ \t]+\n/g,'\n').replace(/\n[ \t]+/g,'\n').replace(/\n{3,}/g,'\n\n').trim();
  const normInline=s=>(s||'').replace(/\u00a0/g,' ').replace(/\s+/g,' ').trim();
  const num=s=>{const n=Number(String(s??'').replace(',','.'));return Number.isFinite(n)?n:null;};
  const visible=el=>{if(!el||!(el instanceof Element)||el.closest('[data-przewijak-overlay="1"]'))return false;const st=getComputedStyle(el);if(st.display==='none'||st.visibility==='hidden'||Number(st.opacity||'1')===0)return false;const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};

  const scanRows=()=>{
    const vw=Math.max(1,innerWidth||1),vh=Math.max(1,innerHeight||1),tokens=[];
    const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);let node,guard=0;
    while((node=walker.nextNode())&&guard<11000){
      guard++;const text=normInline(node.nodeValue||'');if(!text)continue;const parent=node.parentElement;if(!visible(parent))continue;
      const tag=(parent.tagName||'').toLowerCase();if(['script','style','noscript','svg'].includes(tag))continue;
      try{const range=document.createRange();range.selectNodeContents(node);for(const rr of Array.from(range.getClientRects())){
        if(!rr||rr.width<.5||rr.height<.5||rr.width>vw*1.25||rr.height>130)continue;
        tokens.push({text,x:rr.x,y:rr.y,right:rr.right,bottom:rr.bottom,w:rr.width,h:rr.height,cx:rr.x+rr.width/2,cy:rr.y+rr.height/2});if(tokens.length>=5200)break;
      }}catch(e){}if(tokens.length>=5200)break;
    }
    const uniq=[],seen=new Set();for(const t of tokens){const sig=[t.text,Math.round(t.x),Math.round(t.y),Math.round(t.w),Math.round(t.h)].join('|');if(seen.has(sig))continue;seen.add(sig);uniq.push(t);}uniq.sort((a,b)=>(a.cy-b.cy)||(a.x-b.x));
    const rows=[];for(const t of uniq){let best=null,bestD=999;for(let j=Math.max(0,rows.length-14);j<rows.length;j++){const r=rows[j],d=Math.abs(r.cy-t.cy),tol=Math.max(3.5,Math.min(8,Math.max(r.h,t.h)*.38));if(d<=tol&&d<bestD){best=r;bestD=d;}}if(!best){best={cy:t.cy,h:t.h,tokens:[]};rows.push(best);}best.tokens.push(t);const n=best.tokens.length;best.cy=((best.cy*(n-1))+t.cy)/n;best.h=Math.max(best.h,t.h);}
    for(const r of rows){r.tokens.sort((a,b)=>a.x-b.x);r.text=normInline(r.tokens.map(t=>t.text).join(' '));r.x=Math.min(...r.tokens.map(t=>t.x));r.right=Math.max(...r.tokens.map(t=>t.right));r.y=Math.min(...r.tokens.map(t=>t.y));}
    rows.sort((a,b)=>(a.cy-b.cy)||(a.x-b.x));
    return{vw,vh,uniq,rows};
  };

  // Adapter Fortuny: wynik jest brany z geometrii wierszy, nie z luźnych liczb.
  const parseFortuna=(rows)=>{
    const teamRe=/^(.*?\S)\s*\(([^()]{1,60})\)(?:\s|$)/,statusRe=/^([12])\.\s*poł(?:\.|owa)?(?:\s*-\s*(?:(\d{1,2}):(\d{2})|(\d+)\s*m)|\s*<\s*1\s*m)?/i,finishedRe=/(?:zakończ|koniec|finished|full\s*time|^ft$)/i,leagueRe=/(esports?\s*battle|esportsbattle|e-?soccer|esoccer|efootball|volta|gt\s*league|interactive\s+world\s+cup)/i,intRe=/^\d{1,2}$/;
    const parseTeamRow=row=>{const m=teamRe.exec(row.text||'');if(!m)return null;const team=normInline(m[1]),player=normInline(m[2]);if(!team||!player)return null;let joined='',teamRight=row.x;for(const t of row.tokens){joined=normInline(joined+' '+t.text);teamRight=Math.max(teamRight,t.right);if(joined.includes('('+player+')'))break;}const c=row.tokens.filter(t=>intRe.test(t.text)).map(t=>({...t,value:Number(t.text)})).filter(t=>Number.isFinite(t.value)&&t.value<=40).filter(t=>t.x>=teamRight+2&&t.x-teamRight<=520).sort((a,b)=>(a.x-teamRight)-(b.x-teamRight));const score=c.length?c[0]:null;return{team,player,x:row.x,y:row.cy,teamRight,score:score?score.value:null,scoreX:score?score.x:null,scoreCandidateCount:c.length,rowText:row.text};};
    const tr=[];for(let i=0;i<rows.length;i++){const p=parseTeamRow(rows[i]);if(p)tr.push({rowIndex:i,row:rows[i],...p});}
    const records=[];for(let i=0;i+1<tr.length;i++){const a=tr[i],b=tr[i+1],dy=b.y-a.y;if(dy<10||dy>75||Math.abs(a.x-b.x)>90)continue;let crossed=false;for(let ri=a.rowIndex+1;ri<b.rowIndex;ri++){if(statusRe.test(rows[ri].text||'')){crossed=true;break;}}if(crossed)continue;let status='',half=null,minute=null,clock_second_in_period=null,league='';for(let ri=a.rowIndex-1;ri>=Math.max(0,a.rowIndex-18);ri--){const txt=rows[ri].text||'',v=a.y-rows[ri].cy;if(!status&&v<=130){if(finishedRe.test(txt)){status=txt;}else{const sm=statusRe.exec(txt);if(sm){status=txt;half=Number(sm[1]);minute=sm[2]?Number(sm[2]):(sm[4]?Number(sm[4]):(txt.includes('<')?0:null));clock_second_in_period=sm[2]?(Number(sm[2])*60+Number(sm[3])):(sm[4]?Number(sm[4])*60:(txt.includes('<')?0:null));}}}if(!league&&v<=320&&leagueRe.test(txt))league=txt;if(status&&league)break;}let confidence=0;if(a.score!==null&&b.score!==null){const aligned=a.scoreX!==null&&b.scoreX!==null&&Math.abs(a.scoreX-b.scoreX)<=55;confidence=aligned?.78:.35;if(a.scoreCandidateCount===1&&b.scoreCandidateCount===1)confidence+=.08;if(aligned&&Math.abs(a.scoreX-b.scoreX)<=28)confidence+=.09;if(dy>=18&&dy<=52)confidence+=.04;confidence=Math.min(.99,confidence);}records.push({league,status,half,minute,clock_second_in_period,team1:a.team,player1:a.player,team2:b.team,player2:b.player,score1:a.score,score2:b.score,total_goals:(a.score!==null&&b.score!==null)?a.score+b.score:null,_score_source:'layout_geometry',_score_confidence:confidence,_layout_adapter:'fortuna',_layout_debug:JSON.stringify({row1:a.rowText,row2:b.rowText,scoreX1:a.scoreX,scoreX2:b.scoreX,candidates1:a.scoreCandidateCount,candidates2:b.scoreCandidateCount,y1:a.y,y2:b.y})});}
    const best=new Map();for(const r of records){const k=`${(r.player1||'').toLowerCase()}|${(r.player2||'').toLowerCase()}`;if(!k||k==='|')continue;const old=best.get(k);if(!old||Number(r._score_confidence||0)>Number(old._score_confidence||0))best.set(k,r);}return Array.from(best.values());
  };


  const superbetScoreMemory=new Map();

  // Dedykowany adapter Superbet. Nie dziedziczy heurystyk Fortuny:
  // Superbet ma inne nazwy lig (Battle/H2H/GT/EAL/Cyber), osobny wiersz formatu
  // oraz statusy typu "1.Połowa4+2'". Wynik nadal bierzemy wyłącznie z geometrii.
  const parseSuperbet=(rows)=>{
    const teamRe=/^(.*?\S)\s*\(([^()]{1,60})\)(?:\s|$)/;
    const leagueRe=/(?:^|\b)(battle\s*-|h2h\s*-|gg\s*league|gt\s*-|cyber\s+live\s+arena|volta\b|eal\s*-|interactive\s+world\s+cup|esoccer|e-?soccer)(?:\b|\s|$)/i;
    const categoryNoise=/^(?:e-?piłka\s+nożna|wszystko|live|mecz|liczba\s+goli|1\.?\s*połowa\s*-?\s*liczba\s+goli)$/i;
    const teamPrefixNoise=/^(?:(?:badminton|e-?koszyk[oó]wka|e-?hokej(?:\s+na\s+lodzie)?|koszyk[oó]wka|hokej(?:\s+na\s+lodzie)?|tenis\s+sto[lł]owy|table\s+tennis|tenis|siatk[oó]wka|pi[lł]ka\s+r[eę]czna|baseball|rugby|cricket|darts|snooker|mma|boks)\s+){1,3}/i;
    const cleanTeam=s=>{let z=normInline(s);for(let i=0;i<3;i++){const n=z.replace(/^([A-Za-zÀ-ž-]{3,30})\s+\1\s+/i,'').trim();if(n===z)break;z=n;}z=z.replace(teamPrefixNoise,'').replace(/^[\s\-–—·]+|[\s\-–—·]+$/g,'').trim();return z||normInline(s);};
    const formatRe=/mecze\s+rozgrywane\s+w\s+formacie\s*([234]\s*[x×]\s*\d+)\s*min(?:ut(?:y|\.)?)?/i;
    const statusRe=/^(?:([12])\.?\s*połowa\s*(?:(\d{1,3})(?:\+(\d{1,2}))?\s*['’′]?)?|przerwa\s*(?:(\d{1,3})(?:\+(\d{1,2}))?\s*['’′]?)?)/i;
    const scoreInt=/^\d{1,2}$/;
    const marketSide=txt=>/\b(powyżej|powyzej|więcej|wiecej|over)\b/i.test(txt)?'over':(/\b(poniżej|ponizej|mniej|under)\b/i.test(txt)?'under':null);
    const isLeague=txt=>{const z=normInline(txt);return !!z&&!categoryNoise.test(z)&&leagueRe.test(z);};
    const parseTeamRow=(row,rowIndex)=>{
      const m=teamRe.exec(row.text||'');if(!m)return null;
      const team=cleanTeam(m[1]),player=normInline(m[2]);if(!team||!player)return null;
      let joined='',labelRight=row.x;
      for(const t of row.tokens){joined=normInline(joined+' '+t.text);labelRight=Math.max(labelRight,t.right);if(joined.includes('('+player+')'))break;}
      const viewportW=Math.max(1,innerWidth||1);
      const scoreCandidates=row.tokens.filter(t=>scoreInt.test(t.text)).map(t=>({...t,value:Number(t.text)}))
        .filter(t=>Number.isFinite(t.value)&&t.value<=40&&t.right>0&&t.x<viewportW&&t.x>=labelRight+2&&t.x-labelRight<=620)
        .sort((a,b)=>a.x-b.x);
      return{rowIndex,row,team,player,x:row.x,y:row.cy,labelRight,scoreCandidates,scoreCandidateCount:scoreCandidates.length,rowText:row.text};
    };
    const chooseScorePair=(a,b)=>{
      const pairs=[];
      const key=`${String(a.player||'').toLowerCase()}|${String(b.player||'').toLowerCase()}`;
      const rawPrev=superbetScoreMemory.get(key);
      const prev=rawPrev&&(Date.now()-Number(rawPrev.seen||0)<=45000)?rawPrev:null;
      if(rawPrev&&!prev)superbetScoreMemory.delete(key);
      for(const sa of a.scoreCandidates||[])for(const sb of b.scoreCandidates||[]){
        const dx=Math.abs(sa.x-sb.x);if(dx>110)continue;
        const avgX=(sa.x+sb.x)/2;
        const avgRelX=((sa.x-a.labelRight)+(sb.x-b.labelRight))/2;
        const total=Number(sa.value)+Number(sb.value);
        const regressive=!!prev&&(Number(sa.value)<Number(prev.s1)||Number(sb.value)<Number(prev.s2));
        const prevRel=prev?(Number.isFinite(Number(prev.relX))?Number(prev.relX):Number(prev.x||avgRelX)):avgRelX;
        const xdiff=prev?Math.abs(avgRelX-prevRel):0;
        pairs.push({sa,sb,dx,avgX,avgRelX,total,regressive,xdiff,key,prev});
      }
      if(!pairs.length)return null;
      // After the first stable observation, stay on the same score column and prefer
      // monotonic score candidates. This prevents a rerender from jumping between
      // CURRENT and a neighbouring 1P/other-card score column.
      pairs.sort((u,v)=>{
        if(prev){
          if(u.regressive!==v.regressive)return u.regressive?1:-1;
          // The current full-match score cannot be below a period/old score. When
          // several aligned numeric columns exist, prefer the largest total and the
          // same RELATIVE column position. Relative X survives carousel/card shifts.
          if(u.regressive&&u.total!==v.total)return v.total-u.total;
          if(Math.abs(u.xdiff-v.xdiff)>1)return u.xdiff-v.xdiff;
          if(u.total!==v.total)return v.total-u.total;
        }else if(u.total!==v.total){
          // On first sight prefer the largest aligned score pair: a 1P subtotal can
          // never exceed the live full-match total.
          return v.total-u.total;
        }
        return (u.dx-v.dx)||(v.avgX-u.avgX);
      });
      return pairs[0];
    };
    const teams=[];for(let i=0;i<rows.length;i++){const t=parseTeamRow(rows[i],i);if(t)teams.push(t);}
    const mainTotalHeading=txt=>/\bliczba\s+goli\b/i.test(txt)&&!/liczba\s+goli\s+drużyny/i.test(txt)&&!/1\.?\s*połowa\s*-?\s*liczba\s+goli/i.test(txt);
    const stopMarketHeading=txt=>/^(?:liczba\s+goli\s+drużyny|asian\s+total\s+goals|parzysta\s*\/\s*nieparzysta|parzysta\/nieparzysta|podwójna\s+szansa|dokładny\s+wynik|handicap|następny\s+gol|\d+\.\s*gol\b|mecz\b)/i.test(txt);
    const parseMarket=(startIndex,maxY)=>{
      let line=null,under=null,over=null,mainSeen=false;
      const absorb=(side,segment,nextRowText='')=>{
        const nums=(String(segment||'').match(/\d+(?:[.,]\d+)?/g)||[]).map(num).filter(x=>x!==null);
        if(!nums.length)return;
        const ln=nums[0];let odd=null;
        if(nums.length>=3)odd=nums[nums.length-1];
        else if(nums.length===2&&Math.abs(nums[1]-nums[0])>1e-9)odd=nums[1];
        if(odd===null&&nextRowText){const ns=(String(nextRowText).match(/^\s*(\d+(?:[.,]\d+)?)\s*$/)||[]);if(ns[1]){const q=num(ns[1]);if(q!==null&&q>=1.001&&q<=100)odd=q;}}
        if(ln!==null&&ln>=0&&ln<=40)line=ln;
        if(side==='over'&&odd!==null&&odd>=1.001&&odd<=100)over=odd;
        if(side==='under'&&odd!==null&&odd>=1.001&&odd<=100)under=odd;
      };
      for(let ri=startIndex;ri<rows.length&&ri<startIndex+34;ri++){
        const row=rows[ri];if(row.cy>maxY)break;const txt=normInline(row.text||'');if(!txt)continue;
        if(isLeague(txt)||statusRe.test(txt))break;
        if(mainTotalHeading(txt))mainSeen=true;
        if(mainSeen&&stopMarketHeading(txt)&&!/\bliczba\s+goli\b/i.test(txt))break;
        if(mainSeen&&/liczba\s+goli\s+drużyny|asian\s+total\s+goals/i.test(txt))break;
        // Fallback for compact LIVE cards where the heading and both sides are one row.
        if(!mainSeen&&/goli\s+w\s+meczu/i.test(txt)&&!/\bstrzeli\b/i.test(txt)&&marketSide(txt))mainSeen=true;
        if(!mainSeen)continue;
        const hits=Array.from(txt.matchAll(/\b(poniżej|ponizej|mniej|under|powyżej|powyzej|więcej|wiecej|over)\b/ig));
        if(hits.length){
          for(let hi=0;hi<hits.length;hi++){
            const side=/powyżej|powyzej|więcej|wiecej|over/i.test(hits[hi][1])?'over':'under';
            const seg=txt.slice(hits[hi].index,hi+1<hits.length?hits[hi+1].index:txt.length);
            const nextText=(hi===hits.length-1&&ri+1<rows.length)?String(rows[ri+1].text||''):'';
            absorb(side,seg,nextText);
          }
        }
        if(line!==null&&under!==null&&over!==null)break;
      }
      return{line,under_odds:under,over_odds:over};
    };
    const records=[];
    for(let i=0;i+1<teams.length;i++){
      const a=teams[i],b=teams[i+1],dy=b.y-a.y;
      if(dy<10||dy>135||Math.abs(a.x-b.x)>150)continue;
      const scorePair=chooseScorePair(a,b);if(!scorePair)continue;
      const aScore=scorePair.sa.value,bScore=scorePair.sb.value,aScoreX=scorePair.sa.x,bScoreX=scorePair.sb.x;
      let league='',format='',status='',half=null,minute=null,clock_second_in_period=null,leagueIndex=-1;
      for(let ri=a.rowIndex-1;ri>=Math.max(0,a.rowIndex-38);ri--){const txt=normInline(rows[ri].text||'');const dist=a.y-rows[ri].cy;if(dist>520)break;if(!format){const fm=formatRe.exec(txt);if(fm)format=normInline(fm[1]).replace(/[×]/g,'x').replace(/\s+/g,'');}if(!league&&isLeague(txt)){league=txt;leagueIndex=ri;}if(league&&format)break;}
      if(!league)continue;
      for(let ri=a.rowIndex-1;ri>=Math.max(leagueIndex>=0?leagueIndex:0,a.rowIndex-14);ri--){const txt=normInline(rows[ri].text||'');if(/(?:zakończ|koniec|finished|full\s*time|^ft$)/i.test(txt)){status=txt;break;}const sm=statusRe.exec(txt);if(!sm)continue;status=txt;if(sm[1]!==undefined){half=Number(sm[1]);const base=sm[2]===undefined?null:Number(sm[2]),added=sm[3]===undefined?0:Number(sm[3]);if(base!==null){minute=base+added;clock_second_in_period=null;}}else{half=1;const base=sm[4]===undefined?null:Number(sm[4]),added=sm[5]===undefined?0:Number(sm[5]);if(base!==null){minute=base+added;clock_second_in_period=null;}}break;}
      const market=parseMarket(b.rowIndex+1,b.y+420);
      let confidence=.82;if(Math.abs(aScoreX-bScoreX)<=50)confidence+=.08;if(scorePair.dx<=25)confidence+=.05;if(dy>=20&&dy<=90)confidence+=.04;confidence=Math.min(.99,confidence);
      const scoreColumnX=Math.round((aScoreX+bScoreX)/2);
      const scoreBindingX=Math.round(scorePair.avgRelX);
      const scoreBinding=`rel${Math.round(scoreBindingX/10)*10}:${Math.round(dy/5)*5}`;
      const clearReset=!!scorePair.regressive&&half===1&&minute!==null&&minute<=1&&(aScore+bScore)<=1;
      const ambiguousRegression=!!scorePair.regressive&&!clearReset;
      if(!ambiguousRegression){superbetScoreMemory.set(scorePair.key,{relX:scoreBindingX,x:scoreColumnX,s1:aScore,s2:bScore,seen:Date.now()});}
      records.push({league,format,status,half,minute,clock_second_in_period,team1:a.team,player1:a.player,team2:b.team,player2:b.player,score1:ambiguousRegression?null:aScore,score2:ambiguousRegression?null:bScore,total_goals:ambiguousRegression?null:(aScore+bScore),_score_candidate1:aScore,_score_candidate2:bScore,line:market.line,under_odds:market.under_odds,over_odds:market.over_odds,_score_source:'layout_geometry',_score_confidence:ambiguousRegression?0:confidence,_score_binding_suspect:ambiguousRegression,_layout_adapter:'superbet_v6',_score_column_x:scoreColumnX,_score_binding_x:scoreBindingX,_score_binding:scoreBinding,_clock_mode:'absolute_match_minute',_layout_debug:JSON.stringify({row1:a.rowText,row2:b.rowText,scoreX1:aScoreX,scoreX2:bScoreX,scoreRelX:scoreBindingX,scoreCandidates1:(a.scoreCandidates||[]).map(x=>[x.value,x.x]),scoreCandidates2:(b.scoreCandidates||[]).map(x=>[x.value,x.x]),league,format,status,market,ambiguousRegression})});
    }
    const best=new Map();for(const r of records){const k=`${String(r.player1||'').toLowerCase()}|${String(r.player2||'').toLowerCase()}`;if(!k||k==='|')continue;const old=best.get(k);const q=x=>Number(x._score_confidence||0)+(x.status?.04:0)+(x.line!==null?.04:0)+(x.under_odds!==null?.02:0)+(x.over_odds!==null?.02:0);if(!old||q(r)>q(old))best.set(k,r);}return Array.from(best.values());
  };

  const parseBetcris=(rows)=>{
    const leagueRe=/(e-?sports?|e-?soccer|esoccer|efootball|h2h\s*gg|gg\s*league|esports?\s*battle|volta|gt\s*league|interactive\s*world\s*cup|cyber|fifa)/i;
    const participantNoise=/^(live|in[- ]?play|today|tomorrow|draw|tie|over|under|total|totals|handicap|spread|moneyline|1x2|yes|no|more|less|all|sports?|soccer|football|e-?sports?)$/i;
    const marketNoise=/(over|under|total|handicap|spread|draw|tie|odds|moneyline|both teams|next goal|corners|cards|más|mas|menos|więcej|wiecej|mniej)/i;
    const scoreInt=/^\d{1,2}$/;
    const splitName=text=>{let s=normInline(text).replace(/^[•·\-–—]+\s*/,'').replace(/\s+[•·\-–—]+$/,'');const m=/^(.*?)\s*\(([^()]{1,60})\)\s*$/.exec(s);if(m)return{team:normInline(m[1]),player:normInline(m[2])};return{team:s,player:s};};
    const candidateRow=(row,rowIndex)=>{
      const ints=row.tokens.filter(t=>scoreInt.test(t.text)).map(t=>({...t,value:Number(t.text)})).filter(t=>Number.isFinite(t.value)&&t.value<=40);
      if(!ints.length)return null;
      let best=null;
      for(const sc of ints){const left=row.tokens.filter(t=>t.right<=sc.x-1.5);if(!left.length)continue;let label=normInline(left.map(t=>t.text).join(' '));label=label.replace(/^(live|in[- ]?play)\s+/i,'').trim();if(label.length<2||label.length>100||!/[A-Za-zÀ-ž]/.test(label))continue;if(participantNoise.test(label)||marketNoise.test(label))continue;if(/^\d{1,2}:\d{2}$/.test(label))continue;const name=splitName(label);if(!name.team||participantNoise.test(name.team))continue;const cand={rowIndex,row,team:name.team,player:name.player,x:left[0].x,y:row.cy,labelRight:Math.max(...left.map(t=>t.right)),score:sc.value,scoreX:sc.x,scoreCandidateCount:ints.length,rowText:row.text};if(!best||label.length>best.labelLength)best={...cand,labelLength:label.length};}
      return best;
    };
    const tr=[];for(let i=0;i<rows.length;i++){const c=candidateRow(rows[i],i);if(c)tr.push(c);}
    const parseClock=txt=>{const out={status:'',half:null,minute:null,clock_second_in_period:null};const s=normInline(txt);if(!s)return out;if(/(?:zakończ|koniec|finished|full\s*time|^ft$)/i.test(s)){out.status=s;return out;}let hm=/\b([12])\s*(?:h|half|poł|period)\b/i.exec(s);if(!hm)hm=/\b(1st|first|2nd|second)\s*(?:half|period)?\b/i.exec(s);if(hm){const v=String(hm[1]).toLowerCase();out.half=(v==='1'||v.startsWith('1')||v==='first')?1:2;}const cm=/\b(\d{1,2}):(\d{2})\b/.exec(s);const mm=/\b(\d{1,2})\s*(?:m|min|')\b/i.exec(s);if(cm){out.minute=Number(cm[1]);out.clock_second_in_period=Number(cm[1])*60+Number(cm[2]);}else if(mm){out.minute=Number(mm[1]);out.clock_second_in_period=Number(mm[1])*60;}if(out.half!==null||out.minute!==null||/live|in[- ]?play/i.test(s))out.status=s;return out;};
    const parseMarkets=(startIndex,maxY)=>{let line=null,under=null,over=null;for(let ri=startIndex;ri<rows.length&&ri<startIndex+22;ri++){const row=rows[ri];if(row.cy>maxY)break;const txt=normInline(row.text);if(!txt)continue;if(leagueRe.test(txt)&&ri>startIndex+1)break;const low=txt.toLowerCase();let side=null;if(/\b(over|o|más|mas|więcej|wiecej)\b/i.test(low))side='over';if(/\b(under|u|menos|mniej)\b/i.test(low))side='under';if(!side)continue;const nums=(txt.match(/\d+(?:[.,]\d+)?/g)||[]).map(num).filter(x=>x!==null);if(!nums.length)continue;let ln=nums[0],odd=nums.length>1?nums[1]:null;if(odd===null&&ri+1<rows.length){const next=(rows[ri+1].text.match(/\d+(?:[.,]\d+)?/g)||[]).map(num).filter(x=>x!==null);if(next.length&&next[0]>=1.01&&next[0]<=30)odd=next[0];}if(ln!==null&&ln>=0&&ln<=40)line=ln;if(side==='over'&&odd!==null)over=odd;if(side==='under'&&odd!==null)under=odd;}return{line,under_odds:under,over_odds:over};};
    const records=[];
    for(let i=0;i+1<tr.length;i++){
      const a=tr[i],b=tr[i+1],dy=b.y-a.y;if(dy<12||dy>95||Math.abs(a.x-b.x)>120)continue;
      const aligned=a.scoreX!==null&&b.scoreX!==null&&Math.abs(a.scoreX-b.scoreX)<=80;if(!aligned)continue;
      let league='',leagueIndex=-1;for(let ri=a.rowIndex-1;ri>=Math.max(0,a.rowIndex-30);ri--){const txt=rows[ri].text||'',dist=a.y-rows[ri].cy;if(dist>430)break;if(leagueRe.test(txt)){league=txt;leagueIndex=ri;break;}}
      if(!league)continue;
      let status='',half=null,minute=null,clock_second_in_period=null;for(let ri=a.rowIndex-1;ri>=Math.max(leagueIndex, a.rowIndex-12);ri--){const c=parseClock(rows[ri].text);if(c.status){status=c.status;half=c.half;minute=c.minute;clock_second_in_period=c.clock_second_in_period;break;}}
      const market=parseMarkets(b.rowIndex+1,b.y+300);
      let confidence=.78;if(Math.abs(a.scoreX-b.scoreX)<=45)confidence+=.08;if(a.scoreCandidateCount===1&&b.scoreCandidateCount===1)confidence+=.06;if(dy>=20&&dy<=70)confidence+=.04;confidence=Math.min(.98,confidence);
      const fmtm=/\b(2\s*x\s*\d+\s*(?:min|m)?)\b/i.exec(league);const format=fmtm?normInline(fmtm[1]).replace(/\s+/g,''):'';
      records.push({league:`BETCRIS · ${league}`,format,status,half,minute,clock_second_in_period,team1:a.team,player1:a.player,team2:b.team,player2:b.player,score1:a.score,score2:b.score,total_goals:a.score+b.score,line:market.line,under_odds:market.under_odds,over_odds:market.over_odds,_score_source:'layout_geometry',_score_confidence:confidence,_layout_adapter:'betcris',_layout_debug:JSON.stringify({row1:a.rowText,row2:b.rowText,scoreX1:a.scoreX,scoreX2:b.scoreX,league,market})});
    }
    const best=new Map();for(const r of records){const k=`${(r.player1||'').toLowerCase()}|${(r.player2||'').toLowerCase()}`;const old=best.get(k);const q=x=>Number(x._score_confidence||0)+(x.line!==null?.04:0)+(x.under_odds!==null?.02:0)+(x.over_odds!==null?.02:0);if(!old||q(r)>q(old))best.set(k,r);}return Array.from(best.values());
  };


  // Osobny adapter BETCRIS.PL dla domeny betsport.pl.
  // Obsługuje również eBasketball, gdzie wynik i linie totals mogą być znacznie > 40.
  const parseBetcrisPlStrict=(rows)=>{
    const leagueRe=/(e-?football|e-?soccer|esoccer|efootball|h2h\s*gg|gg\s*league|esports?\s*battle|volta|gt\s*league|interactive\s+world\s+cup|cyber\s+live\s+arena|esports?)/i;
    const participantNoise=/^(live|today|tomorrow|draw|tie|over|under|total|totals|handicap|spread|moneyline|1x2|yes|no|more|less|all|sport|sports|basketball|football|soccer|przerwa|break|zakończony|zakończone|finished|full\s*time|ft|informacje|info|edytuj\s+zak(?:ł|l)ad|dla\s+medi[oó]w|produkty|promocje|oferta|zakłady)$/i;
    const marketNoise=/(over|under|total|handicap|spread|draw|tie|odds|moneyline|więcej|wiecej|mniej|powyżej|poniżej)/i;
    const scoreInt=/^\d{1,3}$/;
    const splitName=text=>{let z=normInline(text).replace(/^[•·\-–—]+\s*/,'').replace(/\s+[•·\-–—]+$/,'').replace(/\s+(?:Informacje|Info|Edytuj\s+zak(?:ł|l)ad|Dla\s+medi[oó]w)$/i,'').trim();const m=/^(.*?)\s*\(([^()]{1,60})\)(?:\s+.*)?$/.exec(z);if(m)return{team:normInline(m[1]),player:normInline(m[2])};return{team:z,player:z};};
    const candidateRow=(row,rowIndex)=>{
      const ints=row.tokens.filter(t=>scoreInt.test(t.text)).map(t=>({...t,value:Number(t.text)})).filter(t=>Number.isFinite(t.value)&&t.value<=250);
      if(!ints.length)return null;let best=null;
      for(const sc of ints){const left=row.tokens.filter(t=>t.right<=sc.x-1.5);if(!left.length)continue;let label=normInline(left.map(t=>t.text).join(' ')).replace(/^(live|in[- ]?play)\s+/i,'').trim();if(label.length<2||label.length>120||!/[A-Za-zÀ-ž]/.test(label)||participantNoise.test(label)||marketNoise.test(label)||/^\d{1,2}:\d{2}$/.test(label))continue;const name=splitName(label);if(!name.team)continue;const cand={rowIndex,row,team:name.team,player:name.player,x:left[0].x,y:row.cy,labelRight:Math.max(...left.map(t=>t.right)),score:sc.value,scoreX:sc.x,scoreCandidateCount:ints.length,rowText:row.text,labelLength:label.length};if(!best||cand.labelLength>best.labelLength)best=cand;}
      return best;
    };
    const tr=[];for(let i=0;i<rows.length;i++){const c=candidateRow(rows[i],i);if(c)tr.push(c);}
    const parseClock=txt=>{const out={status:'',half:null,minute:null,clock_second_in_period:null};const z=normInline(txt);if(!z)return out;if(/(?:zakończ|koniec|finished|full\s*time|^ft$)/i.test(z)){out.status=z;return out;}const q=/\bQ\s*([1-4])\b|\b([1-4])(?:st|nd|rd|th)?\s*(?:quarter|kwarta)\b/i.exec(z);if(q){out.status=z;return out;}let hm=/\b([12])\s*\.?\s*(?:h|half|poł(?:owa|\.)?|period)\b/i.exec(z);if(hm)out.half=Number(hm[1]);const cm=/\b(\d{1,2}):(\d{2})\b/.exec(z),mm=/\b(\d{1,3})\s*(?:m|min|['’′])(?=$|\s|[^\w])/i.exec(z);if(cm){out.minute=Number(cm[1]);out.clock_second_in_period=Number(cm[1])*60+Number(cm[2]);}else if(mm){out.minute=Number(mm[1]);out.clock_second_in_period=Number(mm[1])*60;}if(out.half!==null||out.minute!==null||/live|in[- ]?play/i.test(z))out.status=z;return out;};
    const parseMarkets=(startIndex,maxY)=>{
      const books=new Map();let pendingOver=null,pendingUnder=null;
      const sideOf=txt=>/\b(over|o|więcej|wiecej|powyżej)\b/i.test(txt)?'over':(/\b(under|u|mniej|poniżej)\b/i.test(txt)?'under':null);
      const setSide=(side,line,odd)=>{if(line===null)return;const k=Number(line).toFixed(3),b=books.get(k)||{line:Number(line),over_odds:null,under_odds:null};if(side==='over')b.over_odds=odd;if(side==='under')b.under_odds=odd;books.set(k,b);};
      for(let ri=startIndex;ri<rows.length&&ri<startIndex+36;ri++){
        const row=rows[ri];if(row.cy>maxY)break;const txt=normInline(row.text||'');if(!txt)continue;
        const side=sideOf(txt);if(!side)continue;
        let combined=txt;
        for(let j=1;j<=2&&ri+j<rows.length;j++){const nr=rows[ri+j];if(nr.cy>maxY)break;const nt=normInline(nr.text||'');if(!nt||sideOf(nt))break;combined+=' '+nt;}
        const nums=(combined.match(/\d+(?:[.,]\d+)?/g)||[]).map(num).filter(x=>x!==null);
        let line=null,odd=null;
        if(nums.length>=2){
          const lineIndex=nums.findIndex(x=>x>=0.5&&x<=20.5);
          if(lineIndex>=0){
            line=nums[lineIndex];
            const odds=nums.slice(lineIndex+1).filter(x=>x>=1.001&&x<=20);
            if(odds.length)odd=odds[0];
          }
        }else if(nums.length===1){const x=nums[0];if(x>=1.001&&x<=20)odd=x;}
        if(line!==null&&odd!==null)setSide(side,line,odd);
        else if(odd!==null){if(side==='over')pendingOver=odd;else pendingUnder=odd;}
      }
      let vals=Array.from(books.values());
      vals.sort((a,b)=>((b.over_odds!==null)+(b.under_odds!==null))-((a.over_odds!==null)+(a.under_odds!==null)));
      let best=vals[0]||{line:null,over_odds:null,under_odds:null};
      if(best.line!==null){if(best.over_odds===null&&pendingOver!==null)best.over_odds=pendingOver;if(best.under_odds===null&&pendingUnder!==null)best.under_odds=pendingUnder;}
      return best;
    };
    const extractFormat=txt=>{
      const z=normInline(txt||'');
      let m=/\b([234]\s*[x×]\s*\d{1,2})\b/i.exec(z);if(m)return m[1].replace(/×/g,'x').replace(/\s+/g,'');
      m=/\b2\s*(?:połowy|polowy|halves?)\D{0,16}(\d{1,2})\s*min/i.exec(z);if(m)return`2x${m[1]}`;
      if(/\bh2h\b|gg\s*league/i.test(z))return'H2H 8m';
      if(/\bgt\s*league\b|\besoccer\s*gt\b/i.test(z))return'GT 12m';
      if(/\bvolta\b/i.test(z))return'Volta 6m';
      return'';
    };
    const records=[];
    for(let i=0;i+1<tr.length;i++){
      const a=tr[i],b=tr[i+1],dy=b.y-a.y;if(dy<10||dy>115||Math.abs(a.x-b.x)>140)continue;
      const aligned=a.scoreX!==null&&b.scoreX!==null&&Math.abs(a.scoreX-b.scoreX)<=90;if(!aligned)continue;
      let league='BETCRIS',leagueIndex=-1;
      for(let ri=a.rowIndex-1;ri>=Math.max(0,a.rowIndex-35);ri--){const tx=rows[ri].text||'';if(leagueRe.test(tx)){league=tx;leagueIndex=ri;break;}}
      let clk={status:'',half:null,minute:null};
      for(let ri=a.rowIndex-1;ri>=Math.max(0,a.rowIndex-14);ri--){const q=parseClock(rows[ri].text||'');if(q.status){clk=q;break;}}
      const context=[];for(let ri=Math.max(0,a.rowIndex-28);ri<Math.min(rows.length,b.rowIndex+14);ri++){if(Math.abs(rows[ri].cy-a.y)<=520)context.push(rows[ri].text||'');}
      const fmt=extractFormat(`${league} ${context.join(' ')}`);
      const market=parseMarkets(b.rowIndex+1,b.y+460);
      let confidence=.72;if(Math.abs(a.scoreX-b.scoreX)<=35)confidence+=.15;if(a.scoreCandidateCount===1&&b.scoreCandidateCount===1)confidence+=.08;confidence=Math.min(.99,confidence);
      records.push({league:leagueRe.test(league)?normInline(league).slice(0,120):'BETCRIS E-Football',format:fmt,status:clk.status,half:clk.half,minute:clk.minute,clock_second_in_period:null,_clock_mode:clk.minute!==null?'football_90':'',team1:a.team,player1:a.player,team2:b.team,player2:b.player,score1:a.score,score2:b.score,total_goals:a.score+b.score,line:market.line,under_odds:market.under_odds,over_odds:market.over_odds,_score_source:'layout_geometry',_score_confidence:confidence,_layout_adapter:'betcris_pl_strict_v5',_layout_debug:JSON.stringify({row1:a.rowText,row2:b.rowText,scoreX1:a.scoreX,scoreX2:b.scoreX,league,format:fmt,market})});
    }
    const best=new Map();for(const r of records){const k=`${String(r.player1||'').toLowerCase()}|${String(r.player2||'').toLowerCase()}`;if(!k||k==='|')continue;const old=best.get(k);if(!old||Number(r._score_confidence||0)>Number(old._score_confidence||0))best.set(k,r);}return Array.from(best.values());
  };


  // Ostatnia deska ratunku dla pojedynczej strony meczu. Działa tylko przy
  // jednoznacznym wzorcu nazwa+wynik, żeby nie tworzyć fałszywych meczów z kursów.
  const parseBetcrisPlText=(fullText)=>{
    const targetUrl=String(window.__PRZEWIJAK_SOURCE_CONFIG__?.url||location.href||'');if(!/\/match\//i.test(targetUrl))return[];
    const noise=/(live|in[- ]?play|today|tomorrow|draw|tie|over|under|total|totals|handicap|spread|moneyline|1x2|sport|sports|basketball|football|soccer|ebasketball|world|quarter|kwarta|period|połowa|half|kurs|odds|zakłady|zaloguj|login|więcej|wiecej|mniej|powyżej|poniżej)/i;
    const lines=String(fullText||'').split(/\n+/).map(normInline).filter(Boolean).slice(0,900);
    const nameOk=s=>s.length>=2&&s.length<=85&&/[A-Za-zÀ-ž]/.test(s)&&!noise.test(s)&&!/^https?:/i.test(s)&&!/^\d/.test(s)&&!/\bQ\s*[1-4]\b/i.test(s)&&!/\b\d{1,2}:\d{2}\b/.test(s);
    const splitName=s=>{const m=/^(.*?)\s*\(([^()]{1,60})\)\s*$/.exec(s);return m?{team:normInline(m[1]),player:normInline(m[2])}:{team:s,player:s};};
    const c=[];
    for(let i=0;i<lines.length;i++){
      let m=/^(.*?)\s+(\d{1,3})$/.exec(lines[i]);
      if(m&&nameOk(m[1])&&Number(m[2])<=250){const n=splitName(normInline(m[1]));c.push({i,team:n.team,player:n.player,score:Number(m[2]),raw:lines[i]});continue;}
      if(nameOk(lines[i]))for(let j=i+1;j<=Math.min(lines.length-1,i+2);j++){if(/^\d{1,3}$/.test(lines[j])&&Number(lines[j])<=250){const n=splitName(lines[i]);c.push({i,team:n.team,player:n.player,score:Number(lines[j]),raw:lines[i]+' | '+lines[j]});break;}}
    }
    const pairs=[];
    for(let i=0;i+1<c.length;i++){const a=c[i],b=c[i+1];if(b.i-a.i>8||a.team===b.team)continue;let status='';for(let k=Math.max(0,a.i-8);k<a.i;k++){if(/\bQ\s*[1-4]\b|\b\d{1,2}:\d{2}\b|live|in[- ]?play/i.test(lines[k]))status=lines[k];}let line=null,under=null,over=null;for(let k=b.i+1;k<Math.min(lines.length,b.i+40);k++){const low=lines[k].toLowerCase();const ns=(lines[k].match(/\d+(?:[.,]\d+)?/g)||[]).map(num).filter(x=>x!==null);if(/\b(over|więcej|wiecej|powyżej)\b/i.test(low)&&ns.length){if(ns[0]<=400)line=ns[0];const o=ns.length>=3?ns[ns.length-1]:(ns.length===2&&Math.abs(ns[1]-ns[0])>1e-9?ns[1]:null);if(o!==null&&o>=1.001&&o<=100)over=o;}if(/\b(under|mniej|poniżej)\b/i.test(low)&&ns.length){if(ns[0]<=400)line=ns[0];const o=ns.length>=3?ns[ns.length-1]:(ns.length===2&&Math.abs(ns[1]-ns[0])>1e-9?ns[1]:null);if(o!==null&&o>=1.001&&o<=100)under=o;}}
      pairs.push({league:'BETCRIS · betsport.pl',format:'',status,half:null,minute:null,team1:a.team,player1:a.player,team2:b.team,player2:b.player,score1:a.score,score2:b.score,total_goals:a.score+b.score,line,under_odds:under,over_odds:over,_score_source:'text_match_fallback',_score_confidence:.84,_layout_adapter:'betcris_pl_text_v2',_layout_debug:JSON.stringify({a:a.raw,b:b.raw})});
    }
    return pairs.length===1?pairs:[];
  };

  const betcrisPlActivePairMemory=new Map();
  const betcrisNormName=s=>normInline(s||'').toLowerCase().normalize('NFKD').replace(/[\u0300-\u036f]/g,'').replace(/[^a-z0-9]+/g,' ').trim();
  const betcrisTitlePlayers=()=>{
    const title=String(document.title||'');
    const noise=/^(?:live|in play|e football|efootball|esoccer|cyberfootball|world|europe)$/i;
    return Array.from(title.matchAll(/\(([^()]{1,60})\)/g)).map(m=>normInline(m[1])).filter(x=>x&&!noise.test(x)).slice(0,2);
  };
  const betcrisSamePair=(r,p)=>{
    if(!r||!p||p.length<2)return false;
    const a=betcrisNormName(r.player1),b=betcrisNormName(r.player2),x=betcrisNormName(p[0]),y=betcrisNormName(p[1]);
    return !!a&&!!b&&((a===x&&b===y)||(a===y&&b===x));
  };

  const parseBetcrisPl=(rows,fullText)=>{
    const targetUrl=String(window.__PRZEWIJAK_SOURCE_CONFIG__?.url||location.href||'');
    const oneMatch=/\/match\//i.test(targetUrl);
    const strict=parseBetcrisPlStrict(rows).filter(r=>{
      const bad=/(?:^|\b)(?:1\.?\s*poł(?:owa|\.)?|2\.?\s*poł(?:owa|\.)?|half|period|quarter|kwarta|nie\s*rozpoczęto|regulamin|cash\s*out|odpowiedzialna\s*gra|zakłady|bukmacher|powyżej|poniżej|over|under)(?:\b|$)/i;
      return r&&r.player1&&r.player2&&!bad.test(String(r.player1))&&!bad.test(String(r.player2));
    });
    if(oneMatch){
      const urlKey=String(location.origin||'')+String(location.pathname||'');
      const titlePlayers=betcrisTitlePlayers();
      const remembered=betcrisPlActivePairMemory.get(urlKey)||null;
      const q=r=>Number(r._score_confidence||0)+(['status','minute','line','under_odds','over_odds'].reduce((a,k)=>a+(r[k]!==null&&r[k]!==''&&r[k]!==undefined?0.03:0),0));
      let candidates=strict.slice();
      if(titlePlayers.length>=2){
        candidates=candidates.filter(r=>betcrisSamePair(r,titlePlayers));
      }else if(remembered){
        candidates=candidates.filter(r=>betcrisSamePair(r,remembered));
      }else if(candidates.length!==1){
        // Exact /match/ URL without an unambiguous title must HOLD instead of
        // selecting an adjacent card from the same DOM.
        return[];
      }
      if(candidates.length){
        candidates.sort((a,b)=>q(b)-q(a));
        const chosen={...candidates[0],_active_card_bound:true};
        betcrisPlActivePairMemory.set(urlKey,[chosen.player1,chosen.player2]);
        return[chosen];
      }
      const fallback=parseBetcrisPlText(fullText);
      const fallbackPair=titlePlayers.length>=2?titlePlayers:remembered;
      if(fallback.length===1&&(!fallbackPair||betcrisSamePair(fallback[0],fallbackPair))){
        const chosen={...fallback[0],_active_card_bound:true};
        betcrisPlActivePairMemory.set(urlKey,[chosen.player1,chosen.player2]);
        return[chosen];
      }
      return[];
    }
    // Lista LIVE: wolimy brak rekordu niż fałszywe pary z nagłówków/rynków.
    // Luźny parser pozostaje w kodzie diagnostycznie, ale nie zasila bazy.
    return strict;
  };

  const collect=()=>{
    const fullText=normBlock(document.body?.innerText||'');
    const {vw,vh,uniq,rows}=scanRows();
    const host=(location.hostname||'').toLowerCase();
    const cfg=(window.__PRZEWIJAK_SOURCE_CONFIG__&&typeof window.__PRZEWIJAK_SOURCE_CONFIG__==='object')?window.__PRZEWIJAK_SOURCE_CONFIG__:{};
    const forced=String(cfg.parser||window.__PRZEWIJAK_PLATFORM_OVERRIDE__||'auto').toLowerCase();
    const autoFamily=host==='betcris.com'||host.endsWith('.betcris.com')?'betcris':(host==='betsport.pl'||host.endsWith('.betsport.pl')?'betcris_pl':(host==='superbet.pl'||host.endsWith('.superbet.pl')?'superbet':'fortuna'));
    const parserFamily=forced==='auto'?autoFamily:forced;
    const isBetcris=parserFamily==='betcris',isBetcrisPl=parserFamily==='betcris_pl',isSuperbet=parserFamily==='superbet';
    const sourceId=String(cfg.id||parserFamily||'source').toLowerCase();
    const sourceLabel=String(cfg.label||sourceId).trim()||sourceId;
    const collectCfg=(cfg.collect&&typeof cfg.collect==='object')?cfg.collect:{score:true,clock:true,market:true};
    const layout_records=isBetcrisPl?parseBetcrisPl(rows,fullText):(isBetcris?parseBetcris(rows):(isSuperbet?parseSuperbet(rows):parseFortuna(rows)));
    return{platform:sourceId,source_id:sourceId,source_label:sourceLabel,parser_family:parserFamily,collect:collectCfg,adapter:isBetcrisPl?(layout_records[0]?._layout_adapter||'betcris_pl_strict_v5'):(isBetcris?'betcris_layout_v1':(isSuperbet?(layout_records[0]?._layout_adapter||'superbet_v4'):'fortuna_layout_v3')),full_text:fullText,layout_records,token_count:uniq.length,row_count:rows.length,viewport:{w:vw,h:vh},url:location.href,title:document.title||'',collector_ts:new Date().toISOString()};
  };
  window.__PRZEWIJAK_COLLECT_NOW__=collect;


  // Transport Edge: ACK + lokalny bufor. Bezpośredni most Playwright przekazuje dane
  // do lokalnego silnika; kolejka chroni dane przy chwilowym braku odpowiedzi.
  const randomId=()=>{try{return crypto.randomUUID()}catch(e){return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`}};
  const TAB_KEY='__przewijak_tab_id_clean';
  let tabId='';
  try{tabId=sessionStorage.getItem(TAB_KEY)||randomId();sessionStorage.setItem(TAB_KEY,tabId);}catch(e){tabId=randomId();}
  const collectorSession=randomId();
  const collectorId=`${location.hostname}:${tabId}`;
  const QKEY=`__przewijak_q_clean_${tabId}`;
  const STATEKEY=`__przewijak_state_clean_${tabId}`;
  const MAX_QUEUE=60,MAX_QUEUE_CHARS=1600000;
  let queueDropped=0,queueRecovered=0,fail=0,heartbeatFail=0,backoffMs=700,nextRetryAt=0,flushing=false,stopped=false,lastError='',lastServerSession='',lastStorageError='';
  let lastCaptureAt=0,lastAckAt=0,lastHeartbeatAt=0,serverRunning=null;
  // captureSeq counts DOM observations; txSeq counts only packets that are actually
  // sent. Queue compaction can drop unsent observations without creating fake
  // transport seq gaps in the backend.
  let captureSeq=0,txSeq=0;
  try{captureSeq=Number(sessionStorage.getItem('__przewijak_capture_seq_clean')||0)||0;}catch(e){}
  try{txSeq=Number(sessionStorage.getItem('__przewijak_seq_clean')||0)||0;}catch(e){}

  const storeGet=(k,d)=>{try{const x=localStorage.getItem(k);return x?JSON.parse(x):d;}catch(e){return d;}};
  const storeSet=(k,v)=>{try{localStorage.setItem(k,JSON.stringify(v));lastStorageError='';return true;}catch(e){lastStorageError=String(e?.message||e);return false;}};
  let queue=storeGet(QKEY,[]);if(!Array.isArray(queue))queue=[];
  const oldState=storeGet(STATEKEY,{});if(oldState&&typeof oldState==='object'){queueDropped=Number(oldState.queueDropped||0)||0;queueRecovered=Number(oldState.queueRecovered||0)||0;lastServerSession=String(oldState.lastServerSession||'');}
  const stateSig=p=>{try{const rows=(p?.layout_records||[]).map(r=>[r?.player1||'',r?.team1||'',r?.player2||'',r?.team2||'',r?.score1??'',r?.score2??'',r?.status||'',r?.half??'',r?.minute??'',r?.line??'',r?.under_odds??'',r?.over_odds??''].join('|')).sort();return `${p?.url||''}::${rows.join('~')}`;}catch(e){return String(p?.packet_id||'')}};
  const compactQueue=()=>{if(queue.length<2)return;const keepFirst=flushing?1:0;const out=keepFirst?[queue[0]]:[];let lastSig=keepFirst?stateSig(queue[0]):null;for(let i=keepFirst;i<queue.length;i++){const item=queue[i],sig=stateSig(item);if(out.length>keepFirst&&sig===lastSig){out[out.length-1]=item;queueDropped++;}else{out.push(item);lastSig=sig;}}queue=out;while(queue.length>MAX_QUEUE){const idx=(flushing&&queue.length>1)?1:0;queue.splice(idx,1);queueDropped++;}};
  const saveQueue=()=>{compactQueue();let raw='';try{raw=JSON.stringify(queue);}catch(e){raw='[]';}while(raw.length>MAX_QUEUE_CHARS&&queue.length>1){const idx=(flushing&&queue.length>1)?1:0;queue.splice(idx,1);queueDropped++;raw=JSON.stringify(queue);}while(queue.length>1&&!storeSet(QKEY,queue)){const idx=(flushing&&queue.length>1)?1:0;queue.splice(idx,1);queueDropped++;}storeSet(QKEY,queue);storeSet(STATEKEY,{queueDropped,queueRecovered,lastServerSession,lastStorageError});};

  const requestJson=(method,url,data,timeout=3200)=>new Promise((resolve,reject)=>{
    if(typeof window.__przewijakNativeSend!=='function'){reject(new Error('BRAK_NATIVE_BRIDGE'));return;}
    try{Promise.resolve(window.__przewijakNativeSend({method,url,data,timeout})).then(resolve,reject);}catch(e){reject(e);}
  });

  const transportError=e=>/(SERVER_NOT_FOUND|NETWORK|TIMEOUT|ABORT|BAD_JSON|HTTP_5\d\d|BRAK_NATIVE_BRIDGE)/i.test(String(e?.message||e||''));

  const cfgNow=()=>((window.__PRZEWIJAK_SOURCE_CONFIG__&&typeof window.__PRZEWIJAK_SOURCE_CONFIG__==='object')?window.__PRZEWIJAK_SOURCE_CONFIG__:{});
  const parserNow=()=>{const cfg=cfgNow(),h=(location.hostname||'').toLowerCase(),f=String(cfg.parser||window.__PRZEWIJAK_PLATFORM_OVERRIDE__||'auto').toLowerCase();if(f&&f!=='auto')return f;if(h==='betcris.com'||h.endsWith('.betcris.com'))return'betcris';if(h==='betsport.pl'||h.endsWith('.betsport.pl'))return'betcris_pl';if(h==='superbet.pl'||h.endsWith('.superbet.pl'))return'superbet';return'fortuna';};
  const platformNow=()=>String(cfgNow().id||parserNow()||'source').toLowerCase();
  const sourceLabel=s=>String(cfgNow().label||s||platformNow()).toUpperCase();
  const visibilityInfo=()=>({visibility:document.visibilityState,focused:document.hasFocus?.()===true});
  const heartbeatPayload=()=>{const cfg=cfgNow();return{kind:'heartbeat',platform:platformNow(),source_id:platformNow(),source_label:String(cfg.label||platformNow()),parser_family:parserNow(),collect:(cfg.collect&&typeof cfg.collect==='object')?cfg.collect:{score:true,clock:true,market:true},collector_id:collectorId,collector_session:collectorSession,tab_id:tabId,queue_depth:queue.length,queue_dropped:queueDropped,queue_recovered:queueRecovered,last_error:lastError,url:location.href,title:document.title||'',...visibilityInfo()};};

  const handleServerSession=(j)=>{const ss=String(j?.server_session||''),previous=lastServerSession;const restarted=!!(previous&&ss&&previous!==ss);if(ss)lastServerSession=ss;if(j?.running!==undefined)serverRunning=!!j.running;if(j?.server_running!==undefined)serverRunning=!!j.server_running;storeSet(STATEKEY,{queueDropped,queueRecovered,lastServerSession,lastStorageError});return restarted;};

  const shortErr=()=>String(lastError||'').replace(/^heartbeat:/,'HB:').replace(/\s+/g,' ').slice(0,72);
  const transportFresh=()=>{const now=Date.now();return serverRunning===true&&lastHeartbeatAt>0&&(now-lastHeartbeatAt)<10000&&lastAckAt>0&&(now-lastAckAt)<10000;};
  const updatePill=(payload=null)=>{const src=sourceLabel(payload?.platform||platformNow());const n=payload?.layout_records?.length??'?';const fresh=transportFresh();if(!fresh||fail||heartbeatFail){const e=shortErr()||((serverRunning===false)?'ENGINE/SERWER OFFLINE':(lastHeartbeatAt<=0?'czekam na heartbeat':(Date.now()-lastHeartbeatAt>=10000?'heartbeat nieświeży':(lastAckAt<=0?'czekam na ACK':'ACK nieświeży'))));pill.textContent=`${src} ↔ PC ⚠ D${fail}/H${heartbeatFail} · Q${queue.length}${e?' · '+e:''}`;pill.style.background='#92400e';return;}pill.textContent=`${src} → PC ✓ mecze ${n} · Q${queue.length}`;pill.style.background='#065f46';};

  const enqueue=payload=>{queue.push(payload);saveQueue();updatePill(payload);};
  saveQueue();

  const flushQueue=async()=>{
    if(stopped||flushing||!queue.length||serverRunning===false||Date.now()<nextRetryAt)return;
    flushing=true;
    try{
      const original=queue[0];
      // Assign transport sequence only when this item reaches the head of the queue.
      // Unsent items removed by compaction therefore never consume seq numbers.
      if(original.seq===undefined||original.seq===null||!original.packet_id){
        txSeq++;try{sessionStorage.setItem('__przewijak_seq_clean',String(txSeq));}catch(e){}
        original.seq=txSeq;original.packet_id=`${collectorSession}:${txSeq}`;saveQueue();
      }
      const p={...original,queue_depth:queue.length,queue_dropped:queueDropped,queue_recovered:queueRecovered,from_buffer:(Date.now()-Number(original.created_ms||Date.now()))>1400};
      const j=await requestJson('POST',ENDPOINT,p,3600);
      handleServerSession(j);
      if(j?.retired||j?.stop_collector){
        queue=[];saveQueue();lastError='';fail=0;serverRunning=false;window.__PRZEWIJAK_SOURCE_RETIRED__=true;
        try{stop();}catch(e){}
        return;
      }
      if(j?.running===false||j?.server_running===false)throw new Error('ENGINE_STOPPED');
      if(!j?.ok)throw new Error(j?.error||'ACK_FALSE');
      if(j.ack_packet_id&&j.ack_packet_id!==original.packet_id)throw new Error('ACK_MISMATCH');
      if(p.from_buffer)queueRecovered++;
      queue.shift();saveQueue();fail=0;lastError='';backoffMs=700;nextRetryAt=0;lastAckAt=Date.now();serverRunning=true;updatePill(original);
    }catch(e){fail++;lastError=String(e?.message||e);nextRetryAt=Date.now()+backoffMs;backoffMs=Math.min(12000,Math.round(backoffMs*1.75));if(fail>=3)serverRunning=false;updatePill(queue[0]);}
    finally{flushing=false;}
  };

  const makePacket=()=>{const payload=collect();captureSeq++;try{sessionStorage.setItem('__przewijak_capture_seq_clean',String(captureSeq));}catch(e){}return{...payload,kind:'data',collector_id:collectorId,collector_session:collectorSession,tab_id:tabId,capture_seq:captureSeq,created_ms:Date.now(),queue_depth:queue.length,queue_dropped:queueDropped,queue_recovered:queueRecovered,...visibilityInfo()};};

  let collectTimer=null,lastCollectAt=0;
  const capture=()=>{if(stopped||serverRunning===false)return;const now=Date.now();if(now-lastCollectAt<650)return;lastCollectAt=now;lastCaptureAt=now;try{enqueue(makePacket());flushQueue();}catch(e){lastError='collect:'+String(e?.message||e);updatePill();}};
  const scheduleCapture=()=>{if(stopped||collectTimer)return;const wait=Math.max(120,680-(Date.now()-lastCollectAt));collectTimer=setTimeout(()=>{collectTimer=null;capture();},wait);};

  const observer=new MutationObserver(records=>{for(const m of records){const t=m.target instanceof Element?m.target:m.target?.parentElement;if(t?.closest?.('[data-przewijak-overlay="1"]'))continue;scheduleCapture();break;}});
  try{observer.observe(document.documentElement,{subtree:true,childList:true,characterData:true,attributes:false});}catch(e){}
  const fallbackTimer=setInterval(capture,1200);
  const flushTimer=setInterval(flushQueue,250);

  const heartbeat=async()=>{if(stopped)return;try{const wasRunning=serverRunning;const j=await requestJson('POST',HEARTBEAT_ENDPOINT,heartbeatPayload(),4200);if(j?.retired||j?.stop_collector){queue=[];saveQueue();lastError='';fail=0;heartbeatFail=0;serverRunning=false;window.__PRZEWIJAK_SOURCE_RETIRED__=true;try{stop();}catch(e){}return;}const restarted=handleServerSession(j);lastHeartbeatAt=Date.now();heartbeatFail=0;if(j?.ok&&serverRunning!==false){if(restarted||wasRunning===false){nextRetryAt=0;backoffMs=700;if(transportError({message:lastError})){fail=0;lastError='';}capture();}else if(fail&&transportError({message:lastError})){fail=0;lastError='';nextRetryAt=0;backoffMs=700;}updatePill();if(queue.length)flushQueue();}}catch(e){heartbeatFail++;lastError='heartbeat:'+String(e?.message||e);serverRunning=false;updatePill();}};
  const heartbeatTimer=setInterval(heartbeat,4000);
  // UI health is time based as well: a dead backend must not leave a stale green ✓
  // between heartbeat attempts or after a Playwright/native bridge disappears.
  const healthTimer=setInterval(updatePill,1000);

  // Screen Wake Lock: działa, gdy karta przeglądarki jest aktywna. Program PC trzyma
  // Windows dodatkowo utrzymuje system aktywny przez SetThreadExecutionState.
  let wake=null,night=false;
  const releaseWake=()=>{try{if(wake&&!wake.released)wake.release().catch(()=>{});}catch(e){}wake=null;};
  const requestWake=async()=>{if(!night||document.visibilityState!=='visible'||!('wakeLock'in navigator))return false;if(wake&&!wake.released)return true;try{wake=await navigator.wakeLock.request('screen');wake.addEventListener('release',()=>{wake=null;});return true;}catch(e){return false;}};
  const syncNight=async()=>{try{const j=await requestJson('GET',STATUS_ENDPOINT,undefined,2500);night=!!j?.screen_awake;if(night)await requestWake();else releaseWake();}catch(e){}};
  const nightTimer=setInterval(syncNight,3000);
  document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible'){syncNight();scheduleCapture();heartbeat();}else releaseWake();});
  window.addEventListener('focus',()=>{syncNight();scheduleCapture();});window.addEventListener('pageshow',()=>{syncNight();scheduleCapture();});

  const stop=()=>{stopped=true;clearInterval(fallbackTimer);clearInterval(flushTimer);clearInterval(heartbeatTimer);clearInterval(healthTimer);clearInterval(nightTimer);if(collectTimer)clearTimeout(collectTimer);observer.disconnect();releaseWake();pill.remove();window.__PRZEWIJAK_COLLECTOR__=null;};
  window.__PRZEWIJAK_COLLECTOR__={version:COLLECTOR_VERSION,stop,poke:()=>{heartbeat();if(serverRunning!==false){capture();flushQueue();}},queue:()=>queue.length,stats:()=>({version:COLLECTOR_VERSION,queue:queue.length,queueDropped,queueRecovered,fail,heartbeatFail,lastError,lastStorageError,lastCaptureAt,lastAckAt,lastHeartbeatAt,serverRunning,transportFresh:transportFresh(),heartbeatAgeMs:lastHeartbeatAt?Date.now()-lastHeartbeatAt:null,ackAgeMs:lastAckAt?Date.now()-lastAckAt:null,collectorId,collectorSession,captureSeq,txSeq}),collectorId,collectorSession};
  heartbeat();syncNight();capture();flushQueue();
})();
