'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function studio() {
  const html=fs.readFileSync(path.join(__dirname,'../static/index.html'),'utf8');
  const nodes=Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map(m=>[m[1],{
    value:'',textContent:'',hidden:false,disabled:false,dataset:{},style:{},
  }]));
  nodes.exportTarget.value='after_effects';nodes.aeResolution.value='1080x1920';nodes.aeSubtitles.value='words';nodes.aeFps.value='30';
  nodes.aeResult.hidden=true;
  const requests=[];
  const context=vm.createContext({document:{getElementById(id){assert.ok(nodes[id],`Missing element ${id}`);return nodes[id];},
    querySelectorAll(){return [];},addEventListener(){}},setTimeout(){},clearTimeout(){},
    fetch:async(url,options)=>{const body=JSON.parse(options.body);requests.push({url,body});const ext={after_effects:'jsx',premiere:'xml',resolve:'py'}[body.target];
      return {ok:true,json:async()=>({target:body.target,filename:`sample_${body.target}.${ext}`,path:`/Users/me/out/sample_${body.target}.${ext}`,
      subtitles:body.target==='after_effects'?0:1,subtitles_folder:'sample_subtitles',compositions:1,recordings:1,download_url:`/api/exports/example/sample.${ext}`,
    })};}});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8'),context);
  return {nodes,requests,run:code=>vm.runInContext(code,context)};
}

test('single export sends vertical settings and subtitle choice without API credentials',async()=>{
  const ui=studio();
  ui.run("destination={token:'chosen'};singleExportJob={id:'a'.repeat(32),has_wav:true,has_alignment:true,status:'complete'};syncAfterEffectsControls();");
  assert.equal(ui.nodes.createAeBtn.disabled,false);
  ui.nodes.aeSubtitles.value='off';ui.run('syncAfterEffectsControls()');
  await ui.run('createAfterEffects()');
  assert.equal(ui.requests.length,1);
  assert.deepEqual(ui.requests[0],{url:'/api/export',body:{target:'after_effects',destination_token:'chosen',
    settings:{width:1080,height:1920,fps:30,subtitles:'off'},job_id:'a'.repeat(32)}});
  assert.equal(ui.nodes.aeResult.hidden,false);
  assert.match(ui.nodes.aeResultText.textContent,/Saved sample_after_effects.jsx · 1 composition/);
  assert.equal(ui.nodes.createAeBtn.disabled,false);
  ui.nodes.aeSubtitles.value='phrases';ui.run('syncAfterEffectsControls()');
  assert.equal(ui.nodes.aeResult.hidden,true);
});

test('batch export includes every sequence despite a filtered preview',async()=>{
  const ui=studio();
  ui.run("mode='batch';batchId='b'.repeat(32);destination={token:'chosen'};selectedSequence='ONE';conversationTurns=['ONE','TWO'].map((video_id,i)=>({number:i+1,video_id,job:{id:String(i),has_wav:true,has_alignment:true,status:'complete'}}));syncAfterEffectsControls();");
  assert.match(ui.nodes.aeHint.textContent,/2 compositions · all sequences/);
  await ui.run('createAfterEffects()');
  assert.equal(ui.requests[0].body.batch_id,'b'.repeat(32));
  assert.equal(ui.requests[0].body.sequence_id,undefined);
});

test('export is unavailable during generation, for missing alignment, or for edited messages',()=>{
  const ui=studio();
  ui.run("mode='multi';batchId='b'.repeat(32);destination={token:'chosen'};conversationTurns=[{job:{id:'a',has_wav:true,has_alignment:true,status:'complete'}}];syncAfterEffectsControls();");
  assert.equal(ui.nodes.createAeBtn.disabled,false);
  ui.run('busy=true;syncAfterEffectsControls()');assert.equal(ui.nodes.createAeBtn.disabled,true);
  ui.run('busy=false;conversationTurns[0].dirty=true;syncAfterEffectsControls()');assert.equal(ui.nodes.createAeBtn.disabled,true);
  ui.run('conversationTurns[0].dirty=false;conversationTurns[0].job.has_alignment=false;syncAfterEffectsControls()');assert.equal(ui.nodes.createAeBtn.disabled,true);
  ui.run('conversationTurns[0].job.has_alignment=true;conversationTurns[0].job.word_count=0;syncAfterEffectsControls()');assert.equal(ui.nodes.createAeBtn.disabled,true);
});

