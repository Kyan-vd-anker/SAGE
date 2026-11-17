(() => {
  'use strict';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const state = { designs: [], design: null, version: null, pagePromise: null, svgUrl: '', triangles: [], yaw: .65, pitch: -.45, zoom: 1, drag: null };
  const $ = id => document.getElementById(id);

  async function call(url, body) {
    const response = await fetch(url, body ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {cache:'no-store'});
    let data = {};
    try { data = await response.json(); } catch { /* empty response */ }
    if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
    return data;
  }

  function setStatus(message, error=false) {
    const status = $('prototypeStatus');
    status.textContent = message || '';
    status.classList.toggle('openscadStatusError', error);
  }

  function clearSchematic() {
    if (state.svgUrl) URL.revokeObjectURL(state.svgUrl);
    state.svgUrl = '';
    $('prototypeSchematicPreview').removeAttribute('src');
    $('prototypeSchematicPreview').hidden = true;
    $('prototypeSchematicDownload').disabled = true;
  }

  function clearModel() {
    state.triangles = [];
    drawModel();
    $('prototypeCadStatus').textContent = 'The preview will appear here when OpenSCAD rendering is available.';
    $('prototypeCadStatus').classList.remove('openscadStatusError');
  }

  function download(name, content, type) {
    const url = URL.createObjectURL(new Blob([content], {type}));
    const link = document.createElement('a');
    link.href = url;
    link.download = name;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function renderRows(rows, columns) {
    if (!rows.length) return '<p class="meta">None listed.</p>';
    return `<div class="prototypeTableWrap"><table class="prototypeTable"><thead><tr>${columns.map(column=>`<th>${esc(column[1])}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${columns.map(column=>`<td>${esc(row[column[0]])||'—'}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
  }

  function renderPlan(plan) {
    $('prototypeSummary').textContent = `${plan.summary} · Target: ${plan.target}`;
    $('prototypeFirmwareLanguage').textContent = `Target: ${plan.target} · ${plan.firmware_language}. Firmware is a draft and has not been run on hardware.`;
    $('prototypeRequirements').innerHTML = renderRows(plan.requirements||[], [['id','ID'],['text','Requirement'],['verification','Suggested verification']]);
    $('prototypeAssumptions').innerHTML = (plan.assumptions||[]).length ? plan.assumptions.map(item=>`<li>${esc(item)}</li>`).join('') : '<li>None recorded.</li>';
    $('prototypeValidation').innerHTML = (plan.validation_steps||[]).length ? plan.validation_steps.map(item=>`<li>${esc(item)}</li>`).join('') : '<li>None listed.</li>';
    $('prototypeBom').innerHTML = renderRows(plan.components||[], [['ref','Ref'],['name','Component'],['part_number','Candidate part number'],['quantity','Qty'],['role','Role'],['status','Status']]);
    $('prototypeConnections').innerHTML = renderRows(plan.pin_map||[], [['board_pin','Board pin'],['signal','Signal'],['connects_to','Connects to'],['notes','Notes']]) +
      `<h4>Other connections</h4>${renderRows(plan.connections||[], [['from','From'],['to','To'],['signal','Signal'],['notes','Notes']])}`;
    $('prototypePlan').value = JSON.stringify(plan, null, 2);
  }

  function renderDesignList() {
    $('prototypeDesignList').innerHTML = state.designs.length ? state.designs.map(item =>
      `<button type="button" class="openscadDesign ${state.design?.id===item.id?'selected':''}" data-id="${item.id}"><strong>${esc(item.title)}</strong><small>v${item.version||0} · ${esc(item.updated_at)}</small></button>`
    ).join('') : '<p class="meta">No saved prototypes yet. Describe one above to create a coordinated draft package.</p>';
  }

  function projectOptions(projects) {
    const select = $('prototypeProject'), previous = select.value;
    select.innerHTML = '<option value="">No linked project</option>' + projects.map(project=>`<option value="${project.id}">${esc(project.name)}</option>`).join('');
    select.value = previous;
  }

  async function showVersion(id) {
    const version = await call(`/api/prototypes/versions/${id}`);
    state.version = version;
    $('prototypeVersion').value = String(id);
    renderPlan(version.plan);
    $('prototypeFirmware').value = version.firmware;
    $('prototypeFirmware').disabled = false;
    $('prototypeSchematicSpec').value = JSON.stringify(version.schematic_spec, null, 2);
    $('prototypeCadSource').value = version.openscad_source;
    $('prototypeSave').disabled = false;
    $('prototypeFirmwareDownload').disabled = false;
    $('prototypeCadDownload').disabled = false;
    clearSchematic();
    if (version.schematic_svg) {
      state.svgUrl = URL.createObjectURL(new Blob([version.schematic_svg], {type:'image/svg+xml'}));
      $('prototypeSchematicPreview').src = state.svgUrl;
      $('prototypeSchematicPreview').hidden = false;
      $('prototypeSchematicDownload').disabled = false;
      $('prototypeSchematicDownload').onclick = () => download(`${fileBase}_v${version.version}_schematic.svg`, version.schematic_svg, 'image/svg+xml');
      $('prototypeSchematicStatus').textContent = 'Generated schematic draft. Verify all symbols, connections, values, and ratings.';
    } else {
      $('prototypeSchematicStatus').textContent = version.schematic_error || 'No schematic preview was saved for this version.';
      $('prototypeSchematicStatus').classList.add('openscadStatusError');
    }
    if (version.schematic_svg) $('prototypeSchematicStatus').classList.remove('openscadStatusError');
    clearModel();
    if (version.render_error) {
      $('prototypeCadStatus').textContent = version.render_error;
      $('prototypeCadStatus').classList.add('openscadStatusError');
    } else if (version.mesh_url) {
      $('prototypeCadStatus').textContent = 'Loading STL preview…';
      try {
        const response = await fetch(version.mesh_url, {cache:'no-store'});
        if (!response.ok) throw new Error(`Could not load STL preview (${response.status}).`);
        loadStl(await response.arrayBuffer());
        $('prototypeCadStatus').textContent = 'Drag to rotate; scroll to zoom. Draft preview only—check dimensions and fit.';
        $('prototypeCadStatus').classList.remove('openscadStatusError');
      } catch (error) {
        $('prototypeCadStatus').textContent = error.message;
        $('prototypeCadStatus').classList.add('openscadStatusError');
      }
    }
    $('prototypeVersion').selectedOptions[0].textContent = `v${version.version} · ${version.created_at}`;
  }

  async function openDesign(id) {
    const design = await call(`/api/prototypes/${id}`);
    state.design = design;
    $('prototypeTitle').value = design.title;
    $('prototypeTitle').disabled = true;
    $('prototypeProject').value = design.project_id||'';
    $('prototypeProject').disabled = true;
    $('prototypeRevise').disabled = !design.versions.length;
    $('prototypeVersion').innerHTML = design.versions.map(version=>`<option value="${version.id}">v${version.version} · ${esc(version.created_at)}</option>`).join('');
    renderDesignList();
    if (design.versions.length) await showVersion(design.versions[0].id);
    else clearCurrentVersion();
  }

  function clearCurrentVersion() {
    state.version = null;
    $('prototypeVersion').innerHTML = '';
    $('prototypeSummary').textContent = 'Generate or open a prototype to inspect its plan.';
    $('prototypePlan').value = '';
    $('prototypeFirmware').value = '';
    $('prototypeFirmware').disabled = true;
    $('prototypeFirmwareDownload').disabled = true;
    $('prototypeCadDownload').disabled = true;
    $('prototypeSchematicSpec').value = '';
    $('prototypeSchematicStatus').textContent = 'The schematic will appear here when generated.';
    $('prototypeCadSource').value = '';
    $('prototypeSave').disabled = true;
    clearSchematic();
    clearModel();
  }

  async function loadPrototype(selectedId=null) {
    try {
      const [designs, projects] = await Promise.all([call('/api/prototypes'), call('/api/projects')]);
      state.designs = designs;
      projectOptions(projects);
      renderDesignList();
      const target = selectedId || state.design?.id || designs[0]?.id;
      if (target) await openDesign(target);
      else {
        state.design=null;
        $('prototypeTitle').disabled=false;
        $('prototypeProject').disabled=false;
        clearCurrentVersion();
        $('prototypeRevise').disabled=true;
      }
    } catch (error) { setStatus(error.message, true); }
  }

  async function createOrRevise(revise) {
    const prompt = $('prototypePrompt').value.trim();
    if (!prompt) { setStatus('Describe the prototype and its constraints.', true); return; }
    const generate = $('prototypeGenerate'), revision = $('prototypeRevise');
    generate.disabled = true;
    revision.disabled = true;
    setStatus(revise ? 'Sage is generating a coordinated revision…' : 'Sage is generating the plan, firmware, schematic, and model…');
    try {
      let result;
      if (revise) result = await call(`/api/prototypes/${state.design.id}/revise`, {prompt, version_id:state.version?.id});
      else result = await call('/api/prototypes', {title:$('prototypeTitle').value.trim(), project_id:$('prototypeProject').value||null, prompt});
      $('prototypePrompt').value = '';
      setStatus(`Saved prototype version ${result.version.version}. Each artifact is a draft for review.`);
      await loadPrototype(result.design.id);
    } catch (error) { setStatus(error.message, true); }
    finally { generate.disabled=false; revision.disabled=!state.design; }
  }

  async function saveVersion() {
    if (!state.design) return;
    try {
      const plan = JSON.parse($('prototypePlan').value);
      const schematicSpec = JSON.parse($('prototypeSchematicSpec').value);
      const result = await call(`/api/prototypes/${state.design.id}/versions`, {
        plan, firmware:$('prototypeFirmware').value, schematic_spec:schematicSpec,
        openscad_source:$('prototypeCadSource').value, notes:$('prototypeNotes').value,
        prompt:state.version?.prompt||'',
      });
      $('prototypeNotes').value = '';
      setStatus(`Saved edited artifacts as version ${result.version.version}.`);
      await loadPrototype(result.design.id);
    } catch (error) { setStatus(error instanceof SyntaxError ? `Invalid JSON: ${error.message}` : error.message, true); }
  }

  function normalize(vertices, bounds) {
    const center = bounds.slice(0,3).map((value,index)=>(value+bounds[index+3])/2);
    const scale = Math.max(bounds[3]-bounds[0],bounds[4]-bounds[1],bounds[5]-bounds[2])||1;
    for (let i=0;i<vertices.length;i+=3) {
      vertices[i]=(vertices[i]-center[0])/scale;
      vertices[i+1]=(vertices[i+1]-center[1])/scale;
      vertices[i+2]=(vertices[i+2]-center[2])/scale;
    }
    state.triangles=vertices;
    drawModel();
  }

  function loadStl(buffer) {
    const view=new DataView(buffer);
    if (buffer.byteLength>=84) {
      const count=view.getUint32(80,true);
      if (count>0 && 84+count*50===buffer.byteLength) {
        const skip=Math.max(1,Math.ceil(count/40000)),bounds=[Infinity,Infinity,Infinity,-Infinity,-Infinity,-Infinity],vertices=[];
        for (let face=0;face<count;face++) {
          const offset=84+face*50+12,triangle=[];
          for (let vertex=0;vertex<3;vertex++) for (let axis=0;axis<3;axis++) {
            const value=view.getFloat32(offset+vertex*12+axis*4,true);
            bounds[axis]=Math.min(bounds[axis],value);bounds[axis+3]=Math.max(bounds[axis+3],value);triangle.push(value);
          }
          if (face%skip===0) vertices.push(...triangle);
        }
        normalize(vertices,bounds);
        return;
      }
    }
    const text=new TextDecoder().decode(buffer),matches=text.matchAll(/\bvertex\s+([-+\d.eE]+)\s+([-+\d.eE]+)\s+([-+\d.eE]+)/gi),all=[];
    for (const match of matches) { all.push(Number(match[1]),Number(match[2]),Number(match[3])); if(all.length>1_080_000) throw Error('STL preview contains too many vertices.'); }
    if (!all.length) throw Error('OpenSCAD returned an unreadable STL preview.');
    const bounds=[Infinity,Infinity,Infinity,-Infinity,-Infinity,-Infinity];
    for (let i=0;i<all.length;i+=3) for (let axis=0;axis<3;axis++) {
      bounds[axis]=Math.min(bounds[axis],all[i+axis]);bounds[axis+3]=Math.max(bounds[axis+3],all[i+axis]);
    }
    const skip=Math.max(1,Math.ceil(all.length/3/40000)),vertices=[];
    for(let face=0;face<all.length/9;face+=skip) vertices.push(...all.slice(face*9,face*9+9));
    normalize(vertices,bounds);
  }

  function drawModel() {
    const canvas=$('prototypeCanvas');
    if (!canvas) return;
    const rect=canvas.getBoundingClientRect();
    if (!rect.width||!rect.height) return;
    const ratio=window.devicePixelRatio||1,width=Math.round(rect.width*ratio),height=Math.round(rect.height*ratio);
    if(canvas.width!==width||canvas.height!==height){canvas.width=width;canvas.height=height;}
    const context=canvas.getContext('2d');
    context.clearRect(0,0,width,height);
    if(!state.triangles.length)return;
    const cy=Math.cos(state.yaw),sy=Math.sin(state.yaw),cp=Math.cos(state.pitch),sp=Math.sin(state.pitch),points=[];
    for(let i=0;i<state.triangles.length;i+=3){
      const x=state.triangles[i],y=state.triangles[i+1],z=state.triangles[i+2],rx=x*cy-z*sy,rz=x*sy+z*cy;
      points.push([rx,y*cp-rz*sp,y*sp+rz*cp]);
    }
    let depthMin=Infinity,depthMax=-Infinity,extent=1;
    for(const point of points){depthMin=Math.min(depthMin,point[2]);depthMax=Math.max(depthMax,point[2]);extent=Math.max(extent,Math.abs(point[0])*2,Math.abs(point[1])*2);}
    const scale=Math.min(width,height)*.78/Math.max(extent,depthMax-depthMin)*state.zoom;
    const project=p=>[width/2+p[0]*scale,height/2-p[1]*scale,p[2]],faces=[];
    for(let i=0;i<points.length;i+=3){
      const a=points[i],b=points[i+1],c=points[i+2],ux=b[0]-a[0],uy=b[1]-a[1],uz=b[2]-a[2],vx=c[0]-a[0],vy=c[1]-a[1],vz=c[2]-a[2];
      const nx=uy*vz-uz*vy,ny=uz*vx-ux*vz,nz=ux*vy-uy*vx,length=Math.hypot(nx,ny,nz)||1,light=Math.max(0,(nx*.35+ny*.55+nz*.76)/length);
      faces.push({points:[project(a),project(b),project(c)],z:(a[2]+b[2]+c[2])/3,shade:Math.round(75+145*light)});
    }
    faces.sort((a,b)=>a.z-b.z);
    context.lineWidth=ratio*.6;
    for(const face of faces){
      context.beginPath();context.moveTo(face.points[0][0],face.points[0][1]);context.lineTo(face.points[1][0],face.points[1][1]);context.lineTo(face.points[2][0],face.points[2][1]);context.closePath();
      context.fillStyle=`rgb(${Math.round(face.shade*.65)},${face.shade},${Math.min(255,face.shade+38)})`;context.fill();context.strokeStyle='rgba(4,10,16,.48)';context.stroke();
    }
  }

  async function ensurePage() {
    if (!state.pagePromise) {
      state.pagePromise = fetch('/static/prototype.html', {cache:'no-store'}).then(response => {
        if (!response.ok) throw new Error(`Could not load Prototype Designer (${response.status}).`);
        return response.text();
      }).then(html => {
        const template=document.createElement('template');template.innerHTML=html;
        const section=template.content.firstElementChild;
        if(!section)throw new Error('Prototype Designer page is empty.');
        document.querySelector('main').append(section);
        section.classList.add('active');
        return section;
      });
    }
    return state.pagePromise;
  }

  async function loadPage() {
    try {
      await ensurePage();
      await loadPrototype();
    } catch(error) {
      const existing=$('prototypeStatus');
      if(existing)setStatus(error.message,true);
      else console.error(error);
    }
  }

  function bindPage() {
    const nav=$('nav'),scad=nav?.querySelector('[data-page="openscad"]');
    if(nav&&scad&&!nav.querySelector('[data-page="prototype"]')){
      const button=document.createElement('button');
      button.dataset.page='prototype';button.innerHTML='🧩 <span>Prototype designer</span>';button.title='Prototype designer';button.setAttribute('aria-label','Prototype designer');
      scad.parentElement.insertBefore(button,scad);
    }
    const style=document.createElement('link');style.rel='stylesheet';style.href='/static/prototype.css?v=1';document.head.append(style);
    window.loadPrototype=loadPage;
    document.addEventListener('submit',event=>{
      if(event.target.id==='prototypeForm'){event.preventDefault();createOrRevise(false);}
    });
    document.addEventListener('click',event=>{
      if(event.target.closest('#prototypeRevise'))createOrRevise(true);
      if(event.target.closest('#prototypeNew')){
        state.design=null;state.version=null;
        $('prototypeTitle').value='';$('prototypePrompt').value='';$('prototypeNotes').value='';
        $('prototypeTitle').disabled=false;$('prototypeProject').disabled=false;
        clearCurrentVersion();$('prototypeRevise').disabled=true;renderDesignList();setStatus('');
      }
      if(event.target.closest('#prototypeSave'))saveVersion();
      const designButton=event.target.closest('#prototypeDesignList .openscadDesign');
      if(designButton)openDesign(designButton.dataset.id).catch(error=>setStatus(error.message,true));
    });
    document.addEventListener('change',event=>{
      if(event.target.id==='prototypeVersion'&&event.target.value)showVersion(event.target.value).catch(error=>setStatus(error.message,true));
    });
    const canvas=$('prototypeCanvas');
    if(canvas){
      canvas.onpointerdown=event=>{state.drag=[event.clientX,event.clientY];canvas.setPointerCapture(event.pointerId);};
      canvas.onpointermove=event=>{if(!state.drag)return;state.yaw+=(event.clientX-state.drag[0])*.01;state.pitch=Math.max(-1.5,Math.min(1.5,state.pitch+(event.clientY-state.drag[1])*.01));state.drag=[event.clientX,event.clientY];drawModel();};
      canvas.onpointerup=()=>{state.drag=null;};
      canvas.onpointercancel=()=>{state.drag=null;};
      canvas.onwheel=event=>{event.preventDefault();state.zoom=Math.max(.4,Math.min(3,state.zoom*(event.deltaY<0?1.1:.9)));drawModel();};
      window.addEventListener('resize',drawModel);
    }
    document.addEventListener('click',event=>{
      if(event.target.closest('#prototypeFirmwareDownload')&&state.version){
        const language=(state.version.plan.firmware_language||'').toLowerCase(),extension=language.includes('arduino')||language.includes('ino')?'.ino':language.includes('python')?'.py':language.includes('c')?'.c':'.txt';
        download(`${(state.design?.title||'prototype').replace(/[^\w.-]+/g,'_')}_v${state.version.version}${extension}`,$('prototypeFirmware').value,'text/plain');
      }
      if(event.target.closest('#prototypeSchematicDownload')&&state.version)download(`${(state.design?.title||'prototype').replace(/[^\w.-]+/g,'_')}_v${state.version.version}_schematic.svg`,state.version.schematic_svg,'image/svg+xml');
      if(event.target.closest('#prototypeCadDownload')&&state.version)download(`${(state.design?.title||'prototype').replace(/[^\w.-]+/g,'_')}_v${state.version.version}.scad`,$('prototypeCadSource').value,'text/plain');
    });
  }

  bindPage();
})();
