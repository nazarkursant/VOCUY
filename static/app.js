'use strict';

const $ = (id) => document.getElementById(id);
const PREF_KEY = 'vo-studio-preferences-v1';
let voices = [];
let voiceName = '';
let activeJobId = null;
let jobTimer = null;
let busy = false;
let toastTimer = null;
let mode = 'single';
let activeVoice = 0;
let singleProfile = null;
let batchProfile = null;
let voiceProfiles = [];
let conversationTurns = [];
let batchId = null;
let batchTimer = null;
let preparedSource = '';
let destination = null;
let folderBusy = false;
const modeScripts = {single:'', multi:'', batch:''};
const workspaces = {multi:{id:null,turns:[],source:'',editing:true},batch:{id:null,turns:[],source:'',editing:true}};
let editingScript = true;
let preparing = false;
let editingTurn = null;
let player = null;
let timelineSignature = '';
let highlightedNumber = null;
let csvColumns = [];
let staticColumns = new Set();
let csvInspectionSource = '';
let csvInspectionTimer = null;
let csvInspectionVersion = 0;
let selectedSequence = '';
let singleExportJob = null;
let aeExporting = false;
let aeExportSignature = '';

const defaults = {stability:.5, similarity:.75, style:0, speed:1, boost:true};

function toast(message) {
  const box = $('toast');
  box.textContent = message;
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => box.hidden = true, 7000);
}

