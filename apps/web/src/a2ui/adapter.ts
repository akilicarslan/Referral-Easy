/**
 * ─────────────────────────────────────────────────────────────────────────────
 *  THE A2UI BOUNDARY
 * ─────────────────────────────────────────────────────────────────────────────
 *
 * This is the ONLY module in `apps/web` that imports `@a2ui/*`. Everything else
 * in the app talks to the interfaces exported here, expressed purely in terms of
 * this repo's own contract types and plain TypeScript. `test/adapter-boundary.test.ts`
 * greps the source tree and fails if any other file reaches across.
 *
 * Why the boundary exists: the A2UI protocol version is a *wire detail*. Pinning
 * it behind one file is what makes a v0.9 -> v1.0 migration a one-file change
 * instead of a repo-wide hunt.
 *
 * What this module owns:
 *   1. Translating an `sse$SurfaceUpdate` (the gateway's wire shape) into A2UI
 *      v0.9 protocol messages.
 *   2. Reconciling rather than accumulating. Each apply rebuilds the *whole*
 *      component list from a `Map` keyed by `instance_id`, so applying the same
 *      update twice yields an identical tree. Idempotency is a property of
 *      construction here, not a hope about how A2UI merges internally.
 *   3. Exposing the surface to Lit as one element, so no A2UI type or element
 *      name appears in the app.
 *
 * ── Verified against the installed package, 0.13.0 ───────────────────────────
 *   Catalog            `new Catalog(id, protocolVersion, components, functions)`
 *   Custom catalog     `WebComponentImplementation = ComponentApi + {tagName, element}`
 *   MessageProcessor   `.processMessages({version:'v0.9', updateComponents:{...}})`
 *   Surfaces           `.getSurface(id)`, `.onSurfaceCreated(handler)`
 *   Actions            `SurfaceModel.onAction(ActionPayload)`
 *
 * What is verified by a test versus what is not is itemised in
 * `docs/adr/ADR-0027-web-client.md`.
 */

import { MessageProcessor, Catalog } from '@a2ui/web_core/v0_9';
import { basicCatalog } from '@a2ui/web_core/v0_9/basic_catalog';
import type { ComponentApi, ActionPayload } from '@a2ui/web_core/v0_9';
// `v0_9/universal` re-exports the same binding layer as `./universal`, but keeps
// every specifier in this file on a protocol-versioned entry point: a future
// major that changes the web-component binding lands in a new folder rather than
// moving this import underneath us. `test/adapter-boundary.test.ts` pins that.
import type { WebComponentImplementation } from '@a2ui/web_core/v0_9/universal';
import { A2uiSurface } from '@a2ui/lit/v0_9';
import { z } from 'zod-v3';

import type { sse$SurfaceUpdate, sse$ComponentInstance } from '@referraleasy/contracts';
import { COMPONENT_DEFS } from './component-defs.js';

// ── Exported facts about the pin ─────────────────────────────────────────────
// These mirror `package.json`; a test asserts they stay in step so a dependency
// bump cannot leave the ADR and the UI claiming the wrong protocol.

/** Pinned A2UI core version. Must equal the `@a2ui/web_core` dependency. */
export const A2UI_CORE_VERSION = '0.13.0';

/** Pinned A2UI Lit distribution channel. Must equal the `@a2ui/lit` dependency. */
export const A2UI_LIT_VERSION = '0.13.0';

/** Wire protocol we construct messages for. */
export const A2UI_PROTOCOL_VERSION = 'v0.9';

/** Catalog id sent in `createSurface` and used as the surface default. */
export const CATALOG_ID = 'https://referraleasy.local/catalogs/referral/v1';

/**
 * Custom element this adapter mounts a surface into.
 *
 * This is the tag `@a2ui/lit` registers `A2uiSurface` under **at import time** —
 * verified at runtime, not assumed. Defining the same constructor a second time
 * under our own name throws `NotSupportedError`: the custom element registry
 * allows one constructor to own exactly one tag. An earlier revision tried to
 * register `re-a2ui-surface`, the throw was swallowed, and every surface mounted
 * as an inert unknown element — a blank screen, which this project treats as a
 * failure. `defineSurfaceElement` below is now only a safety net.
 */
export const SURFACE_TAG = 'a2ui-surface';

/** Root component id A2UI expects on every surface (`SurfaceModel.rootId`). */
const ROOT_ID = 'root';

// ── App-facing types (deliberately free of any `@a2ui` type) ─────────────────

