// ts-probe: pair 4 of the harness, the frontend SDK (@platform/frontend-sdk, platform-reactcommon)
// against the live go-probe and python-probe http-serve servers. It runs the SDK's own /http and
// /problem code (from the sibling checkout, under Node's undici fetch) and checks:
//   problem-decode  every error scenario, from both servers, parses into the same ProblemDetails
//                   (instance, traceId and timestamp may differ), never synthetic
//   propagation     the server received the SDK's traceparent and x-request-id; its X-Trace-ID
//                   echo is the trace the SDK sent; no identity header arrived, even when set
//   cross-origin    an allowlisted origin gets correlation but no token; any other origin gets
//                   no traceparent, x-request-id, x-session-id or Authorization
// Prints one JSON report and exits 1 when a check fails.
//
//   tsx ts-probe/main.ts --go http://localhost:18081 --py http://localhost:18082 [--sdk <platform-reactcommon>]
import { readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';

const { values } = parseArgs({
  options: { go: { type: 'string' }, py: { type: 'string' }, sdk: { type: 'string' } },
});
const GO = values.go ?? 'http://localhost:18081';
const PY = values.py ?? 'http://localhost:18082';
const SDK = resolve(values.sdk ?? join(dirname(fileURLToPath(import.meta.url)), '../../react/platform-reactcommon'));
const SRC = join(SDK, 'packages/frontend-sdk/src');

// The SDK's build-time constant, set before any SDK module loads.
(globalThis as { __SDK_VERSION__?: string }).__SDK_VERSION__ = (
  JSON.parse(readFileSync(join(SDK, 'packages/frontend-sdk/package.json'), 'utf8')) as { version: string }
).version;
// The "page" is the Go server: same-origin for the SDK.
(globalThis as { location?: URL }).location = new URL(`${GO}/`);

const core = await import(join(SRC, 'core/index.ts'));
const config = await import(join(SRC, 'config/index.ts'));
const http = await import(join(SRC, 'http/index.ts'));
const problem = await import(join(SRC, 'problem/index.ts'));
const tracing = await import(join(SRC, 'tracing/index.ts'));
const tracingInternals = await import(join(SRC, 'tracing/tracing.ts'));

interface Check {
  name: string;
  pass: boolean;
  detail?: unknown;
}
const checks: Check[] = [];
const check = (name: string, pass: boolean, detail?: unknown) => checks.push({ name, pass, ...(pass ? {} : { detail }) });

// A Go and a Python origin that differ only by host spelling: the allowlist names one exactly.
const PY_ALLOWLISTED = new URL(PY).origin;
const PY_OTHER = PY_ALLOWLISTED.replace('localhost', '127.0.0.1');

core.initializeCore({ serviceName: 'ts-probe', domain: 'iam', environment: 'test', buildVersion: '1.0.0' });
config.setRuntimeConfig(
  config.RuntimeConfigSchema.parse({
    api: { baseUrl: '/', propagationAllowlist: [PY_ALLOWLISTED] },
    telemetry: {},
    auth: { idleTimeoutMs: 1_800_000, redirectPath: '/auth/callback' },
    remotes: { loadTimeoutMs: 10_000, retries: 0 },
    i18n: { defaultLocale: 'en', supportedLocales: ['en'] },
    appUpdate: { pollIntervalMs: 300_000 },
  }),
);
tracingInternals.setTracingInternalsForTests({ exporter: () => ({ export: (_s: unknown, cb: (r: { code: number }) => void) => cb({ code: 0 }), shutdown: async () => {} }) });
tracing.initializeTracing({ otlpBaseUrl: '/otel', sampleRatio: 1, propagationAllowlist: [PY_ALLOWLISTED] });
http.setTokenSource({ getAccessToken: async () => 'probe-token', refresh: async () => false, onUnauthorized: () => {} });

// --- problem-decode -----------------------------------------------------------------------
// [name, path, status, prefix]: the scenarios are mounted under /errors, except the unknown route,
// checked at the server root: under a Starlette mount fastapicommon cannot tell a missing route
// from a missing resource and answers NOT_FOUND instead of ROUTE_NOT_FOUND.
const SCENARIOS: [string, string, number, string?][] = [
  ['validation_failed', '/validation', 400],
  ['unauthorized', '/protected', 401],
  ['not_found', '/tenders/123', 404],
  ['route_not_found', '/no-such-route', 404, ''],
  ['conflict_with_extensions', '/conflict', 409],
  ['about_blank', '/gone', 410],
  ['internal_error', '/internal', 500],
  ['server_timeout', '/slow', 503],
];
const normalise = (p: Record<string, unknown>) => {
  const { type, title, status, detail, errorCode, tenantId, validationErrors, extensions, synthetic, typeId } = p;
  return { type, title, status, detail, errorCode, tenantId, validationErrors, extensions: { ...(extensions as object) }, synthetic, typeId };
};
for (const [name, path, status, prefix = '/errors'] of SCENARIOS) {
  const parsed: Record<string, ReturnType<typeof normalise>> = {};
  for (const [lang, base] of [['go', GO], ['python', PY]] as const) {
    const api = http.createApiClient({ baseUrl: `${base}${prefix}` });
    const err = await api.get(path, { retry: false, auth: false }).then(
      () => undefined,
      (e: unknown) => e,
    );
    if (!problem.isProblemError(err)) {
      check(`problem-decode ${name} (${lang})`, false, { got: String(err) });
      continue;
    }
    parsed[lang] = normalise(err as unknown as Record<string, unknown>);
    check(`problem-decode ${name} (${lang}): status ${status}, parsed, not synthetic`, err.status === status && !err.synthetic, parsed[lang]);
  }
  if (parsed.go && parsed.python) {
    const same = JSON.stringify(parsed.go) === JSON.stringify(parsed.python);
    check(`problem-decode ${name}: Go and Python parse to the same ProblemDetails`, same, parsed);
  }
}

// --- propagation --------------------------------------------------------------------------
interface Echo {
  received_trace_id: string;
  x_request_id: string;
  x_session_id: string;
  authorization: boolean;
  identity_headers: string[];
}
for (const [lang, base] of [['go', GO]] as const) {
  const res = await http.createApiClient({ baseUrl: base }).get('/echo', { headers: { 'X-Tenant-ID': 'other-tenant', 'x-plan': 'enterprise' } });
  const e = res.data as Echo;
  check(`propagation (${lang}): server received the SDK's x-request-id`, e.x_request_id === res.requestId && res.requestId !== '', { e, requestId: res.requestId });
  check(`propagation (${lang}): server joined the SDK's trace (X-Trace-ID = traceparent trace id)`, /^[0-9a-f]{32}$/.test(e.received_trace_id) && res.traceId === e.received_trace_id, { e, traceId: res.traceId });
  check(`propagation (${lang}): x-session-id and the token reach the page origin`, e.x_session_id === core.getSessionId() && e.authorization, e);
  check(`propagation (${lang}): no identity header arrived, though the caller set two`, e.identity_headers.length === 0, e);
}

// --- cross-origin -------------------------------------------------------------------------
{
  const res = await http.createApiClient({ baseUrl: PY_ALLOWLISTED }).get('/echo');
  const e = res.data as Echo;
  check('cross-origin: an allowlisted origin gets traceparent and x-request-id', /^[0-9a-f]{32}$/.test(e.received_trace_id) && e.x_request_id === res.requestId, e);
  check('cross-origin: an allowlisted origin never gets the token', !e.authorization, e);
}
{
  const res = await http.createApiClient({ baseUrl: PY_OTHER }).get('/echo');
  const e = res.data as Echo;
  const clean = e.received_trace_id === '' && e.x_request_id === '' && e.x_session_id === '' && !e.authorization;
  check('cross-origin: any other origin gets no traceparent, x-request-id, x-session-id or Authorization', clean, e);
}

await tracing.shutdownTracing();
const failed = checks.filter((c) => !c.pass);
console.log(JSON.stringify({ language: 'ts', sdk: (globalThis as { __SDK_VERSION__?: string }).__SDK_VERSION__, all_pass: failed.length === 0, checks }, null, 2));
process.exit(failed.length === 0 ? 0 : 1);