async function api(endpoint, body) {
  let response;
  try {
    response = await fetch(endpoint, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  } catch (_) {
    throw new Error('Local server unavailable. Please relaunch VOCUY.');
  }
  let json;
  try { json = await response.json(); } catch (_) { throw new Error('Unexpected response from the local app.'); }
  if (!response.ok) throw new Error(json.error || `Request failed (${response.status})`);
  return json;
}

function num(id) { return Number($(id).value); }
function settings() {
  return {
    stability:num('stability'), similarity_boost:num('similarity'),
    style:num('style'), speed:num('speed'), use_speaker_boost:$('boost').checked
  };
}
function updateSliders() {
  ['stability','similarity','style','speed'].forEach(id => {
    const slider = $(id);
    const x = (Number(slider.value)-Number(slider.min))/(Number(slider.max)-Number(slider.min));
    slider.style.background = `linear-gradient(to right,#acdabc 0%,#acdabc ${x*100}%,#3a4841 ${x*100}%,#3a4841 100%)`;
    $(id+'Value').textContent = Number(slider.value).toFixed(2) + (id==='speed'?'×':'');
  });
}
function savePrefs() {
  rememberProfile();
  rememberWorkspace();
  const prefs = {
    name:$('projectName').value, episodeId:$('episodeId').value,
    voiceId:$('voiceId').value, voiceName, modelId:$('modelId').value,
    mode, singleProfile, batchProfile, voiceProfiles,
    batchIds:{multi:workspaces.multi.id,batch:workspaces.batch.id},
    afterEffects:{resolution:$('aeResolution').value,subtitles:$('aeSubtitles').value,fps:$('aeFps').value},
    sampleRate:document.querySelector('[name="quality"]:checked').value, ...{
      stability:$('stability').value, similarity:$('similarity').value,
      style:$('style').value, speed:$('speed').value, boost:$('boost').checked
    }
  };
  try { localStorage.setItem(PREF_KEY, JSON.stringify(prefs)); } catch (_) {}
  if (conversationTurns.length) conversationTurns.forEach(updateMessage);
  if (player) refreshTimeline();
}
function loadPrefs() {
  try {
    const data = JSON.parse(localStorage.getItem(PREF_KEY) || '{}');
    if (typeof data.name === 'string') $('projectName').value = data.name;
    if (typeof data.episodeId === 'string') $('episodeId').value = data.episodeId;
    if (typeof data.voiceId === 'string') $('voiceId').value = data.voiceId;
    if (typeof data.voiceName === 'string') voiceName = data.voiceName;
    if ([...$('modelId').options].some(o => o.value === data.modelId)) $('modelId').value = data.modelId;
    if (['44100','24000'].includes(data.sampleRate)) document.querySelector(`[name="quality"][value="${data.sampleRate}"]`).checked = true;
    for (const id of ['stability','similarity','style','speed']) {
      const slider = $(id);
      if (data[id] !== undefined && Number(data[id]) >= Number(slider.min) && Number(data[id]) <= Number(slider.max)) slider.value = data[id];
    }
    if (typeof data.boost === 'boolean') $('boost').checked = data.boost;
    for (const [id,key] of [['aeResolution','resolution'],['aeSubtitles','subtitles'],['aeFps','fps']]) {
      const value=data.afterEffects?.[key];
      if ([...$(id).options].some(o=>o.value===value)) $(id).value=value;
    }
  } catch (_) {}
  updateSliders();
}

async function loadVoices() {
  const key = $('apiKey').value.trim();
  if (!key) { toast('Enter your ElevenLabs API key first.'); $('apiKey').focus(); return; }
  const btn = $('loadVoicesBtn');
  btn.disabled = true;
  const initial = btn.textContent;
  btn.textContent = 'Loading…';
  try {
    const data = await api('/api/voices', {api_key:key});
    voices = data.voices || [];
    const picker = $('voicePicker');
    picker.replaceChildren();
    const initialOption = new Option(`Choose from ${voices.length} available voices…`, '');
    picker.append(initialOption);
    for (const voice of voices) {
      const o = new Option(voice.name + (voice.category ? ' · ' + voice.category : ''), voice.voice_id);
      picker.append(o);
    }
    if (voices.some(v => v.voice_id === $('voiceId').value)) picker.value = $('voiceId').value;
    picker.disabled = false;
    if (!voices.length) toast('No saved voices were returned. You can paste a voice ID manually.');
  } catch (err) { toast(err.message); }
  finally { btn.disabled = false; btn.textContent = initial; }
}
function updateCounts() {
  const txt = $('scriptText').value;
  $('characterCount').textContent = txt.length.toLocaleString();
  $('wordEstimate').textContent = (txt.match(/\S+/g) || []).length.toLocaleString();
}
function setBusy(value) {
  busy = value;
  $('generateBtn').disabled = value;
  $('generateLabel').textContent = value ? 'Working on your voiceover…' : 'Generate WAV + JSON';
  $('retryBtn').disabled = value;
  syncConversationControls();
}
function showStatus(job) {
  $('singleResultPanel').hidden = mode !== 'single';
  const box = $('statusBox'); box.hidden = false;
  $('statusTitle').textContent = job.step || 'Processing';
  $('statusPill').textContent = (job.status || 'working').toUpperCase();
  $('statusDetail').textContent = job.export_error || job.error || (
    job.step === 'Generating audio' ? 'ElevenLabs is synthesizing your narration in PCM format.' :
    job.step === 'Aligning words' ? 'Audio saved. Computing timestamps for each spoken word.' :
    job.status === 'complete' ? 'Your audio and word/character timing JSON are ready.' :
    'Preparing your files…');
  let pct = job.step === 'Queued' ? 7 : job.step === 'Generating audio' ? 43 : job.step === 'Aligning words' ? 80 : 100;
  if (job.status === 'failed') pct = 12;
  $('progressBar').style.width = pct + '%';
  box.style.borderColor = (job.status === 'partial'||job.status === 'failed') ? '#a27a54' : '';
}
function showResult(job) {
  if(mode==='single')singleExportJob=job;
  syncAfterEffectsControls();
  if (!job.has_wav) { $('resultBox').hidden = true; return; }
  $('resultBox').hidden = false;
  $('resultTitle').textContent = job.name || 'Voiceover';
  $('resultMeta').textContent = `${job.duration ? job.duration.toFixed(1)+'s · ' : ''}${job.sample_rate/1000} kHz WAV${job.word_count!==null&&job.word_count!==undefined ? ' · '+job.word_count+' words aligned' : ''}`;
  $('downloadWav').href = `/api/download/${job.id}/wav`;
  $('downloadJson').href = `/api/download/${job.id}/alignment`;
  $('downloadJson').hidden = !job.has_alignment;
  $('retryBtn').hidden = job.status !== 'partial';
  $('retryBtn').dataset.id = job.id;
  const player = $('audioPlayer');
  const url = `/api/media/${job.id}/wav`;
  if (!player.currentSrc || !player.currentSrc.endsWith(url)) player.src = url;
  if (job.status === 'partial') $('resultMeta').textContent += ' · Alignment needs retry';
  if (job.export_error) toast(job.export_error);
}
async function pollJob(id) {
  if (jobTimer) {clearTimeout(jobTimer); jobTimer=null;}
  try {
    const response = await fetch(`/api/jobs/${id}`, {cache:'no-store'});
    if (!response.ok) throw new Error('Could not read job status.');
    const data = await response.json();
    const job = data.job;
    if (id !== activeJobId) return;
    showStatus(job);
    if (job.status === 'complete' || job.status === 'partial' || job.status === 'failed') {
      setBusy(false);
      showResult(job);
      if (job.status === 'failed') toast(job.error || 'Speech generation failed.');
      if (job.status === 'partial') toast('WAV saved. Alignment failed; you can retry without regenerating the audio.');
      await loadHistory();
      if(mode !== 'single' && batchId) await pollBatch();
    } else {
      jobTimer = setTimeout(() => pollJob(id), 1300);
    }
  } catch (err) {
    setBusy(false);
    toast(err.message);
  }
}
async function generate() {
  if (busy) return;
  const key = $('apiKey').value.trim();
  const voice = $('voiceId').value.trim();
  const text = $('scriptText').value.trim();
  if (!key) {toast('Paste your API key before generating.'); $('apiKey').focus();return;}
  if (!voice) {toast('Choose a voice or enter a voice ID.');$('voiceId').focus();return;}
  if (!text) {toast('Paste your narration script before generating.');$('scriptText').focus();return;}
  if (!destination) {toast('Choose the destination folder before generating.');return;}
  savePrefs();
  singleExportJob=null;
  setBusy(true);
  $('resultBox').hidden = true;
  showStatus({step:'Queued',status:'working'});
  try {
    const body = {
      api_key:key, name:$('projectName').value || 'Voiceover', episode_id:$('episodeId').value,
      voice_id:voice, voice_name:voiceName, model_id:$('modelId').value,
      settings:settings(), sample_rate:Number(document.querySelector('[name="quality"]:checked').value),
      text, destination_token:destination.token,
    };
    const data = await api('/api/generate', body);
    activeJobId = data.job_id;
    await pollJob(data.job_id);
  } catch (err) {setBusy(false); $('statusBox').hidden = true;toast(err.message);}
}
async function retryAlignment(id) {
  if (busy) return;
  const key = $('apiKey').value.trim();
  if (!key) {toast('Enter your API key to retry alignment.'); $('apiKey').focus();return;}
  setBusy(true);
  try {
    const response = await api(`/api/retry/${id}`, {api_key:key});
    activeJobId = response.job_id;
    await pollJob(response.job_id);
  } catch (err) {setBusy(false);toast(err.message);}
}
function element(tag, className, text) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (text!==undefined) e.textContent = text;
  return e;
}
function makeHistoryRow(item) {
  const row = element('div', 'history-row');
  const icon = element('div','history-icon');
  icon.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14m5-11v8m5-12v16M3 10v4"/></svg>';
  row.append(icon);
  const copy = element('div','history-copy');
  const turnLabel = item.turn_number ? ` · ${String(item.turn_number).padStart(3,'0')} · Voice ${item.speaker}` : '';
  copy.append(element('div','history-name', (item.episode_id ? item.episode_id + ' · ' : '')+(item.name||'Voiceover')+turnLabel));
  const stamp = item.created_at ? new Date(item.created_at).toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}) : '';
  let desc = stamp + (item.duration ? ` · ${item.duration.toFixed(1)}s` : '') + ' · ' + (item.status || '');
  copy.append(element('div','history-details',desc));row.append(copy);
  const actions = element('div','history-actions');
  if (item.has_wav) {const a=element('a','', 'WAV');a.href=`/api/download/${item.id}/wav`;a.title='Download audio';actions.append(a);}
  if (item.has_alignment) {const a=element('a','', 'JSON');a.href=`/api/download/${item.id}/alignment`;a.title='Download timestamps';actions.append(a);}
  if (item.has_wav) {
    const b=element('button','use-export','Use');b.type='button';b.title='Use this recording for playback and After Effects export';b.disabled=busy||aeExporting;
    b.addEventListener('click',async()=>{
      if(busy||aeExporting)return;
      $('historyDialog').close();setMode('single');activeJobId=item.id;
      await pollJob(item.id);
    });actions.append(b);
  }
  if (item.status==='partial') {const b=element('button','', 'Retry'); b.type='button';b.title='Retry only alignment'; b.addEventListener('click', () => retryAlignment(item.id));actions.append(b);}
  row.append(actions);return row;
}
async function loadHistory() {
  try {
    const response = await fetch('/api/history', {cache:'no-store'});
    if (!response.ok) throw new Error('Could not load history');
    const data = await response.json();
    const list = $('historyList');
    list.replaceChildren();
    if (!data.items || data.items.length===0) list.append(element('div','history-empty','Your completed voiceovers will show up here.'));
    else data.items.slice(0,9).forEach(item => list.append(makeHistoryRow(item)));
  } catch (err) {toast(err.message);}
}
async function openFolder() {
  try {await api('/api/open-folder', destination ? {destination_token:destination.token} : {});} catch (err) {toast(err.message);}
}

