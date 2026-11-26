import { cp, mkdir, rm } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
const root = fileURLToPath(new URL('../', import.meta.url));
await rm(`${root}/dist`, { recursive: true, force: true });
await mkdir(`${root}/dist/vendor`, { recursive: true });
await cp(`${root}/web`, `${root}/dist`, { recursive: true });
await cp(`${root}/node_modules/cesium/Build/Cesium`, `${root}/dist/vendor/cesium`, { recursive: true });
await mkdir(`${root}/dist/vendor/maplibre`);
const maplibre = `${root}/node_modules/maplibre-gl`;
for (const file of ['maplibre-gl.mjs', 'maplibre-gl-worker.mjs', 'maplibre-gl-shared.mjs', 'maplibre-gl.css']) {
  await cp(`${maplibre}/dist/${file}`, `${root}/dist/vendor/maplibre/${file}`);
}
await cp(`${maplibre}/LICENSE.txt`, `${root}/dist/vendor/maplibre/LICENSE.txt`);
console.log('Built dist/ with Cesium and MapLibre GL JS. Start: python3 server/app.py --built');
