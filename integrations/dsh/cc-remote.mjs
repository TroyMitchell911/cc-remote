/** Bounded authenticated integration endpoints over native DSH services. */
import { createRequire } from 'node:module';
import { isAbsolute } from 'node:path';
export const name = 'cc-remote-metadata';
export const inject = ['connection', 'sessionQuery', 'agents', 'commands', 'agentPresets', 'skills'];
const SID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,123}$/u;
export function apply(ctx) {
  ctx.inject(['schedule'], scoped => {
    scoped.connection.fetch.register({ path: '/api/cc-remote.schedule', methods: ['POST'], requestBody: 'buffered',
      fetch: request => createSchedule(scoped, request) });
  });
  ctx.connection.fetch.register({ path: '/api/cc-remote.catalog', methods: ['GET'], requestBody: 'buffered', fetch: request => catalog(ctx, request) });
  ctx.connection.fetch.register({ path: '/api/cc-remote.metadata', methods: ['GET'], requestBody: 'buffered', fetch: request => metadata(ctx, request) });
}
export async function metadata(ctx, request) {
  const params = new URL(request.url).searchParams;
  const sid = params.get('sessionId'), query = params.get('query');
  if (!SID.test(sid ?? '') || [...params.keys()].some(k => !['sessionId','query'].includes(k) || params.getAll(k).length !== 1) || (query && query.length > 1000)) return Response.json({}, {status:400});
  let observation, lease;
  try {
    observation = await ctx.sessionQuery.observeSession(sid, {signal: request.signal, projectionMode: 'all'});
    if (observation.header.version !== 4) return Response.json({}, {status:409});
    const live = ctx.agents.get(sid);
    if (!live) lease = await ctx.agentPresets.acquireScope(observation.projections?.values?.agentPreset ?? observation.header.agentPreset);
    let matchSeq;
    if (query) {
      const matches = await ctx.sessionQuery.searchEvents({sessionId:sid,query,
        filters:[{kind:'type',values:['user/message','assistant/message']},{kind:'surface',values:['current']}],limit:20});
      matchSeq = matches.items.find(e => e.sessionId === sid && e.surface === 'current')?.seq;
    }
    const body = JSON.stringify({contract:2,version:createRequire(process.argv[1])('@deepseek-ai/dsh/package.json').version,
      sessionId:sid,commands:ctx.commands.list(live ?? lease.key),deliverables:producedFiles(observation.events),matchSeq});
    if (Buffer.byteLength(body) > 1024 * 1024) return Response.json({}, {status:413});
    return new Response(body,{headers:{'content-type':'application/json','cache-control':'no-store'}});
  } catch { return Response.json({}, {status:503}); }
  finally { await lease?.[Symbol.asyncDispose](); observation?.[Symbol.dispose](); }
}
export function producedFiles(events) {
  const calls = new Map(), files = new Map();
  for (const event of events) {
    if (event.surfaceOp && typeof event.surfaceOp === 'object') continue;
    const data = event.data;
    if (event.type === 'tool/call') {
      let args; try {args = JSON.parse(data.arguments);} catch {continue;}
      const path = ['write','edit'].includes(data.name) ? args?.file_path
        : data.name === 'str_replace_editor' && ['create','str_replace','insert'].includes(args?.command) ? args.path : null;
      if (typeof path === 'string' && path.trim() && path.length <= 4096) calls.set(data.callId,path);
    }
    if (event.type === 'tool/result' && !data.error && !data.message?.isError) {
      const path = calls.get(data.message?.toolCallId);
      if (path) files.set(path,{path,label:'创建或修改的文件'});
    }
    if (event.type === 'deliverables/presented') for (const file of data.files ?? []) {
      if (typeof file.path === 'string' && file.path.trim() && file.path.length <= 4096) files.set(file.path,{path:file.path,label:String(file.description ?? '交付文件').slice(0,4096)});
    }
  }
  return [...files.values()].slice(-256).reverse();
}

export async function catalog(ctx, request) {
  const params = new URL(request.url).searchParams;
  const cwd = params.get('cwd'), preset = params.get('preset') || undefined;
  if (!cwd || !isAbsolute(cwd) || cwd.length > 4096 || (preset && preset.length > 256)
    || [...params.keys()].some(k => !['cwd','preset'].includes(k) || params.getAll(k).length !== 1)) return Response.json({}, {status:400});
  let lease;
  try {
    lease = await ctx.agentPresets.acquireScope(preset);
    const require = createRequire(process.argv[1]);
    const { isUserInvocable } = require('@deepseek-ai/dsh-skill');
    const skills = (await ctx.skills.list({cwd, scope: lease.key})).filter(isUserInvocable)
      .slice(0,500).map(({name,description}) => ({name, description}));
    const body = JSON.stringify({contract:2,version:require('@deepseek-ai/dsh/package.json').version,
      skills,commands:ctx.commands.list(lease.key)});
    if (Buffer.byteLength(body) > 1024*1024) return Response.json({}, {status:413});
    return new Response(body,{headers:{'content-type':'application/json','cache-control':'no-store'}});
  } catch { return Response.json({}, {status:503}); }
  finally { await lease?.[Symbol.asyncDispose](); }
}

/** Native Host creation does not activate an Agent or run a model turn. */
export async function createSchedule(ctx, request) {
  let body;
  try {
    const text = await request.text();
    if (Buffer.byteLength(text) > 32768) return Response.json({}, {status:413});
    body = JSON.parse(text);
  } catch { return Response.json({}, {status:400}); }
  const allowed = ['title','prompt','after_seconds','at','every_seconds','daily','weekly','cron'];
  const rule = body?.request;
  if (!SID.test(body?.sessionId ?? '') || !rule || typeof rule !== 'object' || Array.isArray(rule)
    || Object.keys(body).some(k => !['sessionId','request'].includes(k))
    || Object.keys(rule).some(k => !allowed.includes(k))
    || typeof rule.title !== 'string' || !rule.title.trim() || rule.title.length > 120
    || typeof rule.prompt !== 'string' || !rule.prompt.trim() || rule.prompt.length > 24000
    || allowed.slice(2).filter(k => rule[k] !== undefined).length !== 1) return Response.json({}, {status:400});
  try {
    const record = await ctx.schedule.create(body.sessionId, rule, request.signal);
    return Response.json({id:record.id}, {headers:{'cache-control':'no-store'}});
  } catch (error) {
    // Native validation errors are safe to classify; never expose prompts,
    // filesystem details or internal exception strings through this bridge.
    const invalid = error?.name === 'ScheduleError' || error?.name === 'ScheduleInputError';
    return Response.json({}, {status:invalid ? 422 : 503});
  }
}
