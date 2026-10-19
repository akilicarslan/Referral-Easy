import { describe, expect, it } from 'vitest';
import { deepQuery, deepQueryAll, deepText, eventually } from './helpers.js';
import { DEMO_SCENES } from '../src/fixtures/surfaces.js';

/**
 * The demo page, rendered for real.
 *
 * `demo.html` is the artefact a reviewer opens without a backend, so "it builds"
 * is not enough — every scene has to actually produce a component on screen, and
 * the idempotency button has to demonstrate what it claims. This test is the
 * closest thing to a screenshot that runs in CI.
 *
 * `#demo` must exist before the import, because the module renders on import.
 */

const root = document.createElement('div');
root.id = 'demo';
document.body.append(root);

await import('../src/demo-main.js');

const EIGHT_COMPONENTS = [
  're-hospital-picker',
  're-doctor-picker',
  're-slot-picker',
  're-patient-details-form',
  're-booking-review',
  're-appointment-receipt',
  're-directory-fallback-card',
  're-human-handoff-card',
];

describe('the component gallery', () => {
  it('renders every scene on its own surface', async () => {
    await eventually(
      () => deepQuery(root, 're-hospital-picker') !== null,
      'the first scene to render',
    );

    expect(deepQueryAll(root, '.demo-scene')).toHaveLength(DEMO_SCENES.length);

    for (const scene of DEMO_SCENES) {
      expect(deepText(root), `scene "${scene.key}" should be labelled`).toContain(scene.label);
    }
  });

  it('puts all eight components on screen', async () => {
    await eventually(
      () => EIGHT_COMPONENTS.every((tag) => deepQuery(root, tag) !== null),
      `all eight components to render (missing: ${EIGHT_COMPONENTS.filter(
        (tag) => deepQuery(root, tag) === null,
      ).join(', ')})`,
    );

    for (const tag of EIGHT_COMPONENTS) {
      expect(deepQuery(root, tag), `${tag} should be rendered`).not.toBeNull();
    }
  });

  it('labels the fixture data, including on the negative scenes', async () => {
    await eventually(() => deepText(root).includes('Synthetic data'), 'the synthetic labels');

    const text = deepText(root);

    // The scenes that exist to show a rule being obeyed, not a feature working.
    expect(text).toContain('Not verified');
    expect(text).toContain('could not verify');
    expect(text).toContain('Reduced lookup');
    expect(text).toContain('Choose a hospital first');
    expect(text).toContain('Nothing is selected yet');
  });

  it('filters visible scenes without unmounting their component state', () => {
    const filter = root.querySelector<HTMLButtonElement>('[data-filter="booking"]')!;
    filter.click();
    const visible = [...root.querySelectorAll<HTMLElement>('.demo-scene')].filter((scene) => !scene.hidden);
    expect(visible).toHaveLength(6);
    expect(visible.every((scene) => scene.dataset['category'] === 'booking')).toBe(true);
    expect(filter.getAttribute('aria-pressed')).toBe('true');
    expect(deepQuery(root, 're-hospital-picker')).not.toBeNull();
    root.querySelector<HTMLButtonElement>('[data-filter="all"]')!.click();
    expect([...root.querySelectorAll<HTMLElement>('.demo-scene')].every((scene) => !scene.hidden)).toBe(true);
  });

  it('demonstrates idempotency when every fixture is applied a second time', async () => {
    await eventually(() => deepQuery(root, 're-slot-picker') !== null, 'the scenes to render');

    const button = deepQuery<HTMLButtonElement>(root, '#apply-twice');
    expect(button, 'the idempotency button should exist').not.toBeNull();

    button?.click();

    const log = deepText(root);
    expect(log).toContain('IDEMPOTENCY');
    expect(log, 'applying every fixture twice must not change the instance count').toMatch(
      /Instance count unchanged at \d+/,
    );
    // And the components are still there — nothing was duplicated or torn down.
    expect(deepQueryAll(root, 're-hospital-picker')).toHaveLength(1);
    expect(deepQueryAll(root, 're-appointment-receipt')).toHaveLength(3);
  });
});
