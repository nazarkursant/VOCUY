// Execute exported JSX against a small AE API double. Invoked by Python tests.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const result = {imports:0,comps:[],alerts:[]};
function Property() {
  this.keys=[];this.numKeys=0;
  this.value={resetCharStyle(){},resetParagraphStyle(){}};
  this.setValue=value=>{this.value=value;};
  this.setValueAtTime=(time,value)=>{this.keys.push([time,JSON.parse(JSON.stringify(value))]);this.numKeys=this.keys.length;};
  this.setInterpolationTypeAtKey=()=>{};
}
function Item(){this.remove=()=>{throw new Error('Unexpected rollback');};}
function Layer(kind,source) {
  this.kind=kind;this.source=source;this.props={};
  this.property=name=>this.props[name]||(this.props[name]=(name==='ADBE Transform Group'||name==='ADBE Text Properties'?new Layer('group'):new Property()));
}
function File(filename) {
  this.fsName=filename;this.exists=fs.existsSync(filename);this.parent={fsName:path.dirname(filename)};
}
const project={items:{
  addFolder:()=>new Item(),
  addComp(name,width,height,pixelAspect,duration,fps){
    const comp=new Item();Object.assign(comp,{name,width,height,duration,fps,allLayers:[],openInViewer(){}});
    comp.layers={add(source){const l=new Layer('audio',source);comp.allLayers.push(l);return l;},
      addBoxText(size){const l=new Layer('text');l.size=size;comp.allLayers.push(l);return l;}};
    result.comps.push(comp);return comp;
  }
},importFile(options){result.imports++;return new Item();}};
const context={app:{project,beginUndoGroup(){},endUndoGroup(){}},File,$:{fileName:process.argv[2]},
  ImportOptions:function(file){this.file=file;},MarkerValue:function(text){this.comment=text;},
  ParagraphJustification:{CENTER_JUSTIFY:2},KeyframeInterpolationType:{HOLD:1},
  alert(message){result.alerts.push(message);}};
const source=fs.readFileSync(process.argv[2],'utf8').replace(/^#target[^\n]*\n/,'');
vm.runInNewContext(source,context,{timeout:10000});
if(result.alerts.some(s=>!s.startsWith('VO Studio: created')))throw new Error(result.alerts.join('\n'));
result.comps=result.comps.map(c=>({name:c.name,width:c.width,height:c.height,duration:c.duration,
  audio:c.allLayers.filter(l=>l.kind==='audio').map(l=>({start:l.startTime,end:l.outPoint,label:l.label,markers:l.props['ADBE Marker'].keys})),
  text:c.allLayers.filter(l=>l.kind==='text').map(l=>({start:l.inPoint,end:l.outPoint,label:l.label,keys:l.props['ADBE Text Properties'].props['ADBE Text Document'].keys}))}));
process.stdout.write(JSON.stringify(result));
