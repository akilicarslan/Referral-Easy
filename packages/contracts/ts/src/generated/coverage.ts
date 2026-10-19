// GENERATED — do not edit. Source: packages/contracts/schemas/*.json
// Regenerate with: pnpm gen:contracts
import { z } from 'zod';

export const coverage$InsurancePlan = z.object({ "plan_id": z.string().uuid(), "payer_name": z.string().min(1), "plan_name": z.string().min(1), "plan_type": z.enum(["PPO","HMO","EPO","POS","HDHP","medicare_advantage"]), "network_name": z.union([z.string(), z.null()]).optional(), "requires_referral": z.boolean().optional(), "requires_prior_auth_for_specialist": z.boolean().optional(), "synthetic": z.literal(true) }).strict();
export type coverage$InsurancePlan = z.infer<typeof coverage$InsurancePlan>;

export const coverage$NetworkRecord = z.object({ "record_id": z.string().uuid(), "plan_id": z.string().uuid(), "provider_id": z.string().uuid(), "hospital_id": z.unknown().superRefine((x, ctx) => {
    const schemas = [z.string().uuid(), z.null()];
    const { errors, failed } = schemas.reduce<{
      errors: z.core.$ZodIssue[];
      failed: number;
    }>(
      ({ errors, failed }, schema) =>
        ((result) =>
          result.error
            ? {
                errors: [...errors, ...result.error.issues],
                failed: failed + 1,
              }
            : { errors, failed })(
          schema.safeParse(x),
        ),
      { errors: [], failed: 0 },
    );
    const passed = schemas.length - failed;
    if (passed !== 1) {
      ctx.addIssue(errors.length ? {
        path: [],
        code: "invalid_union",
        errors: [errors],
        message: "Invalid input: Should pass single schema. Passed " + passed,
      } : {
        path: [],
        code: "custom",
        errors: [errors],
        message: "Invalid input: Should pass single schema. Passed " + passed,
      });
    }
  }).optional(), "status": z.enum(["in_network","out_of_network","unknown"]).describe("Plan-level network status for this specific provider. Distinct from the provider's general `accepts_insurance` flag (§2.2)."), "effective_from": z.string().date(), "effective_to": z.union([z.string().date(), z.null()]).optional(), "evidence_id": z.string().uuid() }).strict();
export type coverage$NetworkRecord = z.infer<typeof coverage$NetworkRecord>;

export const coverage$CoverageResult = z.object({ "coverage_id": z.string().uuid(), "plan_id": z.string().uuid(), "provider_id": z.unknown().superRefine((x, ctx) => {
    const schemas = [z.string().uuid(), z.null()];
    const { errors, failed } = schemas.reduce<{
      errors: z.core.$ZodIssue[];
      failed: number;
    }>(
      ({ errors, failed }, schema) =>
        ((result) =>
          result.error
            ? {
                errors: [...errors, ...result.error.issues],
                failed: failed + 1,
              }
            : { errors, failed })(
          schema.safeParse(x),
        ),
      { errors: [], failed: 0 },
    );
    const passed = schemas.length - failed;
    if (passed !== 1) {
      ctx.addIssue(errors.length ? {
        path: [],
        code: "invalid_union",
        errors: [errors],
        message: "Invalid input: Should pass single schema. Passed " + passed,
      } : {
        path: [],
        code: "custom",
        errors: [errors],
        message: "Invalid input: Should pass single schema. Passed " + passed,
      });
    }
  }).optional(), "hospital_id": z.unknown().superRefine((x, ctx) => {
    const schemas = [z.string().uuid(), z.null()];
    const { errors, failed } = schemas.reduce<{
      errors: z.core.$ZodIssue[];
      failed: number;
    }>(
      ({ errors, failed }, schema) =>
        ((result) =>
          result.error
            ? {
                errors: [...errors, ...result.error.issues],
                failed: failed + 1,
              }
            : { errors, failed })(
          schema.safeParse(x),
        ),
      { errors: [], failed: 0 },
    );
    const passed = schemas.length - failed;
    if (passed !== 1) {
      ctx.addIssue(errors.length ? {
        path: [],
        code: "invalid_union",
        errors: [errors],
        message: "Invalid input: Should pass single schema. Passed " + passed,
      } : {
        path: [],
        code: "custom",
        errors: [errors],
        message: "Invalid input: Should pass single schema. Passed " + passed,
      });
    }
  }).optional(), "accepts_insurance": z.union([z.boolean().describe("Does the provider accept this payer at all? Null means unknown — the schema distinguishes 'no' from 'we do not know'."), z.null().describe("Does the provider accept this payer at all? Null means unknown — the schema distinguishes 'no' from 'we do not know'.")]).describe("Does the provider accept this payer at all? Null means unknown — the schema distinguishes 'no' from 'we do not know'."), "in_network_for_plan": z.union([z.boolean().describe("Is this provider in-network for THIS plan? A true `accepts_insurance` with a null or false `in_network_for_plan` is exactly the Network Conflict scenario (§20 row 3) and must be shown as unknown or out-of-network, never smoothed over."), z.null().describe("Is this provider in-network for THIS plan? A true `accepts_insurance` with a null or false `in_network_for_plan` is exactly the Network Conflict scenario (§20 row 3) and must be shown as unknown or out-of-network, never smoothed over.")]).describe("Is this provider in-network for THIS plan? A true `accepts_insurance` with a null or false `in_network_for_plan` is exactly the Network Conflict scenario (§20 row 3) and must be shown as unknown or out-of-network, never smoothed over."), "status": z.enum(["in_network","out_of_network","unknown"]).describe("'accepts this insurance' and 'in-network for this plan' are separate facts (§2.2). A contradiction or a gap resolves to unknown — never to a guess, and never to a payment guarantee."), "referral_required": z.union([z.boolean().describe("Null is unknown and stays unknown all the way to the UI."), z.null().describe("Null is unknown and stays unknown all the way to the UI.")]).describe("Null is unknown and stays unknown all the way to the UI."), "prior_auth_required": z.union([z.boolean(), z.null()]), "conflict": z.boolean().describe("True when sources disagree (e.g. a stale directory blurb claims in-network while the plan record says otherwise). The conflict is surfaced, not resolved by preference.").optional(), "conflicting_evidence_ids": z.array(z.string().uuid()).optional(), "winning_evidence_id": z.unknown().superRefine((x, ctx) => {
    const schemas = [z.string().uuid(), z.null()];
    const { errors, failed } = schemas.reduce<{
      errors: z.core.$ZodIssue[];
      failed: number;
    }>(
      ({ errors, failed }, schema) =>
        ((result) =>
          result.error
            ? {
                errors: [...errors, ...result.error.issues],
                failed: failed + 1,
              }
            : { errors, failed })(
          schema.safeParse(x),
        ),
      { errors: [], failed: 0 },
    );
    const passed = schemas.length - failed;
    if (passed !== 1) {
      ctx.addIssue(errors.length ? {
        path: [],
        code: "invalid_union",
        errors: [errors],
        message: "Invalid input: Should pass single schema. Passed " + passed,
      } : {
        path: [],
        code: "custom",
        errors: [errors],
        message: "Invalid input: Should pass single schema. Passed " + passed,
      });
    }
  }).describe("§10: the authoritative structured API result wins over retrieved or stale text. Null when there was no conflict.").optional(), "freshness": z.object({ "state": z.enum(["fresh","stale","unknown"]), "age_seconds": z.number().int().gte(0) }).strict().describe("Evidence freshness is shown to the patient alongside the unknown (§2.2, §10)."), "evidence_ids": z.array(z.string().uuid()), "disclaimer_key": z.literal("coverage_not_a_guarantee").describe("The Coverage agent may not promise payment or coverage (§3). This key binds the response to the approved non-guarantee wording; output guardrails reject a coverage/payment guarantee (§9).").optional(), "synthetic": z.literal(true) }).strict();