/** An action the patient took on a server-issued control. */
export interface AdapterAction {
  /** `instance_id` of the component that raised it. */
  readonly instanceId: string;
  /** The A2UI action name; the app resolves it back to the server-issued string. */
  readonly name: string;
  /** The patient's answer, if the control collected one. */
  readonly context: Record<string, unknown>;
}

/** A non-fatal problem inside the surface pipeline. Never blanks the surface. */
export interface AdapterDiagnostic {
  readonly code: string;
  readonly message: string;
  readonly surfaceId?: string;
}

/** What one `applyUpdate` did, so tests can assert idempotency directly. */
export interface ApplyResult {
  readonly surfaceId: string;
  /** Total instances live after this apply. */
  readonly total: number;
  /** Ids not present before this apply. */
  readonly added: readonly string[];
  /** Ids already present whose payload was replaced. */
  readonly replaced: readonly string[];
  /** Ids dropped by `remove_instance_ids`. */
  readonly removed: readonly string[];
}

/** Handlers the app supplies when it builds a runtime. */
export interface SurfaceRuntimeOptions {
  onAction?: (action: AdapterAction) => void;
  onDiagnostic?: (diagnostic: AdapterDiagnostic) => void;
}

// ── Component schemas ────────────────────────────────────────────────────────
//
// Every field is `.optional()` and the object is `.passthrough()`. That is not
// laziness; it is the offline-first rule made structural:
//
//   * A payload missing a field must still render. Rejecting it at the schema
//     would blank the screen, and a blank screen is a failure.
//   * The backend is the validating authority for these props (contract §2.1:
//     "the gateway never forwards an unvalidated agent payload"). The schema
//     here describes the transport shape and keeps unknown fields reachable.
//
// Each component decides at render time what a missing field *means*, and says
// so on screen.

const accessibilitySchema = z
  .object({
    label: z.union([z.string(), z.object({ path: z.string() }).passthrough()]).optional(),
    description: z.union([z.string(), z.object({ path: z.string() }).passthrough()]).optional(),
  })
  .passthrough()
  .optional();

/**
 * The envelope every one of the eight components carries.
 *
 * `action`/`submitted`/`disabled`/`notice` are the server's words about this
 * instance. They are declared here and carried through verbatim so a component
 * can render them — never synthesised on the client.
 */
const envelope: Record<string, z.ZodTypeAny> = {
  accessibility: accessibilitySchema,
  weight: z.number().optional(),
  action: z.union([z.string(), z.null()]).optional(),
  submitted: z.boolean().optional(),
  disabled: z.boolean().optional(),
  notice: z.union([z.string(), z.null()]).optional(),
};

/** A loose object — used for sub-records whose shape the backend owns. */
const record = () => z.record(z.unknown());

const componentSchema = (fields: Record<string, z.ZodTypeAny>) =>
  z.object({ ...envelope, ...fields }).passthrough();

const tristate = z.enum(['yes', 'no', 'unknown']);

