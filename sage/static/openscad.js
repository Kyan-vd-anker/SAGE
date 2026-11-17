(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const state = { designs: [], design: null, version: null, triangles: [], yaw: 0.75, pitch: -0.5, zoom: 1, drag: null };
  const canvas = $('openscadCanvas');
  if (!canvas) return;

  $('chatFiles').accept += ',.png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp';
  const drawingPanel=document.createElement('article');
  drawingPanel.className='card technicalDrawingCard';
  drawingPanel.innerHTML='<div class="openscadToolbar"><strong>2D technical drawing</strong><button id="openscadMakeDrawing" class="softBtn" type="button" disabled>Create top/front/right views</button><a id="openscadDrawingDownload" class="softBtn" download hidden>Download SVG</a></div><p id="openscadDrawingStatus" class="meta">Create a dimensioned orthographic drawing from the selected 3D version.</p><img id="openscadDrawingPreview" alt="Dimensioned orthographic technical drawing" hidden>';
  canvas.closest('.openscadPreviewCard').after(drawingPanel);
  let drawingUrl='';

  function clearDrawing() {
    if(drawingUrl)URL.revokeObjectURL(drawingUrl);
    drawingUrl='';
    $('openscadDrawingPreview').removeAttribute('src');
    $('openscadDrawingPreview').hidden=true;
    $('openscadDrawingDownload').hidden=true;
    $('openscadDrawingStatus').textContent='Create a dimensioned orthographic drawing from the selected 3D version.';
    $('openscadDrawingStatus').classList.remove('openscadStatusError');
  }

  async function call(url, body) {
    const response = await fetch(url, body ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {cache:'no-store'});
    let data = {};
    try { data = await response.json(); } catch { /* empty response */ }
    if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
    return data;
  }

  function setStatus(message, error=false) {
    const status = $('openscadStatus');
    status.textContent = message || '';
    status.classList.toggle('openscadStatusError', error);
  }

  function setPreviewStatus(message, error=false) {
    $('openscadPreviewStatus').textContent = message || '';
    $('openscadPreviewStatus').classList.toggle('openscadStatusError', error);
  }

  function draw() {
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const ratio = window.devicePixelRatio || 1;
    const width = Math.round(rect.width * ratio), height = Math.round(rect.height * ratio);
    if (canvas.width !== width || canvas.height !== height) { canvas.width = width; canvas.height = height; }
    const context = canvas.getContext('2d');
    context.clearRect(0, 0, width, height);
    if (!state.triangles.length) return;
    const cy = Math.cos(state.yaw), sy = Math.sin(state.yaw), cp = Math.cos(state.pitch), sp = Math.sin(state.pitch);
    const points = [], flat = state.triangles;
    for (let i=0; i<flat.length; i+=3) {
      const x=flat[i], y=flat[i+1], z=flat[i+2];
      const rx=x*cy-z*sy, rz=x*sy+z*cy;
      points.push([rx, y*cp-rz*sp, y*sp+rz*cp]);
    }
    let min=Infinity,max=-Infinity,extent=1;
    for(const point of points){min=Math.min(min,point[2]);max=Math.max(max,point[2]);extent=Math.max(extent,Math.abs(point[0])*2,Math.abs(point[1])*2);}
    const span=Math.max(0.001,max-min);
    const scale=Math.min(width,height)*0.78/Math.max(extent,span)*state.zoom;
    const project=p=>[width/2+p[0]*scale,height/2-p[1]*scale,p[2]];
    const faces=[];
    for(let i=0;i<points.length;i+=3){
      const a=points[i],b=points[i+1],c=points[i+2];
      const ux=b[0]-a[0],uy=b[1]-a[1],uz=b[2]-a[2],vx=c[0]-a[0],vy=c[1]-a[1],vz=c[2]-a[2];
      const nx=uy*vz-uz*vy,ny=uz*vx-ux*vz,nz=ux*vy-uy*vx;
      const length=Math.hypot(nx,ny,nz)||1,light=Math.max(0,(nx*.35+ny*.55+nz*.76)/length);
      faces.push({points:[project(a),project(b),project(c)],z:(a[2]+b[2]+c[2])/3,shade:Math.round(75+145*light)});
    }
    faces.sort((a,b)=>a.z-b.z);
    context.lineWidth=ratio*.6;
    for(const face of faces){
      context.beginPath();context.moveTo(face.points[0][0],face.points[0][1]);
      context.lineTo(face.points[1][0],face.points[1][1]);context.lineTo(face.points[2][0],face.points[2][1]);context.closePath();
      context.fillStyle=`rgb(${Math.round(face.shade*.65)},${face.shade},${Math.min(255,face.shade+38)})`;
      context.fill();context.strokeStyle='rgba(4,10,16,.48)';context.stroke();
    }
  }

  function loadStl(buffer) {
    const view=new DataView(buffer);
    if(buffer.byteLength>=84){
      const count=view.getUint32(80,true);
      if(count>0&&(84+count*50)===buffer.byteLength){
        const skip=Math.max(1,Math.ceil(count/40000)),bounds=[Infinity,Infinity,Infinity,-Infinity,-Infinity,-Infinity],vertices=[];
        for(let face=0;face<count;face++){
          const offset=84+face*50+12,triangle=[];
          for(let vertex=0;vertex<3;vertex++)for(let axis=0;axis<3;axis++){
            const value=view.getFloat32(offset+vertex*12+axis*4,true);
            bounds[axis]=Math.min(bounds[axis],value);bounds[axis+3]=Math.max(bounds[axis+3],value);triangle.push(value);
          }
          if(face%skip===0)vertices.push(...triangle);
        }
        state.triangles=normalize(vertices,bounds);draw();return;
      }
    }
    const text=new TextDecoder().decode(buffer),matches=text.matchAll(/\bvertex\s+([-+\d.eE]+)\s+([-+\d.eE]+)\s+([-+\d.eE]+)/gi),all=[];
    for(const match of matches){all.push(Number(match[1]),Number(match[2]),Number(match[3]));if(all.length>3_000_000)throw Error('STL preview contains too many vertices.')}
    const bounds=[Infinity,Infinity,Infinity,-Infinity,-Infinity,-Infinity];
    for(let i=0;i<all.length;i+=3){for(let axis=0;axis<3;axis++){bounds[axis]=Math.min(bounds[axis],all[i+axis]);bounds[axis+3]=Math.max(bounds[axis+3],all[i+axis]);}}
    if(!all.length)throw Error('OpenSCAD returned an unreadable STL preview.');
    const skip=Math.max(1,Math.ceil(all.length/3/40000)),vertices=[];
    for(let face=0;face<all.length/9;face+=skip)vertices.push(...all.slice(face*9,face*9+9));
    state.triangles=normalize(vertices,bounds);draw();
  }

  function normalize(vertices,bounds) {
    const center=bounds.map((value,index)=>index<3?(value+bounds[index+3])/2:0).slice(0,3);
    const scale=Math.max(bounds[3]-bounds[0],bounds[4]-bounds[1],bounds[5]-bounds[2])||1;
    for(let i=0;i<vertices.length;i+=3){vertices[i]=(vertices[i]-center[0])/scale;vertices[i+1]=(vertices[i+1]-center[1])/scale;vertices[i+2]=(vertices[i+2]-center[2])/scale;}
    return vertices;
  }

  async function showVersion(id) {
    const version=await call(`/api/openscad/versions/${id}`);
    state.version=version;
    $('openscadVersion').value=String(id);
    $('openscadSource').value=version.source;
    $('openscadSource').disabled=false;
    $('openscadDownload').disabled=false;
    $('openscadSaveSource').disabled=false;
    $('openscadPreviewTitle').textContent=`${state.design.title} · v${version.version}`;
    $('openscadPreviewMeta').textContent=version.created_at;
    $('openscadMakeDrawing').disabled=false;
    clearDrawing();
    state.triangles=[];draw();
    if(version.render_error){setPreviewStatus(version.render_error,true);return;}
    if(!version.mesh_url){setPreviewStatus('No rendered mesh is available for this version.',true);return;}
    setPreviewStatus('Loading STL preview…');
    const response=await fetch(version.mesh_url,{cache:'no-store'});
    if(!response.ok)throw Error('Could not load this version’s STL preview.');
    loadStl(await response.arrayBuffer());
    setPreviewStatus('Drag to rotate; scroll to zoom. This draft has not been checked for fit or printability.');
  }

  async function openDesign(id) {
    const design=await call(`/api/openscad/designs/${id}`);
    state.design=design;
    $('openscadTitle').value=design.title;
    $('openscadProject').value=design.project_id||'';
    $('openscadRevise').disabled=false;
    $('openscadSaveSource').disabled=!design.versions.length;
    $('openscadVersion').innerHTML=design.versions.map(version=>`<option value="${version.id}">v${version.version} · ${esc(version.created_at)}</option>`).join('');
    renderDesignList();
    if(design.versions.length)await showVersion(design.versions[0].id);
    else {state.version=null;$('openscadSource').value='';}
  }

  function renderDesignList() {
    $('openscadDesignList').innerHTML=state.designs.length?state.designs.map(item=>`<button type="button" class="openscadDesign ${state.design?.id===item.id?'selected':''}" data-id="${item.id}"><strong>${esc(item.title)}</strong><small>v${item.version||0} · ${esc(item.updated_at)}</small></button>`).join(''):'<p class="meta">No saved models yet. Generate one above or open an OpenSCAD code block from Chat.</p>';
  }

  async function loadOpenScad(selectedId=null) {
    try {
      const [designs,projects]=await Promise.all([call('/api/openscad/designs'),call('/api/projects')]);
      state.designs=designs;
      $('openscadProject').innerHTML='<option value="">No linked project</option>'+projects.map(project=>`<option value="${project.id}">${esc(project.name)}</option>`).join('');
      renderDesignList();
      const target=selectedId||state.design?.id||designs[0]?.id;
      if(target)await openDesign(target);
      else {state.design=null;state.version=null;$('openscadSource').value='';$('openscadSource').disabled=true;$('openscadRevise').disabled=true;$('openscadSaveSource').disabled=true;$('openscadMakeDrawing').disabled=true;clearDrawing();setPreviewStatus('Create or open a model to render its preview. Drag to rotate; scroll to zoom.');}
    } catch(error) { setStatus(error.message,true); }
  }

  async function selectedImages() {
    const files=[...$('openscadImages').files];
    if(files.length>5)throw Error('Select no more than 5 schematic images.');
    let total=0;
    return Promise.all(files.map(async file=>{
      if(file.size>8_000_000)throw Error(`${file.name} is larger than 8 MB.`);
      total+=file.size;if(total>18_000_000)throw Error('Schematic images must total 18 MB or less.');
      const bytes=new Uint8Array(await file.arrayBuffer());let binary='';
      for(let i=0;i<bytes.length;i+=8192)binary+=String.fromCharCode(...bytes.subarray(i,i+8192));
      return {name:file.name,data_b64:btoa(binary)};
    }));
  }

  function setBusy(busy) {
    $('openscadGenerate').disabled=busy;
    $('openscadRevise').disabled=busy||!state.design;
    $('openscadSaveSource').disabled=busy||!state.design;
  }

  async function createOrRevise(revise) {
    const prompt=$('openscadPrompt').value.trim();
    if(!prompt){setStatus('Describe the design or the change you want.',true);return;}
    setBusy(true);setStatus(revise?'Sage is revising the source…':'Sage is creating the source…');
    try {
      const images=await selectedImages();
      const body={prompt,images};
      let result;
      if(revise){
        body.version_id=state.version?.id;
        result=await call(`/api/openscad/designs/${state.design.id}/revise`,body);
      }else{
        body.title=$('openscadTitle').value.trim();
        body.project_id=$('openscadProject').value||null;
        result=await call('/api/openscad/designs',body);
      }
      $('openscadPrompt').value='';$('openscadImages').value='';$('openscadFiles').textContent='Optional schematic images · up to 5 files, 8 MB each';
      setStatus(`Saved version ${result.version.version}.`);
      await loadOpenScad(result.design.id);
      if(result.version.render_error)setStatus(`Version saved; preview unavailable: ${result.version.render_error}`,true);
    } catch(error) { setStatus(error.message,true); }
    finally { setBusy(false); }
  }

  $('openscadForm').onsubmit=event=>{event.preventDefault();createOrRevise(false);};
  $('openscadRevise').onclick=()=>createOrRevise(true);
  $('openscadNew').onclick=()=>{
    state.design=null;state.version=null;$('openscadTitle').value='';$('openscadPrompt').value='';
    $('openscadVersion').innerHTML='';$('openscadSource').value='';$('openscadSource').disabled=true;
    $('openscadRevise').disabled=true;$('openscadSaveSource').disabled=true;$('openscadDownload').disabled=true;$('openscadMakeDrawing').disabled=true;clearDrawing();
    state.triangles=[];draw();renderDesignList();setPreviewStatus('Describe a new model or open a saved design.');
    setStatus('');
  };
  $('openscadDesignList').onclick=event=>{
    const button=event.target.closest('.openscadDesign');
    if(button)openDesign(button.dataset.id).catch(error=>setStatus(error.message,true));
  };
  $('openscadVersion').onchange=()=>showVersion($('openscadVersion').value).catch(error=>setPreviewStatus(error.message,true));
  $('openscadMakeDrawing').onclick=async()=>{
    if(!state.version)return;
    const status=$('openscadDrawingStatus'),image=$('openscadDrawingPreview'),download=$('openscadDrawingDownload'),button=$('openscadMakeDrawing');
    button.disabled=true;status.textContent='Creating dimensioned orthographic views…';status.classList.remove('openscadStatusError');
    try{
      const drawing=await call(`/api/openscad/versions/${state.version.id}/drawing`,{});
      if(drawing.render_error)throw Error(drawing.render_error);
      if(drawingUrl)URL.revokeObjectURL(drawingUrl);
      drawingUrl=URL.createObjectURL(new Blob([drawing.svg],{type:'image/svg+xml'}));
      image.src=drawingUrl;image.hidden=false;download.href=drawingUrl;download.download=`${state.design.title.replace(/[^\w.-]+/g,'_')}_v${state.version.version}_drawing.svg`;download.hidden=false;
      const size=drawing.dimensions_mm;
      status.textContent=`Top, front, and right projections · ${size.x.toFixed(2)} × ${size.y.toFixed(2)} × ${size.z.toFixed(2)} mm. Verify all dimensions before manufacture.`;
    }catch(error){status.textContent=error.message;status.classList.add('openscadStatusError');}
    finally{button.disabled=false;}
  };
  $('openscadSaveSource').onclick=async()=>{
    setBusy(true);setStatus('Saving a new source version and rendering…');
    try{const result=await call(`/api/openscad/designs/${state.design.id}/versions`,{source:$('openscadSource').value,notes:$('openscadNotes').value});$('openscadNotes').value='';setStatus(`Saved version ${result.version.version}.`);await loadOpenScad(result.design.id);if(result.version.render_error)setStatus(`Version saved; preview unavailable: ${result.version.render_error}`,true);}
    catch(error){setStatus(error.message,true);}finally{setBusy(false);}
  };
  $('openscadDownload').onclick=()=>{
    if(!state.version)return;
    const url=URL.createObjectURL(new Blob([$('openscadSource').value],{type:'text/plain;charset=utf-8'})),link=document.createElement('a');
    link.href=url;link.download=`${state.design.title.replace(/[^\w.-]+/g,'_')}_v${state.version.version}.scad`;link.click();URL.revokeObjectURL(url);
  };
  $('openscadFit').onclick=()=>{state.yaw=.75;state.pitch=-.5;state.zoom=1;draw();};
  canvas.onpointerdown=event=>{state.drag=[event.clientX,event.clientY];canvas.setPointerCapture(event.pointerId);};
  canvas.onpointermove=event=>{if(!state.drag)return;state.yaw+=(event.clientX-state.drag[0])*.009;state.pitch=Math.max(-1.5,Math.min(1.5,state.pitch+(event.clientY-state.drag[1])*.009));state.drag=[event.clientX,event.clientY];draw();};
  canvas.onpointerup=canvas.onpointercancel=()=>{state.drag=null;};
  canvas.onwheel=event=>{event.preventDefault();state.zoom=Math.max(.35,Math.min(4,state.zoom*(event.deltaY<0?1.1:.9)));draw();};
  $('openscadImages').onchange=()=>{$('openscadFiles').textContent=[...$('openscadImages').files].map(file=>`${file.name} · ${(file.size/1024).toFixed(0)} KB`).join(' · ')||'Optional schematic images · up to 5 files, 8 MB each';};

  async function openScadFromChat(source) {
    try {
      const result=await call('/api/openscad/designs',{title:'Chat model',prompt:'OpenSCAD source created from a Chat response.',source,project_id:$('chatProject')?.value||null});
      document.querySelector('#nav button[data-page="openscad"]')?.click();
      await loadOpenScad(result.design.id);
      if(result.version.render_error)setStatus(`Saved as version 1; preview unavailable: ${result.version.render_error}`,true);
      else setStatus('OpenSCAD source from Chat saved and rendered as version 1.');
    } catch(error) { if(typeof toast==='function')toast(`Could not open model: ${error.message}`); }
  }
  window.loadOpenScad=loadOpenScad;
  window.openScadFromChat=openScadFromChat;

  document.addEventListener('click',event=>{
    const button=event.target.closest('.openScadCode');
    if(button){
      const source=button.closest('.codeCard')?.querySelector('pre code')?.textContent;
      if(source)openScadFromChat(source);
    }
  });
  const chatLog=$('chatLog');
  function addScadButtons() {
    chatLog.querySelectorAll('.codeCard').forEach(card=>{
      const language=card.querySelector('.codeLanguage')?.textContent.trim().toLowerCase();
      if((language==='scad'||language==='openscad')&&!card.querySelector('.openScadCode')){
        const button=document.createElement('button');button.type='button';button.className='softBtn openScadCode';button.textContent='Open in 3D modeller';
        card.querySelector('.codeButtons')?.append(button);
      }
    });
  }
  new MutationObserver(addScadButtons).observe(chatLog,{childList:true,subtree:true});
  addScadButtons();
  new ResizeObserver(draw).observe(canvas);
})();
