/* True WGS84 globe + satellite imagery + decoded public elevation tiles.
 * Google tiles stay in browser memory and are never exported by this app.
 */
const C = window.Cesium;
export class GroundMap {
  constructor(scene, { onSelect, onStatus, onNotice, onPosition }) {
    this.sceneConfig = scene;
    this.callbacks = { onSelect, onStatus, onNotice, onPosition };
    this.aircraft = new Map();
    this.trails = new Map();
    this.zones = [];
    this.missionEntities=[];
    this.missionKey='';
    this.followId = null;
    this.source = 'terrain';
    this.terrainLoaded = 0;
    this.terrainFailures = 0;
    this.imageryLoaded = false;
    this.showTrails = true;
    this.tilePromises = new Map();
    C.Ion.defaultAccessToken = '';
    this.terrain = this.createTerrain();
    this.viewer = new C.Viewer('cesiumContainer', {
      animation:false, timeline:false, baseLayerPicker:false, geocoder:false,
      homeButton:false, sceneModePicker:false, navigationHelpButton:false,
      fullscreenButton:false, infoBox:false, selectionIndicator:false,
      baseLayer:false, terrainProvider:this.terrain,
      creditContainer:document.getElementById('map-credit'),
      requestRenderMode:true, maximumRenderTimeChange:Infinity,
      targetFrameRate:30,
      contextOptions:{webgl:{alpha:false,antialias:true,preserveDrawingBuffer:false}},
      skyBox:false, skyAtmosphere:new C.SkyAtmosphere(),
      shouldAnimate:false,
    });
    const v = this.viewer;
    v.resolutionScale = Math.min(window.devicePixelRatio || 1, 1.5) / (window.devicePixelRatio || 1);
    v.scene.globe.baseColor=C.Color.fromCssColorString('#3e5746');
    v.scene.backgroundColor=C.Color.fromCssColorString('#8fa7a0');
    v.scene.globe.enableLighting=false;
    v.scene.globe.depthTestAgainstTerrain=false;
    v.scene.globe.maximumScreenSpaceError=3.5;
    v.scene.globe.tileCacheSize=150;
    v.scene.globe.showGroundAtmosphere=false;
    v.scene.fog.enabled=false;
    v.scene.screenSpaceCameraController.minimumZoomDistance=80;
    v.scene.screenSpaceCameraController.maximumZoomDistance=25000000;
    v.clock.currentTime=C.JulianDate.fromIso8601('2026-06-15T04:00:00Z');
    const imagery=new C.UrlTemplateImageryProvider({
      url:'/api/tiles/imagery/{z}/{x}/{y}.jpg', maximumLevel:18,
      credit:new C.Credit('Imagery © Esri, Maxar, Earthstar Geographics, and the GIS User Community',true)
    });
    imagery.errorEvent.addEventListener(()=>onNotice('卫星影像加载失败，请检查网络。地图不会用示意图片替代真实影像。'));
    this.imageryLayer=v.imageryLayers.addImageryProvider(imagery);
    this.imageryLayer.brightness=0.9;
    this.imageryLayer.saturation=0.8;
    this.imageryLayer.contrast=1.07;
    this.home(false);
    this.addZones();
    const handler=new C.ScreenSpaceEventHandler(v.scene.canvas);
    handler.setInputAction(click=>{
      const picked=v.scene.pick(click.position);
      if (picked?.id?.properties?.aircraftId) onSelect(picked.id.properties.aircraftId.getValue());
    },C.ScreenSpaceEventType.LEFT_CLICK);
    handler.setInputAction(move=>{
      const ray=v.camera.getPickRay(move.endPosition);
      const point=ray && v.scene.globe.pick(ray,v.scene);
      if (point) {
        const c=C.Cartographic.fromCartesian(point);
        onPosition(C.Math.toDegrees(c.longitude),C.Math.toDegrees(c.latitude));
      }
    },C.ScreenSpaceEventType.MOUSE_MOVE);
    // A user's drag ends following, rather than continuously fighting the camera.
    handler.setInputAction(()=>{ this.followId=null; },C.ScreenSpaceEventType.LEFT_DOWN);
    v.scene.renderError.addEventListener((_,error)=>onNotice('三维渲染异常：'+String(error.message || error).slice(0,120)));
    this.handler=handler;
    this.readyPromise=new Promise(resolve=>{
      let done=false;
      const finish=()=>{
        if(done)return;done=true;
        document.getElementById('map-loading').classList.add('done');
        setTimeout(()=>document.getElementById('map-loading').hidden=true,650);
        resolve();
      };
      v.scene.globe.tileLoadProgressEvent.addEventListener(count=>{
        if(this.terrainLoaded>0 && count===0)finish();
      });
      setTimeout(finish,10000);
    });
  }