function init() {
  loadPrefs(); updateCounts();
  initConversation();
  $('toggleKey').addEventListener('click', () => {
    const input = $('apiKey');
    const reveal = input.type === 'password';
    input.type = reveal ? 'text' : 'password';
    $('toggleKey').textContent = reveal ? 'Hide' : 'Show';
  });
  $('loadVoicesBtn').addEventListener('click',loadVoices);
  $('voicePicker').addEventListener('change',() => {
    const voice=voices.find(v => v.voice_id===$('voicePicker').value);
    if (voice) {$('voiceId').value=voice.voice_id;voiceName=voice.name;$('voiceDisplayName').value=voice.name;savePrefs();}
  });
  $('voiceId').addEventListener('input',() => {
    const matched=voices.find(v => v.voice_id===$('voiceId').value);
    voiceName=matched ? matched.name : '';
    $('voiceDisplayName').value=voiceName;
    if ($('voicePicker').value!==$('voiceId').value) $('voicePicker').value='';
    savePrefs();
  });
  ['projectName','episodeId','modelId','boost'].forEach(id => $(id).addEventListener('change',savePrefs));
  $('voiceDisplayName').addEventListener('input',()=>{voiceName=$('voiceDisplayName').value;savePrefs();});
  ['stability','similarity','style','speed'].forEach(id => $(id).addEventListener('input',()=>{updateSliders();savePrefs();}));
  document.querySelectorAll('[name="quality"]').forEach(x => x.addEventListener('change',savePrefs));
  $('resetSettings').addEventListener('click',()=>{
    $('stability').value=defaults.stability;$('similarity').value=defaults.similarity;
    $('style').value=defaults.style;$('speed').value=defaults.speed;
    $('boost').checked=defaults.boost;updateSliders();savePrefs();
  });
  $('scriptText').addEventListener('input',()=>{updateCounts();syncConversationControls();scheduleCsvInspection();});
  $('generateBtn').addEventListener('click',generate);
  $('retryBtn').addEventListener('click',()=>retryAlignment($('retryBtn').dataset.id));
  $('refreshHistory').addEventListener('click',loadHistory);
  $('openFolderBtn').addEventListener('click',openFolder);
  $('createAeBtn').addEventListener('click',createAfterEffects);
  ['aeResolution','aeSubtitles','aeFps'].forEach(id=>$(id).addEventListener('change',()=>{savePrefs();syncAfterEffectsControls();}));
  loadHistory();
}

document.addEventListener('DOMContentLoaded', init);