const SCHEMAS: Record<string, z.ZodTypeAny> = {
  // ── Selection components ────────────────────────────────────────────────
  HospitalPicker: componentSchema({
    title: z.string().optional(),
    subtitle: z.string().optional(),
    /** Ordered. The patient sees hospitals before doctors, always (§4). */
    hospitals: z.array(record()).optional(),
    selected_id: z.union([z.string(), z.null()]).optional(),
    coverage: tristate.optional(),
    coverage_freshness: record().optional(),
    empty_reason: z.string().optional(),
    sample_size: z.number().optional(),
    synthetic: z.boolean().optional(),
  }),

  DoctorPicker: componentSchema({
    title: z.string().optional(),
    subtitle: z.string().optional(),
    /** Doctors belong to a hospital. Present so the UI can name it. */
    hospital_id: z.string().optional(),
    hospital_name: z.string().optional(),
    doctors: z.array(record()).optional(),
    selected_id: z.union([z.string(), z.null()]).optional(),
    /** True until a hospital is chosen — the UI must not imply otherwise. */
    awaiting_hospital: z.boolean().optional(),
    empty_reason: z.string().optional(),
    sample_size: z.number().optional(),
    synthetic: z.boolean().optional(),
  }),

  SlotPicker: componentSchema({
    title: z.string().optional(),
    subtitle: z.string().optional(),
    provider_id: z.string().optional(),
    provider_name: z.string().optional(),
    /** IANA zone. A slot time without a zone is not a time (§17). */
    display_timezone: z.string().optional(),
    slots: z.array(record()).optional(),
    selected_id: z.union([z.string(), z.null()]).optional(),
    empty_reason: z.string().optional(),
    sample_size: z.number().optional(),
    synthetic: z.boolean().optional(),
  }),

  // ── The real form. Patient data never travels through LLM-generated HTML. ──
  PatientDetailsForm: componentSchema({
    title: z.string().optional(),
    subtitle: z.string().optional(),
    /** Field descriptors; known types render, unknown types are reported. */
    fields: z.array(record()).optional(),
    /** Synthetic fixture documents the patient may attach (§2.3). */
    documents: z.array(record()).optional(),
    values: record().optional(),
    /** Server-side errors keyed by field. Client validation is advisory only. */
    errors: z.record(z.string()).optional(),
    submit_label: z.string().optional(),
  }),

  // ── Review + receipt ────────────────────────────────────────────────────
  BookingReview: componentSchema({
    title: z.string().optional(),
    hospital: record().optional(),
    provider: record().optional(),
    slot: record().optional(),
    patient_summary: record().optional(),
    attachments: z.array(record()).optional(),
    /** Consent text the patient agrees to. Shown in full, never truncated. */
    consent_text: z.string().optional(),
    requires_consent: z.boolean().optional(),
    submit_label: z.string().optional(),
    /** Set when the answer came from a reduced pipeline (§10). */
    degraded: z.union([z.string(), z.null()]).optional(),
  }),

  AppointmentReceipt: componentSchema({
    title: z.string().optional(),
    /**
     * Booking lifecycle state. `REQUESTED` and `PENDING_REVIEW` are NOT
     * confirmations and must never render as one.
     */
    state: z.string().optional(),
    appointment: record().optional(),
    booking_request_id: z.string().optional(),
    hospital: record().optional(),
    provider: record().optional(),
    slot: record().optional(),
    display_timezone: z.string().optional(),
    /** What the patient should expect next, and when. */
    next_step: z.string().optional(),
    confirmation_reference: z.union([z.string(), z.null()]).optional(),
    reconciles_at: z.union([z.string(), z.null()]).optional(),
    derived_from: z.union([z.string(), z.null()]).optional(),
    degraded: z.union([z.string(), z.null()]).optional(),
    synthetic: z.boolean().optional(),
  }),

  // ── Fallbacks ───────────────────────────────────────────────────────────
  DirectoryFallbackCard: componentSchema({
    title: z.string().optional(),
    body: z.string().optional(),
    reason_code: z.string().optional(),
    reason_detail: z.string().optional(),
    degraded: z.union([z.string(), z.null()]).optional(),
    /** What we could not check, stated plainly rather than implied as "no". */
    unchecked: z.array(z.string()).optional(),
    retry_label: z.string().optional(),
    can_retry: z.boolean().optional(),
    last_known: record().optional(),
    synthetic: z.boolean().optional(),
  }),

  HumanHandoffCard: componentSchema({
    title: z.string().optional(),
    body: z.string().optional(),
    reason_code: z.string().optional(),
    reason_detail: z.string().optional(),
    /** What was already handed over, so the patient need not repeat it. */
    handed_over: z.array(z.string()).optional(),
    /** When a human is expected to pick this up. */
    response_window: z.string().optional(),
    contact_channels: z.array(record()).optional(),
    reference: z.string().optional(),
    synthetic: z.boolean().optional(),
  }),
};

// ── Catalog ──────────────────────────────────────────────────────────────────

type ReferralComponent = WebComponentImplementation<z.ZodTypeAny>;

function buildCatalog(): Catalog<ComponentApi> {
  const implementations: ReferralComponent[] = COMPONENT_DEFS.map((def) => {
    const schema = SCHEMAS[def.componentName];
    if (schema === undefined) {
      throw new Error(`adapter: no schema registered for ${def.componentName}`);
    }
    return {
      name: def.componentName,
      schema,
      tagName: def.elementTag,
      element: def.element as unknown as CustomElementConstructor,
    };
  });

  // The eight ReferralEasy components ARE the product surface. The A2UI basic
  // catalog is merged in ONLY to supply layout containers (`Column`) for the
  // surface root and spacing. No basic-catalog component is ever used to express
  // referral content — that would be the "ninth component" this project
  // explicitly forbids. Reasoning recorded in the ADR.
  const components = [...basicCatalog.components.values(), ...implementations];
  const functions = [...basicCatalog.functions.values()];

  return new Catalog<ComponentApi>(
    CATALOG_ID,
    A2UI_PROTOCOL_VERSION,
    components,
    functions as never,
  );
}

