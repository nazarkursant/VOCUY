// Execute an exported lip-sync JSX against a small AE API double. Invoked by Python tests.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const result = {imports:[],comps:[],alerts:[]};
function Property() {
  this.keys=[];this.numKeys=0;this.hold=0;this.expression='';
  this.setValue=value=>{this.value=value;};
  this.setValuesAtTimes=(times,values)=>{times.forEach((t,i)=>this.keys.push([t,values[i]]));this.numKeys=this.keys.length;};
  this.setInterpolationTypeAtKey=(index,inType,outType)=>{if(inType===1&&outType===1)this.hold++;};
}
function Group(make) {
  this.props={};
  this.property=name=>this.props[name]||(this.props[name]=make(name));
}
function Item(){this.remove=()=>{throw new Error('Unexpected rollback');};}
function Layer(kind,source,comp) {
  this.kind=kind;this.source=source;this.parent=null;this.comp=comp;this.effects=[];
  this.transform=new Group(()=>new Property());
  this.timeRemap=new Property();
  this.property=name=>{
    if(name==='ADBE Transform Group')return this.transform;
    if(name==='ADBE Time Remapping'){if(!this.timeRemapEnabled)throw new Error('Time remapping is off');return this.timeRemap;}
    if(name==='ADBE Effect Parade')return {addProperty:match=>{const effect=new Group(()=>new Property());effect.match=match;this.effects.push(effect);return effect;}};
    throw new Error('Unknown property '+name);
  };
}
function File(filename) {
  this.fsName=filename;this.name=path.basename(filename);this.exists=fs.existsSync(filename);this.parent={fsName:path.dirname(filename)};
}
const project={items:{
  addFolder:name=>Object.assign(new Item(),{name}),
  addComp(name,width,height,pixelAspect,duration,fps){
    const comp=new Item();Object.assign(comp,{name,width,height,duration,fps,frameDuration:1/fps,layerList:[],openInViewer(){}});
    const add=layer=>{comp.layerList.unshift(layer);return layer;};
    comp.layers={add:source=>add(new Layer(source.kind||'footage',source,comp)),
      addNull:duration=>add(Object.assign(new Layer('null',null,comp),{duration}))};
    comp.kind='precomp';result.comps.push(comp);return comp;
  }
},importFile(options){
  result.imports.push({name:options.file.name,sequence:options.sequence});
  const audio=/\.wav$/i.test(options.file.name);
  return Object.assign(new Item(),{name:options.file.name,kind:audio?'audio':'image',width:audio?0:512,height:audio?0:512});
}};
const context={app:{project,beginUndoGroup(){},endUndoGroup(){}},File,$:{fileName:process.argv[2]},
  ImportOptions:function(file){this.file=file;},KeyframeInterpolationType:{HOLD:1},
  alert(message){result.alerts.push(message);}};
const source=fs.readFileSync(process.argv[2],'utf8').replace(/^#target[^\n]*\n/,'');
vm.runInNewContext(source,context,{timeout:10000});
if(result.alerts.some(s=>!s.startsWith('VOCUY: created')))throw new Error(result.alerts.join('\n'));
// Evaluate a mouth's time-remap expression for a slider value, as AE would.
function remapAt(layer,value) {
  const parent=layer.parent;
  const effect=parent&&parent.effects.find(e=>e.name==='Mouth pose');
  return vm.runInNewContext(layer.timeRemap.expression,{hasParent:Boolean(parent),
    parent:{effect:name=>(name==='Mouth pose'&&effect?()=>value:null)},source:layer.source});
}
result.comps=result.comps.map(c=>({name:c.name,width:c.width,height:c.height,duration:c.duration,
  layers:c.layerList.map(l=>({kind:l.kind,name:l.name,parent:l.parent&&l.parent.name,start:l.startTime,in:l.inPoint,out:l.outPoint,
    label:l.label,position:l.transform.props['ADBE Position']&&l.transform.props['ADBE Position'].value,
    source:l.source&&l.source.name,
    effects:l.effects.map(e=>({match:e.match,name:e.name,keys:e.props['ADBE Slider Control-0001'].keys,hold:e.props['ADBE Slider Control-0001'].hold})),
    remap:l.timeRemapEnabled?[1,2,3,99].map(v=>remapAt(l,v)):null}))}));
process.stdout.write(JSON.stringify(result));
