import { LitElement } from 'lit';
import { sharedStyles } from './shared-styles.js';

/**
 * Structural views of the A2UI objects the renderer hands us.
 *
 * These are declared here, structurally, on purpose. This file must NOT import
 * `@a2ui/*` — `src/a2ui/adapter.ts` is the only module allowed to do that
 * (enforced by `test/adapter-boundary.test.ts`). Describing the two objects we
 * consume as plain interfaces keeps the boundary intact while still being
 * type-safe at the point of use.
 */

/** The component model behind a rendered node — where the gateway's props live. */
export interface ComponentModelLike {
  readonly id?: string;
  readonly type?: string;
  readonly properties?: Record<string, unknown>;
}

/** What the renderer assigns to `element.context`. */
export interface ComponentContextLike {
  readonly componentModel?: ComponentModelLike;
  dispatchAction?(action: unknown): unknown;
}

/** What the renderer assigns to `element.node`. */
export interface ComponentNodeLike {
  readonly componentId?: string;
  readonly type?: string;
  readonly context?: ComponentContextLike;
  /** Resolved props, exposed as a signal. Read defensively; see `rawProps`. */
  readonly props?: { readonly value?: unknown };
}

/**
 * Base class for the eight ReferralEasy components.
 *
 * Plain `LitElement`, not A2UI's `A2uiLitElement`, because the A2UI renderer
 * assigns `node` and `context` as ordinary properties and never type-checks the
 * element. Staying on plain Lit keeps `@a2ui/*` confined to the adapter.
 *
 * Props are read from the RAW gateway payload rather than through the binder's
 * resolved view. That is deliberate: display must not depend on how a third
 * party resolves a binding, and the raw object is exactly what the server sent.
 * The resolved signal is used only as a fallback when the raw model is absent.
 */
export abstract class ReferralElement extends LitElement {
  static override properties = {
    node: { attribute: false },
    context: { attribute: false },
  };

  declare node?: ComponentNodeLike | undefined;
  declare context?: ComponentContextLike | undefined;

  /** Shadow DOM: component styles stay inside, page styles stay outside. */
  override createRenderRoot(): HTMLElement | DocumentFragment {
    return super.createRenderRoot();
  }

  static override styles = [sharedStyles];

  /**
   * The props the gateway sent for this instance.
   *
   * Never throws and never returns `undefined`: a component whose payload is
   * missing entirely still renders an honest empty state.
   */
  protected get rawProps(): Record<string, unknown> {
    // Which property carries the model depends on how the renderer reached us,
    // and BOTH paths are real in 0.13.0 (verified against the installed source):
    //
    //   * `renderA2uiNode(node)` — the `ComponentNode` path — assigns `node` and
    //     `context`, and is taken only when a parent already holds a node.
    //   * `renderA2uiNode(context, catalog)` — the `ComponentContext` path, used
    //     by `A2uiSurface.render()` itself and by any container that built a
    //     child context — assigns **only** `context`.
    //
    // Our components are reached through the second path, so reading `node`
    // alone silently yielded `{}` for every field: a screen that renders, but
    // renders "no data" for data it was given. Both are checked, node first.
    const model = this.node?.context?.componentModel ?? this.context?.componentModel;
    const fromModel = model?.properties;
    if (fromModel !== null && typeof fromModel === 'object') {
      return fromModel as Record<string, unknown>;
    }
    const resolved = this.node?.props?.value;
    if (resolved !== null && typeof resolved === 'object' && !Array.isArray(resolved)) {
      return resolved as Record<string, unknown>;
    }
    return {};
  }

  /** The `instance_id` this element is rendering, when known. */
  protected get instanceId(): string | undefined {
    return (
      this.node?.componentId ??
      this.node?.context?.componentModel?.id ??
      this.context?.componentModel?.id
    );
  }

  protected str(key: string): string | undefined {
    const value = this.rawProps[key];
    return typeof value === 'string' && value.length > 0 ? value : undefined;
  }

  protected num(key: string): number | undefined {
    const value = this.rawProps[key];
    return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
  }

  protected bool(key: string): boolean | undefined {
    const value = this.rawProps[key];
    return typeof value === 'boolean' ? value : undefined;
  }

  protected list(key: string): readonly Record<string, unknown>[] {
    const value = this.rawProps[key];
    if (!Array.isArray(value)) {
      return [];
    }
    return value.filter(
      (item): item is Record<string, unknown> =>
        item !== null && typeof item === 'object' && !Array.isArray(item),
    );
  }

  protected record(key: string): Record<string, unknown> | undefined {
    const value = this.rawProps[key];
    if (value === null || typeof value !== 'object' || Array.isArray(value)) {
      return undefined;
    }
    return value as Record<string, unknown>;
  }

  /** The server's inline explanation for this instance, if it sent one. */
  protected get serverNotice(): string | undefined {
    return this.str('notice');
  }

  /** True once the patient has answered this instance; it must not ask twice. */
  protected get isSubmitted(): boolean {
    return this.bool('submitted') === true;
  }

  /** Server-issued disablement. The client adds its own on top of this. */
  protected get isDisabled(): boolean {
    return this.bool('disabled') === true;
  }

  /**
   * Raise a patient action.
   *
   * Only the A2UI action *name* originates here. The action the server will act
   * on (`sse$ClientAction.action`) is the one the server issued on this instance,
   * and the app resolves that from its own store — the client never invents one.
   */
  protected emitAction(name: string, context: Record<string, unknown>): void {
    const dispatcher = this.context?.dispatchAction;
    if (typeof dispatcher !== 'function') {
      // Older/alternate renderer wiring. Fall back to a DOM event so the app can
      // still receive it rather than silently dropping the patient's answer.
      this.dispatchEvent(
        new CustomEvent('re-action', {
          detail: { name, context, instanceId: this.instanceId },
          bubbles: true,
          composed: true,
        }),
      );
      return;
    }
    void dispatcher.call(this.context, { name, context });
  }
}
