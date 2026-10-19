/**
 * The eight components from §12, as catalog entries.
 *
 * This module deliberately does NOT import `@a2ui/*`. It declares the mapping
 * between a component name on the wire and the custom element that renders it,
 * using only structural types. `src/a2ui/adapter.ts` is the single place where
 * that mapping is handed to A2UI.
 *
 * The list is exactly eight. Coverage, preference and readiness information are
 * section and field variants of these components — the moment a ninth name
 * appears here, this project has started growing a UI system per feature, which
 * is the thing it set out not to do.
 */

import { HospitalPicker } from '../components/hospital-picker.js';
import { DoctorPicker } from '../components/doctor-picker.js';
import { SlotPicker } from '../components/slot-picker.js';
import { PatientDetailsForm } from '../components/patient-details-form.js';
import { BookingReview } from '../components/booking-review.js';
import { AppointmentReceipt } from '../components/appointment-receipt.js';
import { DirectoryFallbackCard } from '../components/directory-fallback-card.js';
import { HumanHandoffCard } from '../components/human-handoff-card.js';

/** Structural stand-in for A2UI's `WebComponentImplementation`. */
export interface ComponentDef {
  /** The `component` string as it appears on the wire and in the contract enum. */
  readonly componentName: string;
  /** The custom element tag this component renders into. */
  readonly elementTag: string;
  /** The element class, so A2UI can register it on demand. */
  readonly element: CustomElementConstructor;
}

/** Order is fixed by §4: hospitals, then doctors, then slots. */
export const COMPONENT_DEFS: readonly ComponentDef[] = [
  {
    componentName: HospitalPicker.componentName,
    elementTag: HospitalPicker.elementTag,
    element: HospitalPicker,
  },
  {
    componentName: DoctorPicker.componentName,
    elementTag: DoctorPicker.elementTag,
    element: DoctorPicker,
  },
  {
    componentName: SlotPicker.componentName,
    elementTag: SlotPicker.elementTag,
    element: SlotPicker,
  },
  {
    componentName: PatientDetailsForm.componentName,
    elementTag: PatientDetailsForm.elementTag,
    element: PatientDetailsForm,
  },
  {
    componentName: BookingReview.componentName,
    elementTag: BookingReview.elementTag,
    element: BookingReview,
  },
  {
    componentName: AppointmentReceipt.componentName,
    elementTag: AppointmentReceipt.elementTag,
    element: AppointmentReceipt,
  },
  {
    componentName: DirectoryFallbackCard.componentName,
    elementTag: DirectoryFallbackCard.elementTag,
    element: DirectoryFallbackCard,
  },
  {
    componentName: HumanHandoffCard.componentName,
    elementTag: HumanHandoffCard.elementTag,
    element: HumanHandoffCard,
  },
];

/** Lookup by wire name. */
export function componentDef(name: string): ComponentDef | undefined {
  return COMPONENT_DEFS.find((def) => def.componentName === name);
}