export type coverage$CoverageResult = z.infer<typeof coverage$CoverageResult>;

export const coverage$ReferralRequirement = z.object({ "requirement_id": z.string().uuid(), "hospital_id": z.string().uuid(), "plan_id": z.unknown().superRefine((x, ctx) => {
    const schemas = [z.string().uuid(), z.null()];
    const { errors, failed } = schemas.reduce<{
      errors: z.core.$ZodIssue[];
      failed: number;
    }>(
      ({ errors, failed }, schema) =>
        ((result) =>
          result.error
            ? {
                errors: [...errors, ...result.error.issues],
                failed: failed + 1,
              }
            : { errors, failed })(
          schema.safeParse(x),
        ),
      { errors: [], failed: 0 },
    );
    const passed = schemas.length - failed;
    if (passed !== 1) {
      ctx.addIssue(errors.length ? {
        path: [],
        code: "invalid_union",
        errors: [errors],
        message: "Invalid input: Should pass single schema. Passed " + passed,
      } : {
        path: [],
        code: "custom",
        errors: [errors],
        message: "Invalid input: Should pass single schema. Passed " + passed,
      });
    }
  }).describe("Null means the rule applies to every plan. §4: a hospital's document requirement is never presented as final before that hospital has been chosen.").optional(), "visit_type": z.enum(["first_visit","return_visit"]), "required_documents": z.array(z.enum(["referral_letter","prior_report","authorization_reference"]).describe("Documents required by some hospitals before booking (§2.3). Document text is DATA, never instructions (§9).")), "max_document_age_days": z.union([z.number().int().gte(0), z.null()]).optional(), "requires_prior_auth_reference": z.boolean().optional(), "evidence_id": z.string().uuid() }).strict();
export type coverage$ReferralRequirement = z.infer<typeof coverage$ReferralRequirement>;

export const coverage$CoverageEvidenceSnapshot = z.object({ "snapshot_id": z.string().uuid(), "captured_at": z.string().datetime({ offset: true }).describe("RFC 3339 with an explicit offset. Stored UTC; rendered in the hospital's IANA timezone (§17)."), "authoritative": z.boolean(), "payload_hash": z.string().regex(new RegExp("^[a-f0-9]{64}$")), "stale": z.boolean().optional() }).strict();
export type coverage$CoverageEvidenceSnapshot = z.infer<typeof coverage$CoverageEvidenceSnapshot>;

// Inline enums, also exported by name.
export const coverage$PlanType = z.enum(["PPO", "HMO", "EPO", "POS", "HDHP", "medicare_advantage"]);
export type coverage$PlanType = z.infer<typeof coverage$PlanType>;