function captureProfile() {
  return {voice_id:$('voiceId').value.trim(),voice_name:$('voiceDisplayName').value.trim(),
    model_id:$('modelId').value,settings:settings(),
    sample_rate:Number(document.querySelector('[name="quality"]:checked').value)};
}
function rememberProfile() {
  if(!singleProfile)return;
  if(mode==='multi')voiceProfiles[activeVoice]=captureProfile();
  else if(mode==='batch')batchProfile=captureProfile();
  else singleProfile=captureProfile();
}
function rememberWorkspace() {
  if(mode==='single')return;
  workspaces[mode]={id:batchId,turns:conversationTurns,source:preparedSource,editing:editingScript};
}
function applyProfile(profile) {
  $('voiceId').value=profile.voice_id||'';voiceName=profile.voice_name||'';
  $('voiceDisplayName').value=voiceName;
  $('voicePicker').value=voices.some(v=>v.voice_id===profile.voice_id)?profile.voice_id:'';
  $('modelId').value=[...$('modelId').options].some(o=>o.value===profile.model_id)?profile.model_id:'eleven_multilingual_v2';
  const values=profile.settings||{};
  for(const [id,key] of Object.entries({stability:'stability',similarity:'similarity_boost',style:'style',speed:'speed'})) {
    const input=$(id), value=Number(values[key]);
    input.value=Number.isFinite(value)&&value>=Number(input.min)&&value<=Number(input.max)?value:defaults[id];
  }
  $('boost').checked=values.use_speaker_boost!==false;
  document.querySelector(`[name="quality"][value="${profile.sample_rate===24000?24000:44100}"]`).checked=true;
  updateSliders();
}
function selectVoice(index) {
  rememberProfile();activeVoice=index;applyProfile(voiceProfiles[index]);
  document.querySelectorAll('#voiceTabs button').forEach((b,i)=>b.setAttribute('aria-pressed',String(i===index)));
  $('voiceProfileHint').textContent=`Voice ${index+1} settings`;savePrefs();
}
function setMode(next) {
  if(next!==mode) {
    rememberProfile();rememberWorkspace();modeScripts[mode]=$('scriptText').value;
    player?.stop();$('audioPlayer').pause();clearTimeout(batchTimer);
    mode=next;$('scriptText').value=modeScripts[next];
    const workspace=workspaces[next];
    batchId=workspace?.id||null;conversationTurns=workspace?.turns||[];
    preparedSource=workspace?.source||'';editingScript=workspace?.editing??true;
    renderMessages();
  }
  applyProfile(mode==='multi'?voiceProfiles[activeVoice]:mode==='batch'?batchProfile:singleProfile);
  document.body.classList.toggle('multi-mode',mode==='multi');
  document.body.classList.toggle('batch-mode',mode==='batch');
  for(const name of ['single','multi','batch'])$(name+'Mode').setAttribute('aria-pressed',String(name===mode));
  $('voiceTabs').hidden=$('voiceProfileHint').hidden=mode!=='multi';
  $('prepareControls').hidden=mode==='single';$('csvUpload').hidden=$('csvHint').hidden=$('cakeHookOption').hidden=mode!=='batch';
  $('modeHint').textContent={single:'One voice for your whole script.',multi:'Three voices. One conversation.',batch:'CSV to separate clips, organized by video.'}[mode];
  $('editorHeading').textContent={single:'Narration text',multi:'Conversation script',batch:'Batch CSV'}[mode];
  $('conversationHeading').textContent=mode==='batch'?'Batch clips':'Conversation';
  $('scriptText').maxLength=mode==='batch'?1000000:10000;
  $('scriptText').placeholder=mode==='batch'?'video_id,segment_id,text\nVIDEO_001,topic,"A short title"\nVIDEO_001,what,"The explanation goes here."':mode==='multi'?'1. Hi, how are you?\n2. Good! Is Alex joining?\n1. Yes, here he is.\n3. Hello, everyone!':'Paste your voiceover script here.';
  $('parsingHint').textContent=mode==='batch'?'Use video_id followed by speech columns (e.g. intro, narration, outro). The explainer preset and video_id / segment_id / text CSVs also work.':'Blank paragraphs alternate Voices 1 and 2. Or label each turn with 1., 2., or 3.';
  $('characterLimit').textContent=mode==='batch'?' CSV characters':' / 10,000 characters';
  $('csvFileName').hidden=mode!=='batch';
  $('generationHint').textContent=mode==='batch'?'Saves each sequence under audio/candidates/ and static recordings once under audio/shared/. Earlier takes are kept.':'Speech + forced alignment use ElevenLabs credits.';
  renderStaticColumns();scheduleCsvInspection();
  updateCounts();syncConversationControls();refreshTimeline();savePrefs();
  if(mode!=='single'&&batchId&&!conversationTurns.length){setBusy(true);pollBatch(true);}
}
function syncConversationControls() {
  const stale=Boolean(conversationTurns.length&&preparedSource!==$('scriptText').value);
  const locked=busy||preparing||aeExporting;
  $('scriptPanel').hidden=mode!=='single'&&conversationTurns.length>0&&!editingScript;
  $('conversationPanel').hidden=mode==='single'||!conversationTurns.length||editingScript;
  $('scriptChanged').hidden=!stale;
  for(const name of ['single','multi','batch'])$(name+'Mode').disabled=locked||folderBusy;
  $('scriptText').disabled=$('csvFile').disabled=$('stripCakeHook').disabled=locked;
  document.querySelectorAll('#staticColumnChoices input').forEach(e=>e.disabled=locked);
  $('prepareBtn').disabled=locked;
  $('prepareBtn').textContent=preparing?'Preparing…':mode==='batch'?'Prepare batch':'Prepare speech';
  $('editScriptBtn').disabled=locked;
  $('chooseFolderBtn').disabled=$('useFolderBtn').disabled=locked||folderBusy;
  $('destinationPath').disabled=locked||folderBusy;
  document.querySelectorAll('#voiceTabs button,.settings-col input,.settings-col select,#resetSettings,#loadVoicesBtn').forEach(e=>e.disabled=locked);
  $('voicePicker').disabled=locked||!voices.length;
  $('generateBtn').hidden=mode!=='single';
  $('generateBtn').disabled=locked||folderBusy||!destination||!$('scriptText').value.trim();
  $('singleResultPanel').hidden=mode!=='single'||($('statusBox').hidden&&$('resultBox').hidden);
  $('generateConversationBtn').hidden=mode==='single'||Boolean(batchId&&!busy);
  $('generateConversationBtn').disabled=locked||folderBusy||stale||editingScript||!destination||!conversationTurns.length||Boolean(batchId);
  $('generateConversationBtn').textContent=busy?'Generating…':batchId?'Generated · hover a message for another take':`Generate ${recordingTurns().length||''} ${mode==='batch'?'recordings':'messages'}`;
  document.querySelectorAll('.regenerate-button').forEach(e=>e.disabled=locked||folderBusy||stale||!destination||!batchId);
  document.querySelectorAll('.edit-message').forEach(e=>e.disabled=locked);
  document.querySelectorAll('.use-export').forEach(e=>e.disabled=locked);
  syncAfterEffectsControls();
}

