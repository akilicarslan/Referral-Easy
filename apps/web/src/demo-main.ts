import './styles/tokens.css';
import './styles/base.css';
import './styles/workspace.css';
import './styles/gallery.css';

import { SurfaceRuntime, mountSurface, type SurfaceMount } from './a2ui/adapter.js';
import { DEMO_SCENES, allScenesUpdate } from './fixtures/surfaces.js';
import type { sse$SurfaceUpdate } from '@referraleasy/contracts';

/**
 * The demo page.
 *
 * Renders every one of the eight components from a static `SurfaceUpdate`
 * fixture, with no backend running and no network access needed.
 *
 * The layout is deliberately a *gallery* rather than a replica of the app: each
 * scene is labelled with what it is demonstrating, because several of these
 * scenes exist to show a negative — a requested booking that must not look
 * confirmed, a coverage value that is unknown rather than "no". Those are only
 * reviewable if the reviewer is told what to look for.
 *
 * Every scene gets its own surface id so the components cannot interfere with
 * one another, and each is mounted through the same adapter the real app uses —
 * so what is on screen here is what the app renders, not a second implementation.
 */

const runtime = new SurfaceRuntime({
  onAction: (action) => {
    // There is no gateway here. Show what would have been sent instead of
    // pretending it went somewhere.
    logAction(action.instanceId, action.name, action.context);
  },
  onDiagnostic: (diagnostic) => {
    logAction(diagnostic.code, diagnostic.message, {});
  },
});

const mounts: SurfaceMount[] = [];

function sceneUpdate(sceneIndex: number): sse$SurfaceUpdate {
  const scene = DEMO_SCENES[sceneIndex];
  if (scene === undefined) {
    throw new Error(`No demo scene at index ${sceneIndex}`);
  }
  return {
    ...scene.update,
    // One surface per scene, so a component in one scene cannot affect another.
    surface_id: surfaceIdFor(scene.key),
  };
}

function surfaceIdFor(key: string): string {
  // Deterministic, well-formed UUID derived from the scene key.
  const hex = [...key].map((char) => char.charCodeAt(0).toString(16).padStart(2, '0')).join('');
  const padded = (hex + '0'.repeat(32)).slice(0, 32);
  return `${padded.slice(0, 8)}-${padded.slice(8, 12)}-4${padded.slice(13, 16)}-8${padded.slice(17, 20)}-${padded.slice(20, 32)}`;
}

