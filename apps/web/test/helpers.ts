import type { sse$ComponentInstance, sse$SurfaceUpdate } from '@referraleasy/contracts';

/** Deterministic UUIDs so failures are reproducible. */
export function uuid(n: number): string {
  const tail = n.toString(16).padStart(12, '0');
  return `00000000-0000-4000-8000-${tail}`;
}

export function instance(
  patch: Partial<sse$ComponentInstance> & Pick<sse$ComponentInstance, 'component'>,
): sse$ComponentInstance {
  return {
    instance_id: uuid(1),
    workflow_state_version: 1,
    props: {},
    ...patch,
  } as sse$ComponentInstance;
}

export function surfaceUpdate(
  components: sse$ComponentInstance[],
  extra: Partial<sse$SurfaceUpdate> = {},
): sse$SurfaceUpdate {
  return { surface_id: uuid(99), components, ...extra } as sse$SurfaceUpdate;
}

/**
 * Wait until `check` passes, or fail.
 *
 * Component updates settle over Lit's microtask queue, so tests need to let the
 * queue drain rather than guess a number of ticks.
 */
export async function eventually(
  check: () => boolean,
  message: string,
  timeoutMs = 2000,
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (check()) {
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  throw new Error(`Timed out waiting for: ${message}`);
}

/**
 * Query across shadow roots.
 *
 * The surface, each component and the app shell are all shadow-DOM
 * encapsulated, so a plain `querySelector` from the document never reaches the
 * rendered content.
 */
export function deepQuery<T extends Element = HTMLElement>(root: ParentNode, selector: string): T | null {
  const direct = root.querySelector<T>(selector);
  if (direct !== null) {
    return direct;
  }
  // The root's OWN shadow root is invisible to both `querySelector` and
  // `querySelectorAll('*')` — the latter only walks descendants. Without this,
  // querying a shadow host directly (the app shell, say) silently finds nothing
  // and every assertion about it fails as though the component were broken.
  const own = shadowOf(root);
  if (own !== null) {
    const found = deepQuery<T>(own, selector);
    if (found !== null) {
      return found;
    }
  }
  for (const element of root.querySelectorAll('*')) {
    const shadow = element.shadowRoot;
    if (shadow !== null) {
      const found = deepQuery<T>(shadow, selector);
      if (found !== null) {
        return found;
      }
    }
  }
  return null;
}

/** Every match across shadow roots, including the root's own. */
export function deepQueryAll<T extends Element = HTMLElement>(
  root: ParentNode,
  selector: string,
): T[] {
  const results: T[] = [...root.querySelectorAll<T>(selector)];

  const own = shadowOf(root);
  if (own !== null) {
    results.push(...deepQueryAll<T>(own, selector));
  }
  for (const element of root.querySelectorAll('*')) {
    const shadow = element.shadowRoot;
    if (shadow !== null) {
      results.push(...deepQueryAll<T>(shadow, selector));
    }
  }
  return results;
}

function shadowOf(node: ParentNode): ShadowRoot | null {
  return node instanceof Element ? node.shadowRoot : null;
}

/**
 * All rendered text across shadow roots, whitespace-collapsed.
 *
 * A node's own shadow content is visited before its light children, so a host
 * element's rendered output is included rather than skipped.
 */
export function deepText(root: ParentNode): string {
  const parts: string[] = [];
  const visit = (node: ParentNode): void => {
    const shadow = shadowOf(node);
    if (shadow !== null) {
      visit(shadow);
    }
    for (const child of node.childNodes) {
      if (child.nodeType === Node.TEXT_NODE) {
        parts.push(child.textContent ?? '');
      } else if (child instanceof Element) {
        visit(child);
      }
    }
  };
  visit(root);
  return parts.join(' ').replace(/\s+/g, ' ').trim();
}