function syncAfterEffectsControls() {
  const jobs=mode==='single'?(singleExportJob?[singleExportJob]:[]):conversationTurns.map(playableJob);
  const generated=mode==='single'?Boolean(singleExportJob):Boolean(batchId&&conversationTurns.length);
  const dirty=mode!=='single'&&(conversationTurns.some(t=>t.dirty)||preparedSource!==$('scriptText').value);
  const ready=generated&&jobs.length>0&&jobs.every(j=>j?.has_wav&&j.has_alignment&&j.word_count!==0&&j.status!=='working');
  $('createAeBtn').disabled=busy||preparing||folderBusy||aeExporting||!destination||!ready||dirty;
  $('createAeBtn').textContent=aeExporting?'Creating After Effects script…':'Create an After Effects Composition';
  ['aeResolution','aeSubtitles','aeFps'].forEach(id=>$(id).disabled=aeExporting);
  $('aeHint').textContent=aeExporting?'Saving the script to your destination folder…':busy?'Wait for audio and word timings to finish.':
    dirty?'Generate your edited messages before exporting.':!ready?'Generate audio and word timings to export. Word markers are included with subtitles Off.':
    !destination?'Choose a destination folder for the script.':mode==='batch'?
    `${new Set(conversationTurns.map(t=>t.video_id)).size} compositions · all sequences · shared recordings reused · word markers included.`:
    'One composition · word markers included · uses saved audio, no credits.';
  const signature=JSON.stringify([mode,batchId,jobs.map(j=>j?.id),destination?.token,dirty,$('aeResolution').value,$('aeSubtitles').value,$('aeFps').value]);
  if(signature!==aeExportSignature){$('aeResult').hidden=true;aeExportSignature=signature;}
}

