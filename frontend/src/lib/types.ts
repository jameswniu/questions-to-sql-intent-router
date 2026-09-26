// The shapes the API sends. The stream's events are the pipeline's, as app/web/stream.py encodes them, and every
// field is read defensively: the page shows what arrived and never assumes more.

export interface Person {
  user_id: string;
  name: string;
  title: string;
}

export interface Me extends Person {
  /** The database login every query this user causes runs on. */
  db_role: string;
  /** May open the operator dashboard. */
  ops: boolean;
}

export interface SessionView {
  me: Me;
  demo: boolean;
  /** The users the demo picker offers. Empty behind the sign-in proxy. */
  users: Person[];
}

export interface StageEvent {
  name: string;
  ms: number;
}

export interface EvidenceEvent {
  kind: string;
  payload: unknown;
}

export interface Claim {
  text?: string;
  citations?: string[];
}

/** A retrieved passage, as app/sources/documents.py's Hit. */
export interface Source {
  chunk_id?: string;
  doc_id?: string;
  anchor?: string;
  title?: string;
  header?: string | null;
  section?: string;
  body?: string;
  text?: string;
  snippet?: string;
  kind?: string;
  edition?: string | null;
  region?: string | null;
  score?: number;
  rerank_score?: number;
  similarity?: number;
}

export interface AnswerEvent {
  text?: string;
  claims_kept?: (Claim | string)[];
  /** How many claims the verifier cut. Their words never reach the browser. */
  claims_cut?: number | unknown[];
  could_not_confirm?: string[] | boolean;
  citations?: Source[];
}

export interface DoneEvent {
  request_id: string;
  total_ms?: number;
  route?: string | null;
  outcome?: string;
}
