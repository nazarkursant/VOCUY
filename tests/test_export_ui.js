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
  nodes.aeResolution.value='1080x1920';nodes.aeSubtitles.value='words';nodes.aeFps.value='30';
  nodes.aeResult.hidden=true;
  const requests=[];
  const context=vm.createContext({document:{getElementById(id){assert.ok(nodes[id],`Missing element ${id}`);return nodes[id];},
    querySelectorAll(){return [];},addEventListener(){}},setTimeout(){},clearTimeout(){},
    fetch:async(url,options)=>{requests.push({url,body:JSON.parse(options.body)});return {ok:true,json:async()=>({
      filename:'sample_after_effects.jsx',compositions:1,recordings:1,download_url:'/api/after-effects/example/sample_after_effects.jsx',
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
  assert.deepEqual(ui.requests[0],{url:'/api/after-effects',body:{destination_token:'chosen',
    settings:{width:1080,height:1920,fps:30,subtitles:'off'},job_id:'a'.repeat(32)}});
  assert.equal(ui.nodes.aeResult.hidden,false);
  assert.match(ui.nodes.aeResultText.textContent,/Saved sample_after_effects.jsx/);
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
