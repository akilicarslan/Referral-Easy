/**
 * Environment gaps that would otherwise produce a *false pass*.
 *
 * jsdom does not implement constructable stylesheets: `CSSStyleSheet` exists,
 * but `replaceSync` does not, and neither `document` nor `ShadowRoot` has
 * `adoptedStyleSheets`. `@a2ui/web_core`'s basic catalog — whose `Column` is the
 * surface root — injects its default stylesheet from `connectedCallback`, so the
 * missing method throws *inside the render pipeline*, taking the subtree with it.
 *
 * Left alone, that turns "the A2UI renderer works" into "some tests fail with an
 * error that looks environmental", which is exactly how a real integration break
 * gets dismissed. So it is shimmed here, deliberately minimal, and only when the
 * platform genuinely lacks the API.
 *
 * The shim stores rules as text. It does not style anything, and it does not
 * pretend to: component styling is not what these tests assert.
 */

const stylesheetText = new WeakMap<object, string>();

if (
  typeof CSSStyleSheet !== 'undefined' &&
  typeof CSSStyleSheet.prototype.replaceSync !== 'function'
) {
  Object.defineProperty(CSSStyleSheet.prototype, 'replaceSync', {
    configurable: true,
    writable: true,
    value(this: object, text: string): void {
      stylesheetText.set(this, text);
    },
  });

  if (typeof CSSStyleSheet.prototype.replace !== 'function') {
    Object.defineProperty(CSSStyleSheet.prototype, 'replace', {
      configurable: true,
      writable: true,
      value(this: object, text: string): Promise<CSSStyleSheet> {
        stylesheetText.set(this, text);
        return Promise.resolve(this as unknown as CSSStyleSheet);
      },
    });
  }

  if (!('cssRules' in CSSStyleSheet.prototype)) {
    Object.defineProperty(CSSStyleSheet.prototype, 'cssRules', {
      configurable: true,
      get(this: object): unknown[] {
        const text = stylesheetText.get(this);
        return text === undefined ? [] : [{ cssText: text }];
      },
    });
  }
}

/** Give a prototype a working `adoptedStyleSheets` list, if it has none. */
function ensureAdoptedStyleSheets(prototype: object | undefined): void {
  if (prototype === undefined || 'adoptedStyleSheets' in prototype) {
    return;
  }
  Object.defineProperty(prototype, 'adoptedStyleSheets', {
    configurable: true,
    get(this: object): unknown[] {
      return (this as { __adoptedStyleSheets?: unknown[] }).__adoptedStyleSheets ?? [];
    },
    set(this: object, value: unknown[]): void {
      (this as { __adoptedStyleSheets?: unknown[] }).__adoptedStyleSheets = value;
    },
  });
}

if (typeof ShadowRoot !== 'undefined') {
  ensureAdoptedStyleSheets(ShadowRoot.prototype);
}
if (typeof Document !== 'undefined') {
  ensureAdoptedStyleSheets(Document.prototype);
}
