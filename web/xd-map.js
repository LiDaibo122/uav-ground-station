import {RasterBasemap} from './xd-basemap.js';
import {VectorBasemap} from './xd-maplibre.js';

export const colors = ['#176388', '#17816a', '#a54667', '#6856ad', '#b05f1d', '#526e37'];
const finitePoint = p => p && Number.isFinite(p.x) && Number.isFinite(p.y);

function ecef(lon, lat, altitude) {
 const [l, b] = [lon, lat].map(n => n * Math.PI / 180);
 const e2 = 0.00669437999014;
 const r = 6378137 / Math.sqrt(1 - e2 * Math.sin(b) ** 2);
 return [(r + altitude) * Math.cos(b) * Math.cos(l), (r + altitude) * Math.cos(b) * Math.sin(l),
  (r * (1 - e2) + altitude) * Math.sin(b)];
}

export function loadLayer(data, datum) {
 const lines = [];
 let count = 0;
 const origin = datum && ecef(datum.lon, datum.lat, datum.altitude_m);
 const lon = datum?.lon * Math.PI / 180;
 const lat = datum?.lat * Math.PI / 180;
 function project(coord) {
  if (++count > 30000) throw new Error('底图超过 30,000 个坐标，请先简化图形。');
  if (!Array.isArray(coord) || !coord.slice(0,2).every(Number.isFinite) || coord.length < 2) throw new Error('GeoJSON 坐标无效。');
  const [x,y] = coord;
  if (!datum) return {x,y};
  if (Math.abs(x) > 180 || Math.abs(y) > 90) throw new Error('WGS84 坐标需要经度、纬度顺序。');
  const [a,b,c] = ecef(x,y,datum.altitude_m).map((n,i) => n-origin[i]);
  return {x: -Math.sin(lon)*a+Math.cos(lon)*b,
   y: -Math.sin(lat)*(Math.cos(lon)*a+Math.sin(lon)*b)+Math.cos(lat)*c};
 }
 function add(shape) {
  if (!shape) return;
  const c = shape.coordinates;
  switch (shape.type) {
   case 'FeatureCollection': shape.features.forEach(add); break;
   case 'Feature': add(shape.geometry); break;
   case 'GeometryCollection': shape.geometries.forEach(add); break;
   case 'Point': lines.push([project(c)]); break;
   case 'MultiPoint': c.forEach(p => lines.push([project(p)])); break;
   case 'LineString': lines.push(c.map(project)); break;
   case 'MultiLineString':
   case 'Polygon': c.forEach(line => lines.push(line.map(project))); break;
   case 'MultiPolygon': c.forEach(poly => poly.forEach(line => lines.push(line.map(project)))); break;
   default: throw new Error('请选择有效的 GeoJSON 图层。');
  }
 }
 add(data);
 if (!count) throw new Error('底图中没有可显示的几何。');
 return lines;
}

