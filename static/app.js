'use strict';

const $ = (id) => document.getElementById(id);
const PREF_KEY = 'vo-studio-preferences-v1';
let voices = [];
let voiceName = '';
let activeJobId = null;
let jobTimer = null;
let busy = false;
let toastTimer = null;

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
    throw new Error('Local server unavailable. Please relaunch VO Studio.');
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
  const prefs = {
    name:$('projectName').value, episodeId:$('episodeId').value,
    voiceId:$('voiceId').value, voiceName, modelId:$('modelId').value,
    sampleRate:document.querySelector('[name="quality"]:checked').value, ...{
      stability:$('stability').value, similarity:$('similarity').value,
      style:$('style').value, speed:$('speed').value, boost:$('boost').checked
    }
  };
  try { localStorage.setItem(PREF_KEY, JSON.stringify(prefs)); } catch (_) {}
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
}
function showStatus(job) {
  const box = $('statusBox'); box.hidden = false;
  $('statusTitle').textContent = job.step || 'Processing';
  $('statusPill').textContent = (job.status || 'working').toUpperCase();
  $('statusDetail').textContent = job.error || (
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
  savePrefs();
  setBusy(true);
  $('resultBox').hidden = true;
  showStatus({step:'Queued',status:'working'});
  try {
    const body = {
      api_key:key, name:$('projectName').value || 'Voiceover', episode_id:$('episodeId').value,
      voice_id:voice, voice_name:voiceName, model_id:$('modelId').value,
      settings:settings(), sample_rate:Number(document.querySelector('[name="quality"]:checked').value),
      text,
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
  copy.append(element('div','history-name', (item.episode_id ? item.episode_id + ' · ' : '')+(item.name||'Voiceover')));
  const stamp = item.created_at ? new Date(item.created_at).toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}) : '';
  let desc = stamp + (item.duration ? ` · ${item.duration.toFixed(1)}s` : '') + ' · ' + (item.status || '');
  copy.append(element('div','history-details',desc));row.append(copy);
  const actions = element('div','history-actions');
  if (item.has_wav) {const a=element('a','', 'WAV');a.href=`/api/download/${item.id}/wav`;a.title='Download audio';actions.append(a);}
  if (item.has_alignment) {const a=element('a','', 'JSON');a.href=`/api/download/${item.id}/alignment`;a.title='Download timestamps';actions.append(a);}
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
  try {await api('/api/open-folder',{});} catch (err) {toast(err.message);}
}

function init() {
  loadPrefs(); updateCounts();
  $('toggleKey').addEventListener('click', () => {
    const input = $('apiKey');
    const reveal = input.type === 'password';
    input.type = reveal ? 'text' : 'password';
    $('toggleKey').textContent = reveal ? 'Hide' : 'Show';
  });
  $('loadVoicesBtn').addEventListener('click',loadVoices);
  $('voicePicker').addEventListener('change',() => {
    const voice=voices.find(v => v.voice_id===$('voicePicker').value);
    if (voice) {$('voiceId').value=voice.voice_id;voiceName=voice.name;savePrefs();}
  });
  $('voiceId').addEventListener('input',() => {
    const matched=voices.find(v => v.voice_id===$('voiceId').value);
    voiceName=matched ? matched.name : '';
    if ($('voicePicker').value!==$('voiceId').value) $('voicePicker').value='';
    savePrefs();
  });
  ['projectName','episodeId','modelId','boost'].forEach(id => $(id).addEventListener('change',savePrefs));
  ['stability','similarity','style','speed'].forEach(id => $(id).addEventListener('input',()=>{updateSliders();savePrefs();}));
  document.querySelectorAll('[name="quality"]').forEach(x => x.addEventListener('change',savePrefs));
  $('resetSettings').addEventListener('click',()=>{
    $('stability').value=defaults.stability;$('similarity').value=defaults.similarity;
    $('style').value=defaults.style;$('speed').value=defaults.speed;
    $('boost').checked=defaults.boost;updateSliders();savePrefs();
  });
  $('scriptText').addEventListener('input',updateCounts);
  $('generateBtn').addEventListener('click',generate);
  $('retryBtn').addEventListener('click',()=>retryAlignment($('retryBtn').dataset.id));
  $('refreshHistory').addEventListener('click',loadHistory);
  $('openFolderBtn').addEventListener('click',openFolder);
  loadHistory();
}

document.addEventListener('DOMContentLoaded', init);