  createTerrain() {
    const width=65;
    return new C.CustomHeightmapTerrainProvider({
      width,height:width,tilingScheme:new C.WebMercatorTilingScheme(),
      credit:new C.Credit('<a href="https://registry.opendata.aws/terrain-tiles/" target="_blank">Terrain Tiles / Mapzen</a> · © NASA, USGS, CGIAR, GMTED',true),
      callback:(x,y,level)=>{
        const sourceLevel=Math.min(level,14);
        const factor=2**(level-sourceLevel);
        const tx=Math.floor(x/factor),ty=Math.floor(y/factor);
        const key=`${sourceLevel}/${tx}/${ty}`;
        let promise=this.tilePromises.get(key);
        if(!promise){
          promise=fetch(`/api/tiles/terrain/${key}.png`).then(r=>{
            if(!r.ok)throw new Error('高程数据不可达');return r.blob();
          }).then(blob=>createImageBitmap(blob,{colorSpaceConversion:'none'})).then(bitmap=>{
            const canvas=document.createElement('canvas');canvas.width=bitmap.width;canvas.height=bitmap.height;
            const ctx=canvas.getContext('2d',{willReadFrequently:true});ctx.drawImage(bitmap,0,0);
            const pixels=ctx.getImageData(0,0,canvas.width,canvas.height).data;
            const values=new Float32Array(canvas.width*canvas.height);
            for(let i=0;i<values.length;i++)values[i]=pixels[i*4]*256+pixels[i*4+1]+pixels[i*4+2]/256-32768;
            bitmap.close();
            return {values,size:canvas.width};
          }).catch(error=>{this.tilePromises.delete(key);throw error;});
          this.tilePromises.set(key,promise);
          if(this.tilePromises.size>160)this.tilePromises.delete(this.tilePromises.keys().next().value);
        }
        return promise.then(({values,size})=>{
          const heights=new Float32Array(width*width);
          const ox=(x%factor)/factor,oy=(y%factor)/factor;
          for(let row=0;row<width;row++)for(let col=0;col<width;col++){
            const px=Math.min(size-1,(ox+col/(width-1)/factor)*(size-1));
            const py=Math.min(size-1,(oy+row/(width-1)/factor)*(size-1));
            const x0=Math.floor(px),y0=Math.floor(py),x1=Math.min(size-1,x0+1),y1=Math.min(size-1,y0+1);
            const fx=px-x0,fy=py-y0;
            heights[row*width+col]=(values[y0*size+x0]*(1-fx)+values[y0*size+x1]*fx)*(1-fy)+(values[y1*size+x0]*(1-fx)+values[y1*size+x1]*fx)*fy;
          }
          this.terrainLoaded++;
          this.highestTerrainLevel=Math.max(this.highestTerrainLevel||0,level);
          if(this.source==='terrain')this.callbacks.onStatus(this.terrainFailures?'部分高程加载失败':this.highestTerrainLevel>=9?'真实高程已加载':'地形细化中',this.terrainFailures===0&&this.highestTerrainLevel>=9);
          return heights;
        }).catch(()=>{
          this.terrainFailures++;
          this.callbacks.onStatus('高程加载失败',false);
          this.callbacks.onNotice('部分高程瓦片加载失败。对应区域临时显示平面，请检查网络后刷新。');
          return new Float32Array(width*width);
        });
      }
    });
  }

  addZones(){
    for(const zone of this.sceneConfig.zones){
      const color=C.Color.fromCssColorString(zone.color);
      const coords=zone.coordinates.flat();
      const closed=[...coords,...zone.coordinates[0]];
      const polygon=this.viewer.entities.add({id:'zone-'+zone.id,polygon:{hierarchy:C.Cartesian3.fromDegreesArray(coords),material:color.withAlpha(.10),heightReference:C.HeightReference.CLAMP_TO_GROUND,classificationType:C.ClassificationType.TERRAIN},polyline:{positions:C.Cartesian3.fromDegreesArray(closed),clampToGround:true,width:2,material:color.withAlpha(.8)}});
      const label=this.viewer.entities.add({id:'zone-label-'+zone.id,position:C.Cartesian3.fromDegrees(zone.coordinates[0][0],zone.coordinates[0][1]),label:{text:zone.name,font:'12px FieldLinkSC',fillColor:color,showBackground:true,backgroundColor:C.Color.fromCssColorString('#15221dcc'),backgroundPadding:new C.Cartesian2(10,7),heightReference:C.HeightReference.CLAMP_TO_GROUND,disableDepthTestDistance:Infinity,pixelOffset:new C.Cartesian2(0,-13),distanceDisplayCondition:new C.DistanceDisplayCondition(0,180000)}});
      this.zones.push(polygon,label);
    }
  }

