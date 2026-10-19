import type { FaultType } from '../contracts/enums.js';

export interface AppErrorInit {
  status: number;
  code: string;
  message: string;
  detail?: unknown;
  retryAfterSeconds?: number | null;
  faultType?: FaultType | null;
  scenarioId?: string | null;
}

/**
 * A failure the caller must be able to tell apart from a legitimate empty
 * answer. Every non-2xx the service produces goes through this type, so an
 * upstream fault can never be mistaken for "no specialist".
 */
export class AppError extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail: unknown;
  readonly retryAfterSeconds: number | null;
  readonly faultType: FaultType | null;
  readonly scenarioId: string | null;

  constructor(init: AppErrorInit) {
    super(init.message);
    this.name = 'AppError';
    this.status = init.status;
    this.code = init.code;
    this.detail = init.detail;
    this.retryAfterSeconds = init.retryAfterSeconds ?? null;
    this.faultType = init.faultType ?? null;
    this.scenarioId = init.scenarioId ?? null;
  }
}

export const notFound = (code: string, message: string, detail?: unknown): AppError =>
  new AppError({ status: 404, code, message, detail });

export const badRequest = (code: string, message: string, detail?: unknown): AppError =>
  new AppError({ status: 400, code, message, detail });

export const conflict = (code: string, message: string, detail?: unknown): AppError =>
  new AppError({ status: 409, code, message, detail });
