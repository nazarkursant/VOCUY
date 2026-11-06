/* A single audio element drives the complete ordered timeline. */
(function(root) {
  'use strict';
  function locate(items, seconds) {
    let remaining = Math.max(0, Number(seconds) || 0);
    for (let i = 0; i < items.length; i++) {
      const duration = Math.max(0, items[i].duration || 0);
      if (remaining < duration || i === items.length - 1) return {index:i, offset:Math.min(remaining,duration)};
      remaining -= duration;
    }
    return {index:-1, offset:0};
  }
  class OrderedPlayer {
    constructor(audio, onState, onError) {
      this.audio=audio;this.onState=onState;this.onError=onError;
      this.items=[];this.index=-1;this.seekOffset=null;this.epoch=0;
      audio.addEventListener('timeupdate',()=>this.emit());
      audio.addEventListener('play',()=>this.emit());
      audio.addEventListener('pause',()=>this.emit());
      audio.addEventListener('loadedmetadata',()=>{
        if (this.seekOffset !== null) {audio.currentTime=Math.min(this.seekOffset,audio.duration||this.seekOffset);this.seekOffset=null;}
        this.emit();
      });
      audio.addEventListener('ended',()=>{
        if(this.index+1<this.items.length)this.select(this.index+1,0,true);
        else {this.index=-1;audio.currentTime=0;this.emit();}
      });
      audio.addEventListener('error',()=>{audio.pause();this.onError('Could not play this recording. Try the message again.');this.emit();});
    }
    setItems(items) {
      const current=this.items[this.index];
      this.items=items;
      const next=current ? items.findIndex(x=>x.id===current.id) : -1;
      if(current && next<0)this.stop();
      else this.index=next;
      this.emit();
    }
    state() {
      const item=this.items[this.index];
      const start=this.items.slice(0,Math.max(0,this.index)).reduce((n,x)=>n+x.duration,0);
      const elapsed=this.seekOffset === null ? this.audio.currentTime||0 : this.seekOffset;
      return {item,index:this.index,playing:!this.audio.paused && !this.audio.ended,
        elapsed:item ? start+Math.min(elapsed,item.duration) : 0,
        total:this.items.reduce((n,x)=>n+x.duration,0)};
    }
    emit() {this.onState(this.state());}
    async select(index,offset=0,play=true) {
      if(!this.items[index])return;
      const epoch=++this.epoch;
      this.audio.pause();this.index=index;
      const item=this.items[index];
      if(this.audio.getAttribute('src')!==item.url) {
        this.seekOffset=offset;this.audio.src=item.url;
      } else {this.seekOffset=null;this.audio.currentTime=offset;}
      this.emit();
      if(play) {
        try {await this.audio.play();}
        catch(error) {if(epoch===this.epoch && error.name!=='AbortError')this.onError('Playback could not start. Press play to try again.');}
      }
    }
    toggle() {
      if(!this.audio.paused && !this.audio.ended) {this.audio.pause();return;}
      if(this.index<0)this.select(0);
      else this.select(this.index,this.seekOffset===null?(this.audio.currentTime||0):this.seekOffset,true);
    }
    seek(seconds) {
      const target=locate(this.items,seconds);
      this.select(target.index,target.offset,!this.audio.paused && !this.audio.ended);
    }
    stop() {this.epoch++;this.audio.pause();this.index=-1;this.seekOffset=null;this.emit();}
  }
  const api={locate,OrderedPlayer};
  if(typeof module!=='undefined' && module.exports)module.exports=api;
  else root.StudioPlayback=api;
})(typeof window!=='undefined' ? window : globalThis);