  planeIcon(color){
    const svg=`<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64"><circle cx="32" cy="32" r="25" fill="#13221c" fill-opacity=".78" stroke="${color}" stroke-opacity=".35"/><path d="M32 10 36 29 52 39v5l-17-6v11l6 4v2l-9-2-9 2v-2l6-4V38l-17 6v-5l16-10Z" fill="${color}" stroke="#15221c" stroke-width="1.2"/></svg>`;
    return 'data:image/svg+xml;charset=utf-8,'+encodeURIComponent(svg);
  }

  updateMission(report){
    const key=report?JSON.stringify([report.allocation?.assignments,report.tasks,Object.values(report.aircraft||{}).map(a=>Object.keys(a.visited||{}))]):'';
    if(key===this.missionKey)return;
    this.missionKey=key;
    this.missionEntities.forEach(e=>this.viewer.entities.remove(e));this.missionEntities=[];
    if(!report?.allocation)return;
    Object.entries(report.allocation.assignments).forEach(([id,route],index)=>{
      const color=C.Color.fromCssColorString(['#99cfb0','#e2b777','#9dbbe0'][index%3]);
      const positions=[];
      route.forEach(taskId=>{
        const task=report.tasks.find(t=>t.id===taskId);if(!task)return;
        const lat=report.origin.lat+task.north_m/6371000*180/Math.PI;
        const lon=report.origin.lon+task.east_m/(6371000*Math.cos(report.origin.lat*Math.PI/180))*180/Math.PI;
        const altitude=report.aircraft[id]?.mission?.find(m=>m.task_id===taskId)?.alt??120;
        const position=C.Cartesian3.fromDegrees(lon,lat,report.origin.altitude_m+altitude);
        positions.push(position);
        const visited=Boolean(report.aircraft[id]?.visited?.[taskId]);
        this.missionEntities.push(this.viewer.entities.add({position,point:{pixelSize:8,color:visited?color:C.Color.WHITE,outlineColor:color,outlineWidth:2,disableDepthTestDistance:Infinity},label:{text:taskId+(visited?' ✓':''),font:'10px monospace',fillColor:color,pixelOffset:new C.Cartesian2(0,-18),showBackground:true,backgroundColor:C.Color.fromCssColorString('#14221be6'),disableDepthTestDistance:Infinity}}));
      });
      if(positions.length>1)this.missionEntities.push(this.viewer.entities.add({polyline:{positions,width:2,material:new C.PolylineDashMaterialProperty({color:color.withAlpha(.6),dashLength:10})}}));
    });
  }

  updateAircraft(aircraft,selectedId){
    const current=new Set(aircraft.map(a=>a.id));
    for(const [id,item] of this.aircraft){if(!current.has(id)){this.viewer.entities.remove(item.entity);this.viewer.entities.remove(item.trailEntity);this.aircraft.delete(id);this.trails.delete(id);}}
    aircraft.forEach((a,index)=>{
      let item=this.aircraft.get(a.id);
      const color=a.color || ['#99cfb0','#e2b777','#9dbbe0'][index%3];
      const position=C.Cartesian3.fromDegrees(a.longitude,a.latitude,a.altitude_m);
      if(!item){
        const trail=[];
        if(a.orbit){
          for(let i=0;i<=100;i++){
            const angle=i/100*2*Math.PI;
            trail.push(C.Cartesian3.fromDegrees(a.orbit.longitude+a.orbit.radius_m*Math.cos(angle)/(111320*Math.cos(C.Math.toRadians(a.orbit.latitude))),a.orbit.latitude+a.orbit.radius_m*Math.sin(angle)/111320,a.altitude_m));
          }
        }
        this.trails.set(a.id,trail);
        const entity=this.viewer.entities.add({id:a.id,position,properties:{aircraftId:a.id},billboard:{image:this.planeIcon(color),width:42,height:42,rotation:C.Math.toRadians(-a.heading_deg),disableDepthTestDistance:Infinity},label:{text:a.id,font:'11px monospace',fillColor:C.Color.fromCssColorString(color),showBackground:true,backgroundColor:C.Color.fromCssColorString('#14221bd9'),backgroundPadding:new C.Cartesian2(6,4),pixelOffset:new C.Cartesian2(0,32),disableDepthTestDistance:Infinity}});
        const trailEntity=this.viewer.entities.add({id:'trail-'+a.id,show:this.showTrails,polyline:{positions:trail.slice(),width:1.6,material:new C.PolylineDashMaterialProperty({color:C.Color.fromCssColorString(color).withAlpha(.7),dashLength:14})}});
        item={entity,trailEntity,data:a};this.aircraft.set(a.id,item);
      }
      if(item.data.session_id!==a.session_id){this.trails.set(a.id,[]);item.trailEntity.polyline.positions=[];}
      item.data=a;
      item.entity.position=position;
      item.entity.billboard.rotation=C.Math.toRadians(-a.heading_deg);
      item.entity.billboard.scale=a.id===selectedId?1.17:1;
      item.entity.billboard.color=a.stale?C.Color.GRAY:C.Color.WHITE;
      item.entity.label.text=a.id+(a.stale?' · 过期':'');
      if(!a.orbit){const trail=this.trails.get(a.id);if(!trail.length||!C.Cartesian3.equals(trail.at(-1),position)){trail.push(position);if(trail.length>600)trail.shift();item.trailEntity.polyline.positions=trail.slice();}}
    });
    if(this.followId){
      const item=this.aircraft.get(this.followId);
      if(item){const p=item.entity.position.getValue(this.viewer.clock.currentTime);this.viewer.camera.lookAt(p,new C.HeadingPitchRange(0,-.55,8000));this.viewer.camera.lookAtTransform(C.Matrix4.IDENTITY);}
    }
    this.viewer.scene.requestRender();
  }

