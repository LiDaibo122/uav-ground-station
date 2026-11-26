export const earthRadius = 6378137;
const eccentricitySquared = 0.00669437999014;
export const circumference = 2 * Math.PI * earthRadius;

export function mapSources(config) {
 const osmCredit = ['© OpenStreetMap contributors', 'https://www.openstreetmap.org/copyright'];
 const mapTilerCredit = ['© MapTiler', 'https://www.maptiler.com/copyright/'];
 const key = encodeURIComponent(config.maptiler_api_key || '');
 const mapTiler = (name, style, ext) => ({
  name, url: key ? `https://api.maptiler.com/maps/${style}/256/{z}/{x}/{y}.${ext}?key=${key}` : null,
  credits: [mapTilerCredit, osmCredit], logo: 'https://api.maptiler.com/resources/logo.svg', minZoom: 0, maxZoom: 19,
 });
 const providers = {
  maplibre: {name:config.maplibre_style_url ? 'MapLibre · 自定义样式' : 'MapLibre · OpenFreeMap',
   styleUrl:config.maplibre_style_url || 'https://tiles.openfreemap.org/styles/liberty', credits:[]},
  osm: {name:'OpenStreetMap 道路', url:'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
   credits:[osmCredit], minZoom:0, maxZoom:19},
  'maptiler-streets':mapTiler('MapTiler 道路','streets-v4','png'),
  'maptiler-satellite':mapTiler('MapTiler 卫星','satellite-v4','jpg'),
 };
 const item = config.custom_xyz;
 if (item) providers.custom = {name:item.name, url:item.url, credits:[[item.attribution,item.attribution_url]],
  minZoom:item.min_zoom, maxZoom:item.max_zoom};
 return providers;
}

export function mercatorFrame(datum) {
 const phi = datum.lat * Math.PI / 180;
 const lambda = datum.lon * Math.PI / 180;
 const cos = Math.cos(phi);
 const q = 1 - eccentricitySquared * Math.sin(phi) ** 2;
 // Match the Web Mercator grid to East/North metres in the local tangent plane.
 const eastScale = (earthRadius / Math.sqrt(q) + datum.altitude_m) * cos / earthRadius;
 const northScale = (earthRadius * (1-eccentricitySquared) / q ** 1.5 + datum.altitude_m) * cos / earthRadius;
 const originX = earthRadius * lambda;
 const originY = earthRadius * Math.asinh(Math.tan(phi));
 return {eastScale,northScale,originX,originY};
}

export function visibleTiles(view, size, datum, source) {
 const {eastScale,northScale,originX,originY}=mercatorFrame(datum);
 const dx = size.width / (2 * view.scale), dy = size.height / (2 * view.scale);
 if (![datum.lat,datum.lon,datum.altitude_m,view.x,view.y,dx,dy].every(Number.isFinite)
     || Math.abs(datum.lat)>85 || view.scale<=0) return [];
 const resolution = view.scale * circumference * Math.min(eastScale,northScale) / 256;
 let z = Math.max(source.minZoom,Math.min(source.maxZoom,Math.round(Math.log2(resolution))));
 let count, side, left, right, top, bottom;
 while (true) {
  count = 2 ** z;
  side = circumference / count;
  const column = east => (originX + east/eastScale + circumference/2)/side;
  const row = north => (circumference/2 - originY - north/northScale)/side;
  left = Math.floor(column(view.x-dx));
  right = Math.ceil(column(view.x+dx));
  top = Math.max(0,Math.floor(row(view.y+dy)));
  bottom = Math.min(count,Math.ceil(row(view.y-dy)));
  if (left>=right || top>=bottom) return [];
  if ((right-left)*(bottom-top)<=64) break;
  if (z===source.minZoom) return [];
  z--;
 }
 const tiles = [];
 for (let y=top; y<bottom; y++) {
  for (let x=left; x<right; x++) {
   const tileX = (x % count + count) % count;
   tiles.push({id:`${z}/${tileX}/${y}`,
    url:source.url.replaceAll('{z}',z).replaceAll('{x}',tileX).replaceAll('{y}',y),
    west:(x*side-circumference/2-originX)*eastScale,
    north:(circumference/2-y*side-originY)*northScale,
    width:side*eastScale, height:side*northScale});
  }
 }
 return tiles;
}

export class RasterBasemap {
 constructor(redraw, report) {
  this.redraw=redraw;
  this.report=report;
  this.cache=new Map();
 }

 select(source, datum) {
  if (source!==this.source) this.cache.clear();
  this.source=source;
  this.datum=datum;
 }

 draw(ctx, view, size, screen) {
  if (!this.source || !this.datum) return false;
  const tiles=visibleTiles(view,size,this.datum,this.source);
  const counts={ready:0,error:0,pending:0};
  for (const tile of tiles) {
   let record=this.cache.get(tile.id);
   if (!record) {
    const image=new Image();
    image.crossOrigin='anonymous';
    record={image,status:'pending'};
    const cache=this.cache;
    const finish=status=>{
     if (cache.get(tile.id)!==record) return;
     record.status=status;
     this.redraw();
    };
    image.onload=()=>finish('ready');
    image.onerror=()=>finish('error');
    this.cache.set(tile.id,record);
    image.src=tile.url;
   }
   counts[record.status]++;
   if (record.status==='ready') {
    const top=screen({x:tile.west,y:tile.north});
    const bottom=screen({x:tile.west+tile.width,y:tile.north-tile.height});
    ctx.drawImage(record.image,Math.floor(top.x),Math.floor(top.y),
     Math.ceil(bottom.x)-Math.floor(top.x),Math.ceil(bottom.y)-Math.floor(top.y));
   }
  }
  if (this.cache.size>128) {
   const visible=new Set(tiles.map(tile=>tile.id));
   for (const id of this.cache.keys()) {
    if (this.cache.size<=128) break;
    if (!visible.has(id)) this.cache.delete(id);
   }
  }
  if (!tiles.length) this.report('已超出在线底图范围，请点击地图适配。');
  else if (counts.pending) this.report('正在加载在线底图…');
  else if (counts.error) this.report(counts.ready ?
   '部分在线地图未加载，请检查网络或 API Key。' :
   '在线底图加载失败，当前显示离线网格。请检查网络或 API Key。');
  else this.report('');
  return counts.ready>0;
 }
}