export class FlightMap {
 constructor(canvas, coordinate, selectVehicle, reportBasemap, reportFallback) {
  this.canvas = canvas;
  this.ctx = canvas.getContext('2d');
  this.data = {vehicles: [], trails: {}, targets: []};
  this.view = {x: 350, y: 160, scale: .6};
  this.background = [];
  const redraw = () => {
   if (this.redrawScheduled) return;
   this.redrawScheduled = true;
   requestAnimationFrame(() => {
    this.redrawScheduled = false;
    this.draw();
   });
  };
  this.reportBasemap=reportBasemap;
  this.reportFallback=reportFallback;
  this.usingFallback=false;
  this.basemap = new RasterBasemap(redraw,status=>{this.rasterStatus=status;});
  this.vector = new VectorBasemap(document.getElementById('vector-map'),redraw,status=>{this.vectorStatus=status;});
  this.auto = true;
  this.hits = [];
  new ResizeObserver(() => this.auto ? this.fit(false) : redraw()).observe(canvas);
  window.addEventListener('resize',redraw);
  window.visualViewport?.addEventListener('resize',redraw);
  document.fonts.ready.then(() => this.draw());
  canvas.addEventListener('wheel', e => {
   e.preventDefault();
   this.zoom(Math.exp(-e.deltaY * .002), e.offsetX, e.offsetY);
  }, {passive: false});
  let drag;
  canvas.addEventListener('pointerdown', e => {
   if (!e.isPrimary || ![0,1].includes(e.button)) return;
   if (e.button===1) e.preventDefault();
   if (this.sketchMode) this.auto=false;
   drag = {mode:e.button===0 ? this.sketchMode : '', button:e.button,
    x:e.offsetX, y:e.offsetY, cx:this.view.x, cy:this.view.y, moved:false};
   canvas.setPointerCapture(e.pointerId);
  });
  canvas.addEventListener('pointermove', e => {
   const p = this.world(e.offsetX, e.offsetY);
   coordinate.textContent = `E ${Math.round(p.x)}  N ${Math.round(p.y)}`;
   if (!e.isPrimary || !drag) return;
   const [dx,dy] = [e.offsetX-drag.x, e.offsetY-drag.y];
   if (!drag.moved && Math.hypot(dx,dy)<4) return;
   drag.moved = true;
   this.auto = false;
   if (drag.mode) {
    if (['area','restrict'].includes(drag.mode)) {
     this.rubber = {a:this.world(drag.x,drag.y),b:p};
     this.draw();
    }
    return;
   }
   this.view.x = drag.cx-dx/this.view.scale;
   this.view.y = drag.cy+dy/(this.view.scale*this.vector.northRatio);
   this.draw();
  });
  canvas.addEventListener('pointerup', e => {
   if (!e.isPrimary) return;
   if (drag?.mode && drag.mode===this.sketchMode) {
    const a=this.world(drag.x,drag.y), b=this.world(e.offsetX,e.offsetY);
    if (['area','restrict'].includes(drag.mode) && drag.moved) this.onSketch?.(drag.mode,a,b);
    if (!['area','restrict'].includes(drag.mode) && !drag.moved) this.onSketch?.(drag.mode,b);
   } else if (drag && drag.button===0 && !drag.mode && !drag.moved &&
              !this.onSelectSketch?.(e.offsetX,e.offsetY)) {
    const hit = this.hits.find(p => Math.hypot(e.offsetX-p.x,e.offsetY-p.y)<20);
    if (hit) selectVehicle(hit.id);
    else {
     const point=(this.sketches||[]).find(t=>{
      if (!t.incident) return false;
      const p=this.screen({x:t.east,y:t.north});
      return Math.hypot(e.offsetX-p.x,e.offsetY-p.y)<15;
     });
     if(point)this.onIncident?.(point.id);
    }
   }
   drag = null; this.rubber=null; this.draw();
  });
  canvas.addEventListener('auxclick', e => {if(e.button===1)e.preventDefault();});
  canvas.addEventListener('pointercancel', () => {drag = null;this.rubber=null;this.draw();});
 }
 setData(data) { this.data=data; if (this.auto) this.fit(false); else this.draw(); }
 setLayer(lines) { this.background=lines; this.draw(); }
 setBasemap(source, datum, fallback) {
  this.basemap.select(source?.url ? source : source?.styleUrl ? fallback : null,datum);
  this.vector.select(source?.styleUrl ? source : null,datum);
  this.draw();
 }
 fit(follow = true) {
  this.auto = follow || this.auto;
  const pts = [...this.data.vehicles.map(v => v.world_position_m), ...this.data.targets,
   ...Object.values(this.data.trails).flat(),
   ...(this.sketches||[]).flatMap(t=>t.kind==='area' ?
    [{x:t.west,y:t.south},{x:t.east,y:t.north}] : [{x:t.east,y:t.north}])].filter(finitePoint);
  if (pts.length && this.canvas.clientWidth && this.canvas.clientHeight) {
   const bounds = pts.reduce((b,p) => [Math.min(b[0],p.x), Math.min(b[1],p.y), Math.max(b[2],p.x), Math.max(b[3],p.y)],
    [Infinity,Infinity,-Infinity,-Infinity]);
   const [left,bottom,right,top]=bounds;
   this.view = {x:(left+right)/2, y:(top+bottom)/2,
    scale:Math.max(.015, Math.min(3, (this.canvas.clientWidth-85)/Math.max(500,right-left),
     (this.canvas.clientHeight-97)/Math.max(260,top-bottom)))};
  }
  this.draw();
 }
 zoom(factor, x=this.canvas.clientWidth/2, y=this.canvas.clientHeight/2) {
  this.auto = false;
  const anchor=this.world(x,y);
  this.view.scale=Math.min(20,Math.max(.015,this.view.scale*factor));
  const now=this.world(x,y);
  this.view.x += anchor.x-now.x;
  this.view.y += anchor.y-now.y;
  this.draw();
 }
 world(x,y) {
  return {x:this.view.x+(x-this.canvas.clientWidth/2)/this.view.scale,
   y:this.view.y-(y-this.canvas.clientHeight/2)/(this.view.scale*this.vector.northRatio)};
 }
 screen(p) {
  return {x:this.canvas.clientWidth/2+(p.x-this.view.x)*this.view.scale,
   y:this.canvas.clientHeight/2-(p.y-this.view.y)*this.view.scale*this.vector.northRatio};
 }
 draw() {
  const {canvas,ctx}=this;
  const [w,h]=[canvas.clientWidth,canvas.clientHeight];
  if (!w || !h) return;
  const pixel=Math.min(devicePixelRatio||1, 2);
  if (canvas.width!==Math.round(w*pixel) || canvas.height!==Math.round(h*pixel)) {
   canvas.width=Math.round(w*pixel);
   canvas.height=Math.round(h*pixel);
  }
  ctx.setTransform(pixel,0,0,pixel,0,0);
  const size={width:w,height:h};
  this.vectorStatus='';
  this.rasterStatus='';
  const hasVector=this.vector.update(this.view,size,pixel);
  const vectorFailed=Boolean(this.vector.source && this.vector.failed);
  ctx.clearRect(0,0,w,h);
  if (!hasVector) {
   ctx.fillStyle='#e9efec';
   ctx.fillRect(0,0,w,h);
  }
  const hasRaster=(!this.vector.source || vectorFailed) &&
   this.basemap.draw(ctx,this.view,size,point=>this.screen(point));
  const hasFallback=Boolean(vectorFailed && hasRaster);
  if (hasFallback!==this.usingFallback) {
   this.usingFallback=hasFallback;
   this.reportFallback(hasFallback);
  }
  if (this.vector.source) {
   this.reportBasemap(vectorFailed
    ? this.vectorStatus + (hasFallback ? '已切换 OpenStreetMap 在线道路底图。' : this.rasterStatus)
    : this.vectorStatus);
  } else if (this.basemap.source) {
   this.reportBasemap(this.rasterStatus);
  }
  const hasTiles=hasVector || hasRaster;
  const need=64/this.view.scale;
  const base=10**Math.floor(Math.log10(need));
  const step=([1,2,5,10].find(n=>n*base>=need)||10)*base;
  const bl=this.world(0,h),tr=this.world(w,0);
  ctx.font='11px system-ui, FieldLinkSC';ctx.textBaseline='alphabetic';
  ctx.save();
  if (hasTiles) ctx.globalAlpha=.24;
  for (let x=Math.ceil(bl.x/step)*step;x<=tr.x;x+=step) {
   const p=this.screen({x,y:0}).x;
   ctx.strokeStyle=x===0?'#a8bbb2':'#d3ded8';ctx.beginPath();ctx.moveTo(p,0);ctx.lineTo(p,h);ctx.stroke();
   ctx.fillStyle='#516961';ctx.fillText(String(Math.round(x)),p+4,h-40);
  }
  for (let y=Math.ceil(bl.y/step)*step;y<=tr.y;y+=step) {
   const p=this.screen({x:0,y}).y;
   ctx.strokeStyle=y===0?'#a8bbb2':'#d3ded8';ctx.beginPath();ctx.moveTo(0,p);ctx.lineTo(w,p);ctx.stroke();
   ctx.fillStyle='#516961';ctx.fillText(String(Math.round(y)),8,p-4);
  }
  ctx.restore();
  const drawLine=(line,color,width)=>{
   ctx.strokeStyle=color;ctx.lineWidth=width;ctx.beginPath();
   line.forEach((pt,i)=>{const p=this.screen(pt); if (!i||pt.break_before) ctx.moveTo(p.x,p.y); else ctx.lineTo(p.x,p.y);});
   ctx.stroke();ctx.lineWidth=1;
  };
  for (const line of this.background) {
   drawLine(line,'#849c90',1.4);
   if (line.length===1) {const p=this.screen(line[0]);ctx.fillStyle='#658777';ctx.fillRect(p.x-2,p.y-2,4,4);}
  }
  this.data.vehicles.forEach((v,i)=>{
   const trail=this.data.trails[v.vehicle] || [];
   if (hasTiles) drawLine(trail,'#ffffffe6',4);
   drawLine(trail,colors[i%colors.length],2);
  });
  ctx.fillStyle='#304842';ctx.font='bold 12px system-ui, FieldLinkSC';ctx.fillText('▲ N',12,18);
  function label(text,x,y,ink) {
   ctx.font='600 12px system-ui, FieldLinkSC, sans-serif';
   ctx.lineJoin='round';ctx.lineWidth=3;ctx.strokeStyle='#f6fbf8';ctx.strokeText(text,x,y);
   ctx.fillStyle=ink;ctx.fillText(text,x,y);ctx.lineWidth=1;
  }
  for (const t of this.data.targets) {
   const p=this.screen(t);
   ctx.beginPath();ctx.moveTo(p.x,p.y-6);ctx.lineTo(p.x+6,p.y);ctx.lineTo(p.x,p.y+6);ctx.lineTo(p.x-6,p.y);ctx.closePath();
   ctx.fillStyle='#fcf3db';ctx.fill();ctx.strokeStyle='#925114';ctx.lineWidth=1.8;ctx.stroke();
   label(t.label,p.x+9,p.y-7,'#6d400f');
  }
  for (const t of this.sketches || []) {
   const rect=t.kind==='area';
   const a=this.screen({x:rect?t.west:t.east,y:t.north});
   ctx.strokeStyle=t.selected?'#086659':t.zone?'#b14444':t.incident?'#984213':'#674299';ctx.lineWidth=t.selected?3:2;
   if (rect) {
    const b=this.screen({x:t.east,y:t.south});
    ctx.fillStyle=t.zone?'#c3454530':'#774a9124';ctx.fillRect(a.x,a.y,b.x-a.x,b.y-a.y);
    ctx.strokeRect(a.x,a.y,b.x-a.x,b.y-a.y);
   } else {
    ctx.beginPath();ctx.arc(a.x,a.y,6,0,Math.PI*2);ctx.stroke();
   }
   label(t.label,a.x+8,a.y+12,ctx.strokeStyle);
  }
  if (this.rubber) {
   const a=this.screen(this.rubber.a),b=this.screen(this.rubber.b);
   ctx.strokeStyle='#674299';ctx.strokeRect(a.x,a.y,b.x-a.x,b.y-a.y);
  }
  this.hits=[];
  this.data.vehicles.forEach((v,i)=>{
   if (!finitePoint(v.world_position_m)) return;
   const p=this.screen(v.world_position_m),color=colors[i%colors.length];
   ctx.save();ctx.translate(p.x,p.y);
   ctx.rotate(Number.isFinite(v.world_heading_rad)?-v.world_heading_rad:0);
   ctx.beginPath();ctx.moveTo(10,0);ctx.lineTo(-7,6);ctx.lineTo(-4,0);ctx.lineTo(-7,-6);ctx.closePath();
   ctx.fillStyle=v.connected?color:'#61746c';ctx.fill();ctx.strokeStyle='#fff';ctx.lineWidth=1.25;ctx.stroke();ctx.restore();
   label(v.vehicle,p.x+10,p.y+16,v.connected?color:'#465b53');
   this.hits.push({...p,id:v.vehicle});
  });
 }
}
