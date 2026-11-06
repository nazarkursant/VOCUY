const test=require('node:test');
const assert=require('node:assert/strict');
const {locate,OrderedPlayer}=require('../static/playback.js');
class FakeAudio extends EventTarget {
  constructor(){super();this.paused=true;this.ended=false;this.currentTime=0;this.duration=0;this._src='';}
  getAttribute(name){return name==='src'?this._src:null;}
  set src(value){this._src=value;this.currentTime=0;this.ended=false;}
  get src(){return this._src;}
  async play(){this.paused=false;this.ended=false;this.dispatchEvent(new Event('play'));}
  pause(){const wasPlaying=!this.paused;this.paused=true;if(wasPlaying)this.dispatchEvent(new Event('pause'));}
  end(){this.paused=true;this.ended=true;this.dispatchEvent(new Event('ended'));}
  metadata(duration){this.duration=duration;this.dispatchEvent(new Event('loadedmetadata'));}
}
const items=[{id:'a',number:1,duration:2,url:'/a.wav'},{id:'b',number:2,duration:3,url:'/b.wav'},{id:'c',number:4,duration:4,url:'/c.wav'}];
function setup(){const audio=new FakeAudio(),states=[],errors=[];const player=new OrderedPlayer(audio,s=>states.push(s),e=>errors.push(e));player.setItems(items);return {audio,player,states,errors};}
test('global seeking maps boundaries and skips missing recordings',()=>{
  assert.deepEqual(locate(items,2),{index:1,offset:0});
  assert.deepEqual(locate(items,6.5),{index:2,offset:1.5});
  assert.deepEqual(locate(items,100),{index:2,offset:4});
  assert.deepEqual(locate([],4),{index:-1,offset:0});
});
test('play all advances in order and highlights the currently playing message',async()=>{
  const {audio,player,states}=setup();player.toggle();assert.equal(audio.src,'/a.wav');
  assert.equal(states.at(-1).item.number,1);assert.equal(states.at(-1).playing,true);
  audio.end();assert.equal(audio.src,'/b.wav');assert.equal(states.at(-1).item.number,2);
  audio.end();assert.equal(audio.src,'/c.wav');assert.equal(states.at(-1).item.number,4);
  audio.end();assert.equal(player.index,-1);assert.equal(states.at(-1).playing,false);
  player.toggle();assert.equal(audio.src,'/a.wav');
});
test('pause and resume keep the current position',()=>{
  const {audio,player,states}=setup();player.select(1);audio.metadata(3);audio.currentTime=1.5;
  player.toggle();assert.equal(states.at(-1).playing,false);assert.equal(states.at(-1).elapsed,3.5);
  player.toggle();assert.equal(audio.currentTime,1.5);assert.equal(player.index,1);
});
test('seek to another file applies the offset when its metadata loads',()=>{
  const {audio,player}=setup();player.toggle();audio.metadata(2);player.seek(6.5);
  assert.equal(audio.src,'/c.wav');audio.metadata(4);assert.equal(audio.currentTime,1.5);
  assert.equal(player.state().elapsed,6.5);assert.equal(player.state().playing,true);
});
test('replacing a take stops stale playback; changing other takes preserves playback',()=>{
  const {audio,player}=setup();player.select(1);audio.currentTime=1;
  player.setItems([{...items[0],id:'new-a'},items[1],items[2]]);
  assert.equal(player.index,1);assert.equal(audio.currentTime,1);assert.equal(audio.paused,false);
  player.setItems([items[0],{...items[1],id:'new-b'},items[2]]);
  assert.equal(audio.paused,true);assert.equal(player.index,-1);
});
test('playback rejection reports an error without advancing the queue',async()=>{
  const {audio,player,errors}=setup();audio.play=async()=>{throw new Error('blocked');};
  await player.select(0);assert.equal(errors.length,1);assert.equal(player.index,0);
});
test('paused seek followed by play keeps the requested offset before metadata loads',()=>{
  const {audio,player}=setup();player.seek(6.5);player.toggle();audio.metadata(4);
  assert.equal(player.index,2);assert.equal(audio.currentTime,1.5);
});
test('shared audio uses occurrence IDs so polling keeps the correct sequence highlighted',()=>{
  const {audio,player}=setup();
  const shared=[{id:'shared:1',number:1,duration:2,url:'/shared.wav'},items[1],{id:'shared:3',number:3,duration:2,url:'/shared.wav'}];
  player.setItems(shared);player.select(2);audio.metadata(2);audio.currentTime=1;
  player.setItems(shared.map(item=>({...item})));
  assert.equal(player.index,2);assert.equal(player.state().item.number,3);assert.equal(audio.currentTime,1);
  assert.equal(player.state().elapsed,6);
});
test('consecutive uses of the same recording restart from the beginning',()=>{
  const {audio,player}=setup();
  player.setItems([{id:'shared:1',number:1,duration:2,url:'/shared.wav'},{id:'shared:2',number:2,duration:2,url:'/shared.wav'}]);
  player.toggle();audio.metadata(2);audio.currentTime=2;audio.end();
  assert.equal(player.index,1);assert.equal(audio.currentTime,0);assert.equal(player.state().playing,true);
});
