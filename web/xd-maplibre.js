import {circumference, earthRadius, mercatorFrame} from './xd-basemap.js';

let library;
function loadMapLibre() {
 if (!library) {
  const css = document.createElement('link');
  css.rel='stylesheet';
  css.href='/vendor/maplibre/maplibre-gl.css';
  document.head.append(css);
  library=import('/vendor/maplibre/maplibre-gl.mjs').then(module=>{
   module.setWorkerUrl('/vendor/maplibre/maplibre-gl-worker.mjs');
   return module;
  }).catch(error=>{
   css.remove();
   library=null;
   throw error;
  });
 }
 return library;
}

export function localCamera(view, datum) {
 if (![datum.lat,datum.lon,datum.altitude_m,view.x,view.y,view.scale].every(Number.isFinite)
     || Math.abs(datum.lat)>85 || view.scale<=0) return null;
 const {eastScale,northScale,originX,originY}=mercatorFrame(datum);
 const x = originX + view.x/eastScale;
 const y = originY + view.y/northScale;
 const lat=Math.atan(Math.sinh(y/earthRadius))*180/Math.PI;
 if (Math.abs(lat)>85.051129) return null;
 return {center:[x/earthRadius*180/Math.PI,lat],
  zoom:Math.log2(view.scale * circumference * eastScale / 512),
  northRatio:eastScale/northScale};
}

export class VectorBasemap {
 constructor(container, redraw, report) {
  this.container=container;
  this.redraw=redraw;
  this.report=report;
  this.generation=0;
  this.northRatio=1;
 }

 select(source, datum) {
  if (source!==this.source) {
   this.generation++;
   this.map?.remove();
   this.map=null;
   this.pending=false;
   this.failed=false;
   this.ready=false;
   this.lastCamera=null;
   this.lastSize=null;
   this.container.hidden=true;
   this.container.dataset.ready='false';
  }
  this.source=source;
  this.datum=datum;
 }

 update(view,size,pixelRatio) {
  this.northRatio=1;
  if (!this.source || !this.datum) return false;
  const camera=localCamera(view,this.datum);
  if (!camera) {
   this.container.hidden=true;
   this.lastSize=null;
   this.report('已超出底图投影范围，请点击地图适配。');
   return false;
  }
  if (this.failed) {
   this.report(this.failed);
   return false;
  }
  if (!this.map && !this.pending) this.start(camera,pixelRatio);
  if (!this.map) return false;
  this.container.hidden=false;
  const sizeKey=`${size.width}:${size.height}`;
  if (pixelRatio!==this.lastPixelRatio) {
   this.map.setPixelRatio(pixelRatio);
   this.lastPixelRatio=pixelRatio;
  } else if (sizeKey!==this.lastSize) {
   this.map.resize();
  }
  this.lastSize=sizeKey;
  const cameraKey=`${camera.center}:${camera.zoom}`;
  if (cameraKey!==this.lastCamera) {
   this.map.jumpTo({center:camera.center,zoom:camera.zoom,bearing:0,pitch:0});
   this.lastCamera=cameraKey;
  }
  this.report(this.ready ? this.layerError || '' : '正在加载 MapLibre 底图…');
  if (this.ready) this.northRatio=camera.northRatio;
  return this.ready;
 }

 async start(camera,pixelRatio) {
  const generation=this.generation;
  this.pending=true;
  this.layerError='';
  this.report('正在加载 MapLibre 底图…');
  try {
   const {Map}=await loadMapLibre();
   if (this.generation!==generation) return;
   this.container.hidden=false;
   if (!this.container.clientWidth || !this.container.clientHeight) return;
   const map=new Map({
    container:this.container,
    style:this.source.styleUrl,
    center:camera.center, zoom:camera.zoom, minZoom:0, maxZoom:24, bearing:0, pitch:0,
    interactive:false, trackResize:false, pixelRatio, attributionControl:{compact:true}, fadeDuration:0,
    transformStyle:(_old,style)=>({...style,projection:{type:'mercator'},terrain:undefined}),
   });
   this.map=map;
   this.lastPixelRatio=pixelRatio;
   let styleLoaded=false;
   map.on('style.load',()=>{styleLoaded=true;});
   const markReady=()=>{
    if (this.map!==map || this.ready || !styleLoaded) return;
    this.ready=true;
    this.failed=false;
    this.container.dataset.ready='true';
    this.redraw();
   };
   map.on('load',markReady);
   map.on('idle',markReady);
   map.on('error',()=>{
    if (this.map!==map) return;
    if (!styleLoaded) {
     this.failed='MapLibre 样式加载失败。';
    } else {
     this.layerError='部分 MapLibre 图层未加载。';
    }
    this.redraw();
   });
   map.on('webglcontextlost',()=>{
    if (this.map!==map) return;
    styleLoaded=false;
    this.ready=false;
    this.container.dataset.ready='false';
    this.failed='MapLibre 的 WebGL 暂不可用。';
    this.redraw();
   });
   map.on('webglcontextrestored',()=>{
    if (this.map!==map) return;
    this.lastSize=null;
    if (map.isStyleLoaded()) {
     styleLoaded=true;
     markReady();
    }
   });
   this.redraw();
  } catch {
   if (this.generation!==generation) return;
   this.container.hidden=true;
   this.failed='MapLibre 或 WebGL 初始化失败。';
   this.redraw();
  } finally {
   if (this.generation===generation) this.pending=false;
  }
 }
}
