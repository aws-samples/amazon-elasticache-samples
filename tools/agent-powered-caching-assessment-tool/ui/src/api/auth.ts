// IDENTITY SEAM. The API has no authentication today: it answers loopback callers
// only and the UI is served from the same origin. When an identity provider is chosen
// (OIDC, SAML or SigV4, per the product's deferred-auth decision), implement it here and
// nowhere else: every API request goes through `authorize`, and nothing else in the UI
// knows how a caller proves who it is. Do not add a login screen until that decision.

export interface Identity {
  /** Display name for the top bar. Null while there is no identity provider. */
  displayName: string | null;
}

export function currentIdentity(): Identity {
  return { displayName: null };
}

/** Add credentials to an outgoing API request. A no-op until authentication exists. */
export async function authorize(request: Request): Promise<Request> {
  return request;
}
