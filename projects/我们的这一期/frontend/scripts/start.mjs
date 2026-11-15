import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync } from 'node:fs';
import { basename, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const flags = process.argv.slice(2);
const argument = flags.indexOf('--port');
const port = argument >= 0 ? flags[argument + 1] : process.env.PORT || '3000';
if (!port || !/^\d+$/.test(port) || Number(port) < 1024 || Number(port) > 65535) throw new Error('网页端口无效。');
const source = resolve('.next/standalone');
if (!existsSync(resolve(source, 'server.js'))) throw new Error('请先运行 npm run build 生成正式网页。');
const buildId = readFileSync(resolve('.next/BUILD_ID'), 'utf8');
// A build replaces .next. Serve a private, complete copy so verification and
// subsequent builds cannot remove chunks/public files from a live process.
const runtimeRoot = resolve('../data/runtime/web-builds');
mkdirSync(runtimeRoot, { recursive: true });
const runtime = mkdtempSync(resolve(runtimeRoot, `web-${port}-`));
const copy = { recursive: true, dereference: true,
  filter: path => basename(path) !== '.env' && !basename(path).startsWith('.env.') };
cpSync(source, runtime, copy);
cpSync(resolve('.next/static'), resolve(runtime, '.next/static'), copy);
if (existsSync('public')) cpSync(resolve('public'), resolve(runtime, 'public'), copy);
if (readFileSync(resolve('.next/BUILD_ID'), 'utf8') !== buildId ||
    readFileSync(resolve(runtime, '.next/BUILD_ID'), 'utf8') !== buildId) {
  throw new Error('构建在启动期间发生变化，请等待构建完成后重新启动网页。');
}
process.env.HOSTNAME = '127.0.0.1';
process.env.PORT = port;
await import(pathToFileURL(resolve(runtime, 'server.js')).href);
