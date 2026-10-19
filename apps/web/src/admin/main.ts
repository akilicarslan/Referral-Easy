import '../styles/tokens.css';
import '../styles/base.css';
import './shell.js';

import { AdminClient } from './api.js';
import { AdminShell } from './shell.js';

/**
 * Entry point for the admin app.
 *
 * Third entry point, third HTML document: the patient client (`index.html`), the component gallery
 * (`demo.html`) and this one. They are separate documents rather than routes in one app because they
 * are separate audiences — a patient must not be able to reach the admin by changing a URL, and the
 * admin carries a bundle the patient page has no use for.
 *
 * **What this page is not.** It reads telemetry from a demo stack over a fixture session that
 * proves nothing about who is asking: `POST /v1/auth/admin-session` exists only in the demo profile,
 * validates no credential, and says `fixture: true` in its answer. That is honest for a portfolio
 * demo and would not be for a deployment — §9's audience-checked service identity is what replaces
 * it, and this client would be the code to change.
 */
export function bootAdmin(): void {
  const shell = new AdminShell();
  shell.client = new AdminClient();

  const root = document.getElementById('admin');
  if (root === null) {
    throw new Error('Missing #admin root element.');
  }
  // The pre-bundle skeleton is replaced rather than appended to: it is a *loading* state, and leaving
  // it under a rendered dashboard would show a permanent "nothing has been read yet" beside real
  // numbers.
  root.replaceChildren(shell);
}

/**
 * Last-resort failure screen, same rule as the patient client: `boot` throwing must not leave a blank
 * page, and the screen must say that nothing was changed.
 */
function renderBootFailure(error: unknown): void {
  const host = document.getElementById('admin') ?? document.body;
  const box = document.createElement('section');
  box.setAttribute('role', 'alert');
  box.style.cssText = 'padding:24px;font:16px/1.5 system-ui,sans-serif;color:#131a1f';

  const heading = document.createElement('h1');
  heading.textContent = 'The admin could not start';
  const detail = document.createElement('p');
  detail.textContent = `Nothing was read and nothing was changed. (${
    error instanceof Error ? error.message : String(error)
  })`;

  box.append(heading, detail);
  host.replaceChildren(box);
}

function run(): void {
  try {
    bootAdmin();
  } catch (error) {
    renderBootFailure(error);
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', run, { once: true });
} else {
  run();
}
