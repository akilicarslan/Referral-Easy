import type { Store } from '../db/store.js';
import type { HospitalAdapterType } from '../contracts/enums.js';
import { InstantConfirmAdapter } from './instant-confirm.js';
import { ReviewRequiredAdapter } from './review-required.js';
import type { BookingAdapter } from './types.js';

/**
 * The only place that knows which hospital speaks which dialect. Everything
 * above this line sees `BookingAdapter`, so no caller can branch on the vendor.
 */
export class AdapterRegistry {
  private readonly instantConfirm: InstantConfirmAdapter;
  private readonly reviewRequired: ReviewRequiredAdapter;

  constructor(store: Store) {
    this.instantConfirm = new InstantConfirmAdapter(store);
    this.reviewRequired = new ReviewRequiredAdapter(store);
  }

  for(adapterType: HospitalAdapterType): BookingAdapter {
    return adapterType === 'instant_confirm' ? this.instantConfirm : this.reviewRequired;
  }
}