/**
 * Define the surface element if — and only if — the package did not.
 *
 * Normally a no-op. It exists so that a future `@a2ui/lit` that stops
 * self-registering degrades to "we define it ourselves" rather than to an inert
 * element. The failure is logged rather than swallowed: a silent catch here is
 * exactly what turned this into a blank screen the first time.
 */
function defineSurfaceElement(): void {
  if (customElements.get(SURFACE_TAG) !== undefined) {
    return;
  }
  try {
    customElements.define(SURFACE_TAG, A2uiSurface);
  } catch (error) {
    // Never fatal: throwing at module load would take the whole app down.
    console.error(`[referraleasy] could not register <${SURFACE_TAG}>:`, error);
  }
}

/** Run once, at module load, so app code never deals with element registration. */
defineSurfaceElement();

// ── The runtime ──────────────────────────────────────────────────────────────

/**
 * Owns one A2UI `MessageProcessor` and reconciles `SurfaceUpdate`s into it.
 *
 * Not itself a Lit element: the app's own store decides what to render, and the
 * surface host follows it through `subscribe()`.
 */
export class SurfaceRuntime {
  readonly #processor: MessageProcessor<ComponentApi>;
  readonly #options: SurfaceRuntimeOptions;

  /** surfaceId -> (instanceId -> instance). Insertion order is render order. */
  readonly #instances = new Map<string, Map<string, sse$ComponentInstance>>();
  readonly #listeners = new Set<() => void>();
  #disposed = false;

  constructor(options: SurfaceRuntimeOptions = {}) {
    this.#options = options;
    const catalog = buildCatalog();

    this.#processor = new MessageProcessor<ComponentApi>(
      [catalog],
      (action: ActionPayload) => {
        const instanceId = action.sourceComponentId;
        if (typeof instanceId !== 'string' || instanceId.length === 0) {
          this.#diagnose({
            code: 'ACTION_WITHOUT_SOURCE',
            message: 'An action arrived with no source component and was dropped.',
          });
          return;
        }
        this.#options.onAction?.({
          instanceId,
          name: typeof action.name === 'string' ? action.name : '',
          context:
            action.context !== null &&
            typeof action.context === 'object' &&
            !Array.isArray(action.context)
              ? (action.context as Record<string, unknown>)
              : {},
        });
      },
      { version: A2UI_PROTOCOL_VERSION },
    );
  }

  /** Subscribe to surface changes. Returns an unsubscribe function. */
  subscribe(listener: () => void): () => void {
    this.#listeners.add(listener);
    return () => {
      this.#listeners.delete(listener);
    };
  }

  /** Instances live for a surface, in render order. */
  instances(surfaceId: string): readonly sse$ComponentInstance[] {
    return [...(this.#instances.get(surfaceId)?.values() ?? [])];
  }

  /** How many instances are live for a surface. */
  count(surfaceId: string): number {
    return this.#instances.get(surfaceId)?.size ?? 0;
  }

  /** Surface ids the runtime knows about. */
  surfaceIds(): readonly string[] {
    return [...this.#instances.keys()];
  }

  /** The underlying surface model. Only the mounted `<${SURFACE_TAG}>` needs it. */
  surfaceModel(surfaceId: string): unknown {
    return this.#processor.getSurface(surfaceId);
  }

  /**
   * Apply one `SurfaceUpdate`.
   *
   * The instance map is the source of truth, and every apply pushes the complete
   * list back through A2UI. Applying the same update twice is therefore a no-op
   * on the second call: same keys, same order, same payloads.
   */
  applyUpdate(update: sse$SurfaceUpdate): ApplyResult {
    const surfaceId = update.surface_id;
    if (this.#disposed) {
      return { surfaceId, total: 0, added: [], replaced: [], removed: [] };
    }

    let live = this.#instances.get(surfaceId);
    if (live === undefined) {
      live = new Map<string, sse$ComponentInstance>();
      this.#instances.set(surfaceId, live);
      this.#ensureSurface(surfaceId);
    }

    const added: string[] = [];
    const replaced: string[] = [];
    const removed: string[] = [];

    for (const id of update.remove_instance_ids ?? []) {
      if (live.delete(id)) {
        removed.push(id);
      }
    }

    for (const instance of update.components ?? []) {
      if (live.has(instance.instance_id)) {
        replaced.push(instance.instance_id);
      } else {
        added.push(instance.instance_id);
      }
      // `Map.set` on an existing key replaces in place and keeps position, which
      // is what gives the component column a stable order across updates.
      live.set(instance.instance_id, instance);
    }

    this.#pushComponents(surfaceId, live);
    this.#emit();

    return { surfaceId, total: live.size, added, replaced, removed };
  }

  /** Drop everything for a surface — used when the workflow restarts. */
  removeSurface(surfaceId: string): void {
    if (!this.#instances.delete(surfaceId)) {
      return;
    }
    try {
      this.#processor.processMessages({
        version: A2UI_PROTOCOL_VERSION,
        deleteSurface: { surfaceId },
      } as never);
    } catch {
      // A surface that was never created cannot be deleted. Not worth reporting.
    }
    this.#emit();
  }

  dispose(): void {
    this.#disposed = true;
    this.#instances.clear();
    this.#listeners.clear();
    this.#processor.dispose();
  }

  // ── internals ────────────────────────────────────────────────────────────

  #ensureSurface(surfaceId: string): void {
    try {
      this.#processor.processMessages({
        version: A2UI_PROTOCOL_VERSION,
        createSurface: {
          surfaceId,
          catalogId: CATALOG_ID,
          theme: {},
          sendDataModel: false,
        },
      } as never);
    } catch (error) {
      this.#diagnose({
        code: 'SURFACE_CREATE_FAILED',
        message: `Could not create surface ${surfaceId}: ${describeError(error)}`,
        surfaceId,
      });
    }
  }

  #pushComponents(surfaceId: string, live: Map<string, sse$ComponentInstance>): void {
    // `root` is a layout container from the A2UI basic catalog. It is
    // infrastructure, not a product component: it holds no referral meaning and
    // is never sent by the gateway.
    const components: Record<string, unknown>[] = [
      { id: ROOT_ID, component: 'Column', children: [...live.keys()], justify: 'start', align: 'stretch' },
    ];

    for (const instance of live.values()) {
      const props = (instance.props ?? {}) as Record<string, unknown>;
      components.push({
        ...props,
        id: instance.instance_id,
        component: instance.component,
        action: instance.action ?? null,
        submitted: instance.submitted ?? false,
        disabled: instance.disabled ?? false,
        notice: instance.notice ?? props['notice'] ?? null,
      });
    }

    try {
      this.#processor.processMessages({
        version: A2UI_PROTOCOL_VERSION,
        updateComponents: { surfaceId, components },
      } as never);
    } catch (error) {
      this.#diagnose({
        code: 'SURFACE_UPDATE_REJECTED',
        message: `A2UI rejected an update for ${surfaceId}: ${describeError(error)}`,
        surfaceId,
      });
    }
  }

  #diagnose(diagnostic: AdapterDiagnostic): void {
    this.#options.onDiagnostic?.(diagnostic);
  }

  #emit(): void {
    for (const listener of this.#listeners) {
      listener();
    }
  }
}

