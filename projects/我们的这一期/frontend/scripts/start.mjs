import { cpSync, existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const flags = process.argv.slice(2);
const argument = flags.indexOf('--port');
const port = argument >= 0 ? flags[argument + 1] : process.env.PORT || '3000';
if (!port || !/^\d+$/.test(port) || Number(port) < 1024 || Number(port) > 65535) throw new Error('网页端口无效。');
const server = resolve('.next/standalone/server.js');
if (!existsSync(server)) throw new Error('请先运行 npm run build 生成正式网页。');
cpSync(resolve('.next/static'), resolve('.next/standalone/.next/static'), { recursive:true });
if (existsSync('public')) cpSync(resolve('public'), resolve('.next/standalone/public'), { recursive:true });
process.env.HOSTNAME = '127.0.0.1';
process.env.PORT = port;
await import(pathToFileURL(server).href);