test('failed regeneration exports the previous playable take but a new unaligned take blocks it',()=>{
  const ui=studio();
  ui.run("mode='multi';batchId='b'.repeat(32);destination={token:'chosen'};conversationTurns=[{job:{id:'new',status:'failed',has_wav:false},previous_job:{id:'old',status:'complete',has_wav:true,has_alignment:true}}];syncAfterEffectsControls();");
  assert.equal(ui.nodes.createAeBtn.disabled,false);
  ui.run("conversationTurns[0].job={id:'new',status:'partial',has_wav:true,has_alignment:false};syncAfterEffectsControls()");
  assert.equal(ui.nodes.createAeBtn.disabled,true);
});

test('Premiere Pro and DaVinci Resolve exports name their sequence type and import steps',async()=>{
  const ui=studio();
  ui.run("destination={token:'chosen'};singleExportJob={id:'a'.repeat(32),has_wav:true,has_alignment:true,status:'complete'};");
  ui.nodes.exportTarget.value='premiere';ui.run('syncAfterEffectsControls()');
  assert.equal(ui.nodes.createAeBtn.textContent,'Create a Premiere Pro Sequence');
  assert.match(ui.nodes.aeHint.textContent,/One sequence/);
  await ui.run('createAfterEffects()');
  assert.equal(ui.requests[0].body.target,'premiere');
  assert.match(ui.nodes.aeResultText.textContent,/sample_premiere.xml · 1 sequence/);
  assert.equal(ui.nodes.downloadAe.textContent,'Download XML');
  assert.match(ui.nodes.aeInstructions.textContent,/File → Import.*sample_subtitles/);
  ui.nodes.exportTarget.value='resolve';ui.run('syncAfterEffectsControls()');
  assert.equal(ui.nodes.aeResult.hidden,true);
  assert.equal(ui.nodes.createAeBtn.textContent,'Create a DaVinci Resolve Timeline');
  await ui.run('createAfterEffects()');
  assert.equal(ui.requests[1].body.target,'resolve');
  assert.match(ui.nodes.aeInstructions.textContent,/exec\(open\(r'\/Users\/me\/out\/sample_resolve.py'/);
});

test('lip sync is a separate export that needs a mouth folder and sends one per voice',async()=>{
  const ui=studio();
  ui.run("mode='multi';batchId='b'.repeat(32);destination={token:'chosen'};conversationTurns=[{job:{id:'a',has_wav:true,has_alignment:true,status:'complete'}}];syncAfterEffectsControls();");
  assert.equal(ui.nodes.createAeBtn.disabled,false);
  assert.equal(ui.nodes.createLipSyncBtn.disabled,true);
  assert.match(ui.nodes.lipSyncHint.textContent,/Choose a mouth folder/);
  assert.deepEqual([1,2,3].map(n=>ui.nodes['mouthRow'+n].hidden),[false,false,false]);
  ui.run("mouthFolders[0]={token:'m1',poses:20,table:'mouth_map.csv'};mouthFolders[2]={token:'m3',poses:3,table:'mouth_map.csv'};syncAfterEffectsControls();");
  assert.equal(ui.nodes.createLipSyncBtn.disabled,false);
  ui.nodes.aeFps.value='24';ui.nodes.aeResolution.value='1920x1080';
  await ui.run('createLipSync()');
  assert.deepEqual(ui.requests[0],{url:'/api/lip-sync',body:{destination_token:'chosen',settings:{width:1920,height:1080,fps:24},
    mouths:{1:'m1',3:'m3'},batch_id:'b'.repeat(32)}});
  assert.equal(ui.nodes.lipSyncResult.hidden,false);
  ui.run("mode='single';singleExportJob={id:'c'.repeat(32),has_wav:true,has_alignment:true,status:'complete'};syncAfterEffectsControls();");
  assert.deepEqual([1,2,3].map(n=>ui.nodes['mouthRow'+n].hidden),[false,true,true]);
  await ui.run('createLipSync()');
  assert.deepEqual(ui.requests[1].body.mouths,{1:'m1'});
  assert.equal(ui.requests[1].body.job_id,'c'.repeat(32));
});