function describeError(error: unknown): string {
  if (error instanceof Error) {
    return error.message;
  }
  return String(error);
}

// ── Mounting ─────────────────────────────────────────────────────────────────

/** Handle returned by `mountSurface`, for teardown. */
export interface SurfaceMount {
  /** Remove the surface from the DOM and stop following the runtime. */
  unmount(): void;
}

/**
 * Render one surface into a container element.
 *
 * A plain function rather than a Lit element on purpose. The A2UI surface must
 * be created imperatively (`<a2ui-surface>` takes a surface *object*, not an
 * attribute), and Lit's dynamic tag names require the `static-html` escape hatch.
 * Mounting it directly keeps both facts inside this boundary instead of pushing
 * them into every caller's template.
 *
 * The node is recreated only when the surface *identity* changes, so an update
 * mutates the existing tree and does not destroy focus or scroll position.
 */
export function mountSurface(
  container: Element,
  runtime: SurfaceRuntime,
  surfaceId: string,
): SurfaceMount {
  let currentSurface: unknown;
  let node: HTMLElement | undefined;

  const sync = (): void => {
    const surface = runtime.surfaceModel(surfaceId);

    if (surface === currentSurface) {
      return;
    }
    currentSurface = surface;

    node?.remove();
    node = undefined;

    if (surface === undefined) {
      return;
    }
    // `SURFACE_TAG` is the tag the registry actually has a definition for. A
    // name with no definition yields an inert unknown element and a blank
    // surface, so the constant and the registry are kept in step by
    // `defineSurfaceElement`, and `SURFACE_TAG` is pinned to upstream's name.
    node = document.createElement(SURFACE_TAG);
    (node as unknown as { surface: unknown }).surface = surface;
    container.append(node);
  };

  sync();
  const unsubscribe = runtime.subscribe(sync);

  return {
    unmount(): void {
      unsubscribe();
      node?.remove();
      node = undefined;
    },
  };
}