function render(): void {
  const root = document.getElementById('demo');
  if (root === null) {
    throw new Error('Missing #demo root element.');
  }

  root.innerHTML = '';

  const header = document.createElement('header');
  header.className = 'gallery-header';
  header.innerHTML = `
    <div class="site-header"><a class="wordmark" href="/"><span class="brand-mark" aria-hidden="true">↗</span>ReferralEasy<span class="brand-context">Care, connected.</span></a><nav aria-label="Main navigation"><a href="/">Referrals</a><a href="/demo.html" aria-current="page">Component gallery</a><a href="/admin.html">Admin dashboard ↗</a></nav></div>
    <div class="gallery-intro"><div><span class="eyebrow">EXPLORE THE REFERRAL EXPERIENCE</span><h1>Every step. Every state.</h1><p>Try the eight referral components, including the moments when information is missing or an appointment is still pending.</p></div><div class="gallery-context"><span class="re-chip re-chip--synthetic">Synthetic data</span><p>No backend required.<br>Choices stay in this gallery.</p><a href="/">Create a demo referral ↗</a></div></div>
    <div class="gallery-toolbar"><div class="gallery-filters" role="group" aria-label="Filter gallery"><button type="button" data-filter="all" aria-pressed="true">All states</button><button type="button" data-filter="choices" aria-pressed="false">Provider choices</button><button type="button" data-filter="booking" aria-pressed="false">Review & outcomes</button><button type="button" data-filter="recovery" aria-pressed="false">Recovery & missing data</button></div><div class="demo-actions"><button type="button" id="apply-twice">Apply every update twice (idempotency)</button><button type="button" id="reload">Reset</button></div></div>
    <details class="gallery-activity"><summary>Local activity <span id="activity-count">· No actions yet</span></summary><div class="demo-log" id="demo-log" role="status" aria-live="polite">Actions appear here. Nothing is sent to a hospital.</div></details>
  `;
  root.append(header);

  const grid = document.createElement('main');
  grid.className = 'gallery-grid';
  root.append(grid);
  DEMO_SCENES.forEach((scene, index) => {
    const update = sceneUpdate(index);

    const section = document.createElement('section');
    section.className = 'demo-scene';
    section.dataset['category'] = ['hospitals', 'doctors', 'slots'].includes(scene.key) ? 'choices' : ['form', 'review', 'review-degraded', 'receipt-requested', 'receipt-confirmed', 'receipt-unverified'].includes(scene.key) ? 'booking' : 'recovery';

    const heading = document.createElement('div');
    heading.className = 'demo-scene-head';
    heading.innerHTML = `
      <h2>${escapeHtml(scene.label)}</h2>
      <p>${escapeHtml(scene.note)}</p>
    `;

    const slot = document.createElement('div');
    slot.className = 'demo-slot';

    section.append(heading, slot);
    grid.append(section);

    runtime.applyUpdate(update);
    mounts.push(mountSurface(slot, runtime, update.surface_id));
  });

  root.querySelectorAll<HTMLButtonElement>('[data-filter]').forEach((button) => {
    button.addEventListener('click', () => {
      const filter = button.dataset['filter'];
      root.querySelectorAll<HTMLButtonElement>('[data-filter]').forEach((other) => other.setAttribute('aria-pressed', String(other === button)));
      root.querySelectorAll<HTMLElement>('.demo-scene').forEach((scene) => { scene.hidden = filter !== 'all' && scene.dataset['category'] !== filter; });
    });
  });

  document.getElementById('apply-twice')?.addEventListener('click', () => {
    // Applying every fixture a second time must change nothing: the instance
    // count is keyed by `instance_id`, and a later update replaces in place.
    const before = countInstances();
    DEMO_SCENES.forEach((_scene, index) => {
      runtime.applyUpdate(sceneUpdate(index));
    });
    const after = countInstances();
    logAction(
      'IDEMPOTENCY',
      after === before
        ? `Applied every fixture twice. Instance count unchanged at ${after}.`
        : `Instance count changed from ${before} to ${after} — that is a bug.`,
      {},
    );
  });

  document.getElementById('reload')?.addEventListener('click', () => {
    window.location.reload();
  });
}

function countInstances(): number {
  return runtime.surfaceIds().reduce((total, id) => total + runtime.count(id), 0);
}

function logAction(code: string, message: string, context: Record<string, unknown>): void {
  const log = document.getElementById('demo-log');
  if (log === null) {
    return;
  }
  const entry = document.createElement('div');
  entry.className = 'demo-log-entry';
  const payload = Object.keys(context).length > 0 ? ` ${JSON.stringify(context)}` : '';
  entry.textContent = `${code} — ${message}${payload}`;
  if (!log.querySelector('.demo-log-entry')) log.textContent = '';
  log.append(entry);
  while (log.children.length > 20) log.firstElementChild?.remove();
  const count = document.getElementById('activity-count');
  if (count) count.textContent = `· ${log.children.length} recent ${log.children.length === 1 ? 'action' : 'actions'}`;
  const activity = log.closest('details');
  if (activity) activity.open = true;
  log.scrollTop = log.scrollHeight;
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/** Everything on one surface, for a single screenshot of the whole catalog. */
export function renderAllOnOneSurface(): void {
  const root = document.getElementById('demo');
  if (root === null) {
    return;
  }
  const slot = document.createElement('div');
  root.append(slot);
  const update = allScenesUpdate();
  runtime.applyUpdate(update);
  mounts.push(mountSurface(slot, runtime, update.surface_id));
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', render, { once: true });
} else {
  render();
}
