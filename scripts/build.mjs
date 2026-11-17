import { cp, mkdir, rm } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
const root = fileURLToPath(new URL('../', import.meta.url));
await rm(`${root}/dist`, { recursive: true, force: true });
await mkdir(`${root}/dist/vendor`, { recursive: true });
await cp(`${root}/web`, `${root}/dist`, { recursive: true });
await cp(`${root}/node_modules/cesium/Build/Cesium`, `${root}/dist/vendor/cesium`, { recursive: true });
console.log('Built dist/ with locally bundled Cesium. Start: python3 server/app.py --built');
