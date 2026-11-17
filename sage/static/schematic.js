(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const state = { designs: [], design: null, version: null, imageUrl: '' };

  async function call(url, body) {
    const response = await fetch(url, body ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {cache:'no-store'});
    let data = {};
    try { data = await response.json(); } catch { /* empty response */ }
    if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
    return data;
  }

  function setStatus(message, error=false) {
    const status = $('schematicStatus');
    status.textContent = message || '';
    status.classList.toggle('openscadStatusError', error);
  }

  function clearPreview() {
    if(state.imageUrl)URL.revokeObjectURL(state.imageUrl);
    state.imageUrl='';
    $('schematicPreview').removeAttribute('src');
    $('schematicPreview').hidden=true;
    $('schematicDownload').disabled=true;
    $('schematicSpec').textContent='';
  }

  function renderDesignList() {
    $('schematicDesignList').innerHTML=state.designs.length?state.designs.map(item=>`<button type="button" class="openscadDesign ${state.design?.id===item.id?'selected':''}" data-id="${item.id}"><strong>${esc(item.title)}</strong><small>v${item.version||0} · ${esc(item.updated_at)}</small></button>`).join(''):'<p class="meta">No saved schematics yet. Describe a circuit above to create one.</p>';
  }

  async function showVersion(id) {
    const version=await call(`/api/schematics/versions/${id}`);
    state.version=version;
    $('schematicVersion').value=String(id);
    $('schematicPreviewTitle').textContent=`${state.design.title} · v${version.version}`;
    clearPreview();
    state.imageUrl=URL.createObjectURL(new Blob([version.svg],{type:'image/svg+xml'}));
    $('schematicPreview').src=state.imageUrl;
    $('schematicPreview').hidden=false;
    $('schematicDownload').disabled=false;
    $('schematicDownload').onclick=()=>{
      const link=document.createElement('a');
      link.href=state.imageUrl;
      link.download=`${state.design.title.replace(/[^\w.-]+/g,'_')}_v${version.version}.svg`;
      link.click();
    };
    $('schematicSpec').textContent=JSON.stringify(version.spec,null,2);
  }

  async function openDesign(id) {
    const design=await call(`/api/schematics/designs/${id}`);
    state.design=design;
    $('schematicTitle').value=design.title;
    $('schematicProject').value=design.project_id||'';
    $('schematicRevise').disabled=!design.versions.length;
    $('schematicVersion').innerHTML=design.versions.map(version=>`<option value="${version.id}">v${version.version} · ${esc(version.created_at)}</option>`).join('');
    renderDesignList();
    if(design.versions.length)await showVersion(design.versions[0].id);
    else {state.version=null;clearPreview();}
  }

  async function loadSchematics(selectedId=null) {
    try {
      const [designs,projects]=await Promise.all([call('/api/schematics/designs'),call('/api/projects')]);
      state.designs=designs;
      $('schematicProject').innerHTML='<option value="">No linked project</option>'+projects.map(project=>`<option value="${project.id}">${esc(project.name)}</option>`).join('');
      renderDesignList();
      const target=selectedId||state.design?.id||designs[0]?.id;
      if(target)await openDesign(target);
      else {state.design=null;state.version=null;$('schematicRevise').disabled=true;clearPreview();}
    }catch(error){setStatus(error.message,true);}
  }

  async function createOrRevise(revise) {
    const prompt=$('schematicPrompt').value.trim();
    if(!prompt){setStatus('Describe the circuit and its connections.',true);return;}
    const generate=$('schematicGenerate'),revision=$('schematicRevise');
    generate.disabled=true;revision.disabled=true;
    setStatus(revise?'Sage is revising the schematic…':'Sage is creating the schematic…');
    try {
      let result;
      if(revise){
        result=await call(`/api/schematics/designs/${state.design.id}/revise`,{prompt,version_id:state.version?.id});
      }else{
        result=await call('/api/schematics/designs',{title:$('schematicTitle').value.trim(),project_id:$('schematicProject').value||null,prompt});
      }
      $('schematicPrompt').value='';
      setStatus(`Saved schematic version ${result.version.version}.`);
      await loadSchematics(result.design.id);
    }catch(error){setStatus(error.message,true);}
    finally{generate.disabled=false;revision.disabled=!state.design;}
  }

  $('schematicForm').onsubmit=event=>{event.preventDefault();createOrRevise(false);};
  $('schematicRevise').onclick=()=>createOrRevise(true);
  $('schematicNew').onclick=()=>{
    state.design=null;state.version=null;
    $('schematicTitle').value='';$('schematicPrompt').value='';$('schematicVersion').innerHTML='';
    $('schematicRevise').disabled=true;clearPreview();renderDesignList();setStatus('');
  };
  $('schematicDesignList').onclick=event=>{
    const button=event.target.closest('.openscadDesign');
    if(button)openDesign(button.dataset.id).catch(error=>setStatus(error.message,true));
  };
  $('schematicVersion').onchange=()=>showVersion($('schematicVersion').value).catch(error=>setStatus(error.message,true));
  window.loadSchematics=loadSchematics;
})();