async function createAfterEffects() {
  if($('createAeBtn').disabled)return;
  const [width,height]=$('aeResolution').value.split('x').map(Number);
  const body={destination_token:destination.token,settings:{width,height,fps:Number($('aeFps').value),subtitles:$('aeSubtitles').value},
    ...(mode==='single'?{job_id:singleExportJob.id}:{batch_id:batchId})};
  aeExporting=true;$('aeResult').hidden=true;syncConversationControls();
  try {
    const result=await api('/api/after-effects',body);
    $('aeResultText').textContent=`Saved ${result.filename} · ${result.compositions} composition${result.compositions===1?'':'s'} · ${result.recordings} recording${result.recordings===1?'':'s'}.`;
    $('downloadAe').href=result.download_url;
    $('aeResult').hidden=false;
  }catch(err){toast(err.message);}
  finally{aeExporting=false;syncConversationControls();}
}
async function prepareConversation() {
  if(busy||preparing)return;
  preparing=true;$('prepareError').hidden=true;syncConversationControls();
  try {
    if(mode==='batch'&&csvInspectionSource!==$('scriptText').value)await inspectCsvColumns();
    const data=await api(mode==='batch'?'/api/prepare-csv':'/api/prepare',{text:$('scriptText').value,strip_cake_hook:$('stripCakeHook').checked,static_columns:[...staticColumns]});
    player.stop();clearTimeout(batchTimer);batchId=null;conversationTurns=data.turns;
    selectedSequence='';
    preparedSource=$('scriptText').value;editingScript=false;
    $('conversationSummary').textContent=mode==='batch'?`${data.videos} sequences · ${data.turns.length} messages · ${data.recordings} recordings`:`${data.turns.length} messages · click to edit before generating`;
    renderMessages();refreshTimeline();savePrefs();
  }catch(err){$('prepareError').textContent=err.message;$('prepareError').hidden=false;}
  finally{preparing=false;syncConversationControls();}
}
function profileFor(turn) {return mode==='batch'?batchProfile:voiceProfiles[turn.speaker-1];}
function nameFor(turn) {return profileFor(turn)?.voice_name||turn.job?.voice_name||`Voice ${turn.speaker}`;}
function playableJob(turn) {return turn.job?.has_wav?turn.job:turn.previous_job?.has_wav?turn.previous_job:null;}
function recordingTurns(turns=conversationTurns) {
  const seen=new Set();
  return turns.filter(t=>{if(!t.static_key)return true;if(seen.has(t.static_key))return false;seen.add(t.static_key);return true;});
}
function visibleTurns() {return conversationTurns.filter(t=>mode!=='batch'||!selectedSequence||t.video_id===selectedSequence);}
function sharedCopies(turn) {return turn.static_key?conversationTurns.filter(t=>t.static_key===turn.static_key):[turn];}
function renderStaticColumns() {
  $('staticColumnControls').hidden=mode!=='batch'||!csvColumns.length;
  $('cakeHookOption').hidden=mode!=='batch'||!csvColumns.some(c=>c.id==='cake');
  const list=$('staticColumnChoices');list.replaceChildren();
  csvColumns.forEach(column=>{
    const label=element('label','batch-option'),input=document.createElement('input');
    input.type='checkbox';input.value=column.id;input.checked=staticColumns.has(column.id);input.disabled=busy||preparing;
    input.addEventListener('change',()=>{if(input.checked)staticColumns.add(column.id);else staticColumns.delete(column.id);});
    label.append(input,document.createTextNode(column.label));list.append(label);
  });
}
function scheduleCsvInspection() {
  clearTimeout(csvInspectionTimer);
  if(mode==='batch')csvInspectionTimer=setTimeout(inspectCsvColumns,350);
}
async function inspectCsvColumns() {
  clearTimeout(csvInspectionTimer);
  const text=$('scriptText').value,version=++csvInspectionVersion;
  if(mode!=='batch')return;
  try {
    const data=text.trim()?await api('/api/inspect-csv',{text}):{columns:[]};
    if(version!==csvInspectionVersion||mode!=='batch'||$('scriptText').value!==text)return;
    csvColumns=data.columns;csvInspectionSource=text;
    staticColumns=new Set([...staticColumns].filter(key=>csvColumns.some(c=>c.id===key)));
  }catch(_) {
    if(version!==csvInspectionVersion||mode!=='batch'||$('scriptText').value!==text)return;
    csvColumns=[];csvInspectionSource='';
  }
  renderStaticColumns();
}
function iconButton(className,label,path) {
  const button=element('button',className);button.type='button';button.title=label;button.setAttribute('aria-label',label);
  button.innerHTML=`<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${path}"/></svg>`;
  return button;
}
function renderMessages() {
  $('sequenceFilter').hidden=mode!=='batch';
  if(mode==='batch') {
    const videos=[...new Set(conversationTurns.map(t=>t.video_id))];
    if(!videos.includes(selectedSequence))selectedSequence='';
    const picker=$('sequencePicker');picker.replaceChildren(new Option('All sequences',''));
    videos.forEach(id=>picker.add(new Option(id,id)));picker.value=selectedSequence;
  }
  const list=$('messageList');list.replaceChildren();let group='';
  visibleTurns().forEach(turn=>{
    if(mode==='batch'&&turn.video_id!==group){group=turn.video_id;list.append(element('div','batch-group',group));}
    const card=element('article','message');card.dataset.number=turn.number;card.setAttribute('role','listitem');
    const body=element('button','message-body');body.type='button';
    const label=element('span','message-label');label.append(element('span','message-number'),element('span','message-name'));
    body.append(label,element('span','message-text'));
    body.addEventListener('click',()=>{
      const index=player.items.findIndex(x=>x.number===turn.number);
      if(index<0){if(!busy)openMessageEditor(turn);return;}
      if(player.index===index&&player.state().playing)player.toggle();else player.select(index);
    });
    const actions=element('div','message-actions');
    const edit=iconButton('edit-message',`Edit message ${turn.number}`,'m15 5 4 4M4 16l-1 5 5-1L20 8a3 3 0 0 0-4-4Z');
    edit.addEventListener('click',()=>openMessageEditor(turn));
    const regenerate=iconButton('regenerate-button',`Regenerate message ${turn.number}`,'M20 7v5h-5M4 17v-5h5M5 9a7 7 0 0 1 12-3l3 6M4 12l3 6a7 7 0 0 0 12-3');
    if(turn.static_key)regenerate.title='Regenerate this static message in every sequence';
    regenerate.addEventListener('click',()=>generateMessage(turn));actions.append(edit,regenerate);
    card.append(body,actions);list.append(card);updateMessage(turn);
  });
}
function updateMessage(turn) {
  const card=document.querySelector(`.message[data-number="${turn.number}"]`);if(!card)return;
  card.dataset.speaker=turn.speaker;
  card.querySelector('.message-number').textContent=String(turn.number).padStart(2,'0');
  card.querySelector('.message-name').textContent=nameFor(turn);
  card.querySelector('.message-text').textContent=turn.text;
  card.querySelector('.regenerate-button').hidden=!turn.job;
  const error=[turn.job?.error,turn.job?.export_error].filter(Boolean).join(' ');
  card.classList.toggle('has-error',Boolean(error));
  card.title=error||(turn.dirty?'Edited text; regenerate to update the recording.':turn.video_id?`${turn.video_id} / ${turn.segment_id}${turn.static_key?' · Shared across sequences':''}`:'');
  card.setAttribute('aria-busy',String(turn.job?.status==='working'));
}
function openMessageEditor(turn) {
  if(busy)return;editingTurn=turn;
  $('messageDialogTitle').textContent=`Message ${String(turn.number).padStart(2,'0')}`;
  $('messageText').value=turn.text;$('messageSpeaker').value=String(turn.speaker);
  $('messageSpeakerField').hidden=mode==='batch';
  $('sharedMessageHint').hidden=!turn.static_key;
  [...$('messageSpeaker').options].forEach((o,i)=>o.textContent=voiceProfiles[i]?.voice_name||`Voice ${i+1}`);
  $('messageDialog').showModal();
}
function saveMessage(event) {
  event.preventDefault();if(!editingTurn)return;
  const text=$('messageText').value.trim();if(!text)return;
  for(const turn of sharedCopies(editingTurn)) {
    turn.text=text;turn.speaker=mode==='batch'?1:Number($('messageSpeaker').value);turn.dirty=true;updateMessage(turn);
  }
  $('scriptText').value=mode==='batch'?csvFromTurns(conversationTurns):conversationTurns.map(t=>`${t.speaker}. ${t.text}`).join('\n\n');
  preparedSource=$('scriptText').value;updateCounts();rememberWorkspace();
  refreshTimeline();$('messageDialog').close();editingTurn=null;
  syncConversationControls();
}
function timeLabel(seconds) {
  seconds=Math.max(0,Math.floor(seconds||0));const minutes=Math.floor(seconds/60);
  return `${minutes}:${String(seconds%60).padStart(2,'0')}`;
}
function refreshTimeline() {
  if(!player)return;
  const items=visibleTurns().map(turn=>({turn,job:playableJob(turn)})).filter(x=>x.job).map(({turn,job})=>({id:`${job.id}:${turn.number}`,number:turn.number,speaker:turn.speaker,name:nameFor(turn),duration:Math.max(.01,job.duration||.01),url:`/api/media/${job.id}/wav`}));
  $('timeline').hidden=!items.length||mode==='single';
  const signature=items.map(x=>`${x.id}:${x.duration}:${x.name}`).join('|');
  if(signature!==timelineSignature) {
    timelineSignature=signature;
    const track=$('timelineClips');track.replaceChildren();track.classList.toggle('crowded',items.length>25);
    items.forEach((item,i)=>{
      const clip=element('button','timeline-clip',String(item.number).padStart(2,'0'));clip.type='button';clip.style.flexGrow=String(item.duration);clip.dataset.speaker=item.speaker;clip.dataset.number=item.number;
      clip.title=`Message ${item.number} · ${item.name}`;clip.setAttribute('aria-label',clip.title);
      clip.addEventListener('click',()=>player.select(i));track.append(clip);
    });
  }
  player.setItems(items);
}
function playbackState(state) {
  $('playAllBtn').textContent=state.playing?'Ⅱ':'▶';$('playAllBtn').setAttribute('aria-label',state.playing?'Pause conversation':'Play all messages');
  $('timelineCurrent').textContent=timeLabel(state.elapsed);$('timelineTotal').textContent=timeLabel(state.total);
  $('timelineSeek').max=String(state.total||1);$('timelineSeek').value=String(state.elapsed);
  $('timelineSeek').setAttribute('aria-valuetext',`${timeLabel(state.elapsed)} of ${timeLabel(state.total)}`);
  $('timelineLabel').textContent=state.item?`${String(state.item.number).padStart(2,'0')} · ${state.item.name}`:'Listen in order';
  document.querySelectorAll('.message.is-playing').forEach(e=>e.classList.remove('is-playing'));
  document.querySelectorAll('.timeline-clip.is-current').forEach(e=>e.classList.remove('is-current'));
  if(state.item)document.querySelector(`.timeline-clip[data-number="${state.item.number}"]`)?.classList.add('is-current');
  const number=state.playing?state.item?.number:null;
  if(number) {
    const card=document.querySelector(`.message[data-number="${number}"]`);card?.classList.add('is-playing');
    if(card&&number!==highlightedNumber) {
      const list=$('messageList'),bounds=list.getBoundingClientRect(),rect=card.getBoundingClientRect();
      if(rect.top<bounds.top||rect.bottom>bounds.bottom)list.scrollTo({top:list.scrollTop+rect.top-bounds.top-14,behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'});
    }
  }
  highlightedNumber=number;
}
async function chooseDestination(usePath=false) {
  if(busy||folderBusy)return;folderBusy=true;syncConversationControls();
  $('destinationHint').textContent=usePath?'Checking folder…':'Choose a folder in the system dialog…';
  try {
    const data=await api(usePath?'/api/destination':'/api/choose-folder',usePath?{path:$('destinationPath').value}:{});
    if(data.destination){destination=data.destination;$('destinationPath').value=destination.path;}
    $('destinationHint').textContent=destination?'Folder selected. Earlier takes are kept.':'Choose a folder before generating.';
  }catch(err){$('destinationHint').textContent=err.message;}
  finally{folderBusy=false;syncConversationControls();}
}
function batchRequest() {
  rememberProfile();savePrefs();const key=$('apiKey').value.trim();
  if(!key){$('apiKey').focus();throw new Error('Enter your ElevenLabs API key before generating.');}
  if(!destination)throw new Error('Choose the destination folder before generating.');
  return {api_key:key,mode,name:$('projectName').value,episode_id:$('episodeId').value,profiles:mode==='batch'?[batchProfile,batchProfile,batchProfile]:voiceProfiles,destination_token:destination.token};
}
async function generateConversation() {
  if(busy||batchId||!conversationTurns.length)return;
  try {
    const body=batchRequest();player.stop();setBusy(true);
    const turns=conversationTurns.map(t=>({text:t.text,speaker:t.speaker,video_id:t.video_id,segment_id:t.segment_id,script_revision:t.script_revision,static_key:t.static_key}));
    const data=await api('/api/batches',{...body,turns});batchId=data.batch_id;savePrefs();await pollBatch();
  }catch(err){setBusy(false);toast(err.message);}
}
async function generateMessage(turn) {
  if(busy||!batchId)return;
  try {
    const body=batchRequest();player.stop();setBusy(true);
    await api(`/api/batches/${batchId}/regenerate`,{...body,number:turn.number,text:turn.text,speaker:turn.speaker});
    sharedCopies(turn).forEach(t=>t.dirty=false);await pollBatch();
  }catch(err){setBusy(false);toast(err.message);}
}
function csvFromTurns(turns) {
  const quote=value=>'"'+String(value).replace(/"/g,'""')+'"';
  return 'video_id,segment_id,text\n'+turns.map(t=>[t.video_id,t.segment_id,t.text].map(quote).join(',')).join('\n');
}
async function pollBatch(restore=false) {
  clearTimeout(batchTimer);const id=batchId,owner=mode;if(!id)return;
  try {
    const response=await fetch(`/api/batches/${id}`,{cache:'no-store'});
    if(!response.ok){if(response.status===404){batchId=null;setBusy(false);savePrefs();return;}throw new Error('Could not read generation status.');}
    const {batch}=await response.json();if(batchId!==id||owner!==mode)return;
    if(restore||!conversationTurns.length) {
      conversationTurns=batch.turns;
      if(mode==='batch')staticColumns=new Set(batch.turns.map(t=>t.static_key).filter(Boolean));
      $('scriptText').value=mode==='batch'?csvFromTurns(batch.turns):batch.turns.map(t=>`${t.speaker}. ${t.text}`).join('\n\n');
      preparedSource=$('scriptText').value;editingScript=false;updateCounts();renderMessages();
    }else for(const saved of batch.turns) {
      const turn=conversationTurns.find(t=>t.number===saved.number);
      if(turn){turn.job=saved.job;turn.previous_job=saved.previous_job;updateMessage(turn);}
    }
    const recordings=recordingTurns(batch.turns),ready=recordings.filter(t=>t.job?.has_wav).length;
    const errors=recordings.filter(t=>t.job?.error||t.job?.export_error);
    const sequences=mode==='batch'?`${new Set(batch.turns.map(t=>t.video_id)).size} sequences · `:'';
    $('conversationSummary').textContent=batch.working?`${ready} / ${recordings.length} ready · generating in order`:`${sequences}${ready} ${mode==='batch'?'recordings':'messages'} ready${errors.length?` · ${errors.length} need attention — hover the outlined messages`:''}`;
    setBusy(batch.working);refreshTimeline();rememberWorkspace();
    if(batch.working)batchTimer=setTimeout(()=>pollBatch(),1200);else loadHistory();
  }catch(err){$('conversationSummary').textContent='Reconnecting… Generation may still be running.';batchTimer=setTimeout(()=>pollBatch(restore),3000);}
}
async function importCsv() {
  const file=$('csvFile').files[0];if(!file)return;
  if(file.size>4000000){toast('Choose a CSV smaller than 4 MB.');return;}
  preparing=true;syncConversationControls();
  try {
    const text=new TextDecoder('utf-8',{fatal:true}).decode(await file.arrayBuffer());
    if(text.length>1000000)throw new Error('Use a CSV with at most 1,000,000 characters.');
    $('scriptText').value=text;$('csvFileName').textContent=file.name;staticColumns.clear();updateCounts();await inspectCsvColumns();
  }catch(err){toast(err instanceof TypeError?'Save the CSV as UTF-8 and try again.':err.message);}
  finally{preparing=false;syncConversationControls();}
}
function initConversation() {
  $('voiceDisplayName').value=voiceName;singleProfile=captureProfile();batchProfile=structuredClone(singleProfile);
  const blank={...singleProfile,voice_id:'',voice_name:'',settings:{stability:.5,similarity_boost:.75,style:0,speed:1,use_speaker_boost:true}};
  voiceProfiles=[structuredClone(singleProfile),structuredClone(blank),structuredClone(blank)];
  let savedMode='single';
  try {
    const prefs=JSON.parse(localStorage.getItem(PREF_KEY)||'{}');
    if(prefs.singleProfile&&typeof prefs.singleProfile==='object')singleProfile=prefs.singleProfile;
    if(prefs.batchProfile&&typeof prefs.batchProfile==='object')batchProfile=prefs.batchProfile;
    if(Array.isArray(prefs.voiceProfiles)&&prefs.voiceProfiles.length===3&&prefs.voiceProfiles.every(p=>p&&typeof p==='object'))voiceProfiles=prefs.voiceProfiles;
    if(['multi','batch'].includes(prefs.mode))savedMode=prefs.mode;
    for(const name of ['multi','batch']) {
      const id=prefs.batchIds?.[name]||(name==='multi'?prefs.batchId:null);
      if(typeof id==='string'&&/^[a-f0-9]{32}$/.test(id))workspaces[name].id=id;
    }
  }catch(_){}
  applyProfile(singleProfile);
  player=new StudioPlayback.OrderedPlayer($('conversationAudio'),playbackState,toast);
  $('conversationAudio').addEventListener('play',()=>$('audioPlayer').pause());
  $('audioPlayer').addEventListener('play',()=>player.stop());
  for(const name of ['single','multi','batch'])$(name+'Mode').addEventListener('click',()=>setMode(name));
  document.querySelectorAll('#voiceTabs button').forEach((b,i)=>b.addEventListener('click',()=>selectVoice(i)));
  $('prepareBtn').addEventListener('click',prepareConversation);
  $('editScriptBtn').addEventListener('click',()=>{player.stop();editingScript=true;syncConversationControls();scheduleCsvInspection();});
  $('chooseFolderBtn').addEventListener('click',()=>chooseDestination());
  $('useFolderBtn').addEventListener('click',()=>chooseDestination(true));
  $('destinationPath').addEventListener('input',()=>{destination=null;$('destinationHint').textContent='Select “Use path” to confirm this folder.';syncConversationControls();});
  $('generateConversationBtn').addEventListener('click',generateConversation);
  $('playAllBtn').addEventListener('click',()=>player.toggle());
  $('timelineSeek').addEventListener('input',()=>player.seek(Number($('timelineSeek').value)));
  $('messageForm').addEventListener('submit',saveMessage);
  $('closeMessage').addEventListener('click',()=>$('messageDialog').close());
  $('csvFile').addEventListener('change',importCsv);
  $('sequencePicker').addEventListener('change',()=>{player.stop();selectedSequence=$('sequencePicker').value;renderMessages();refreshTimeline();syncConversationControls();});
  $('historyBtn').addEventListener('click',()=>$('historyDialog').showModal());
  $('closeHistory').addEventListener('click',()=>$('historyDialog').close());
  setMode(savedMode);$('voiceProfileHint').textContent='Voice 1 settings';
}