  home(animate=true){
    this.followId=null;
    const {longitude,latitude}=this.sceneConfig.center;
    const target=C.Cartesian3.fromDegrees(longitude,latitude,1400);
    if(animate)this.viewer.camera.flyToBoundingSphere(new C.BoundingSphere(target,1),{duration:1.6,offset:new C.HeadingPitchRange(0,-.75,21000)});
    else{this.viewer.camera.lookAt(target,new C.HeadingPitchRange(0,-.75,21000));this.viewer.camera.lookAtTransform(C.Matrix4.IDENTITY);}
  }
  flyTo(lon,lat,height=15000,animate=true){
    this.followId=null;
    const options={destination:C.Cartesian3.fromDegrees(lon,lat-.072,height),orientation:{heading:0,pitch:C.Math.toRadians(-53),roll:0},duration:animate?1.6:0};
    if(animate)this.viewer.camera.flyTo(options);else this.viewer.camera.setView(options);
  }
  focus(id){
    const item=this.aircraft.get(id);if(!item)return;
    this.viewer.camera.flyToBoundingSphere(new C.BoundingSphere(C.Cartesian3.fromDegrees(item.data.longitude,item.data.latitude,item.data.altitude_m),400),{duration:1.2,offset:new C.HeadingPitchRange(0,-.65,8500)});
  }
  follow(id){this.followId=this.followId===id?null:id;return !!this.followId;}
  zoom(direction){this.followId=null;const step=Math.max(100,this.viewer.camera.positionCartographic.height*.28);if(direction>0)this.viewer.camera.zoomIn(step);else this.viewer.camera.zoomOut(step);this.viewer.scene.requestRender();}
  north(){this.viewer.camera.setView({orientation:{heading:0,pitch:this.viewer.camera.pitch,roll:0}});this.viewer.scene.requestRender();}
  tilt(){this.topdown=!this.topdown;const {longitude,latitude}=this.sceneConfig.center;this.viewer.camera.flyTo({destination:C.Cartesian3.fromDegrees(longitude,latitude-(this.topdown?0:.072),18500),orientation:{heading:0,pitch:C.Math.toRadians(this.topdown?-90:-53),roll:0},duration:1.2});return !this.topdown;}
  setLayers(zones,trails){this.zones.forEach(e=>e.show=zones);this.showTrails=trails;this.aircraft.forEach(a=>a.trailEntity.show=trails);this.viewer.scene.requestRender();}

  async setSource(source,key=''){
    if(source===this.source)return;
    if(source==='google'){
      if(!key.trim())throw new Error('请填写你自己的 Google Map Tiles API Key。');
      let tileset;
      try{
        tileset=await C.createGooglePhotorealistic3DTileset(key.trim(),{maximumScreenSpaceError:12,showCreditsOnScreen:true,cacheBytes:268435456,maximumCacheOverflowBytes:134217728});
      }catch{throw new Error('Google 3D Tiles 未连接。请检查 Key、Map Tiles API、计费和网络；已保留原地图。');}
      this.googleTiles=this.viewer.scene.primitives.add(tileset);
      this.imageryLayer.show=false;
      this.viewer.scene.globe.show=false;
      this.source='google';
      tileset.tileFailed.addEventListener(()=>this.callbacks.onNotice('Google 三维瓦片加载失败，请检查覆盖、配额或网络。'));
      this.callbacks.onStatus('Google 实景三维',true);
    }else{
      if(this.googleTiles){this.viewer.scene.primitives.remove(this.googleTiles);this.googleTiles=null;}
      this.viewer.scene.globe.show=true;this.imageryLayer.show=true;
      this.source='terrain';
      this.callbacks.onStatus(this.terrainFailures?'部分高程加载失败':this.terrainLoaded?'真实高程已加载':'地形加载中',this.terrainLoaded>0&&!this.terrainFailures);
    }
    this.viewer.scene.requestRender();
  }
}
