// ═══════════════════════════════════════════════════════════════════════════
// TokenBar — asks for the API token, and says why.
//
// It appears when the control plane requires a token and this tab does not have
// one, and it does NOT block reading: the dashboards stay usable while
// unauthenticated, because reads are open by default and a monitor that demands
// a credential to show a graph would just be turned off.
//
// The three states are kept distinct because they have different fixes:
//   no token in this tab        -> type one
//   token rejected (401)        -> it is wrong, type another
//   server has none configured  -> nobody can write until an operator sets one,
//                                  and typing a token here will not help
// Collapsing those into "unauthorised" sends people looking in the wrong place.
// ═══════════════════════════════════════════════════════════════════════════
import React, { useCallback, useEffect, useState } from 'react';
import {
  authError, fetchAuthStatus, getToken, onTokenChange, setToken,
} from '../authClient';
import './TokenBar.css';
import API_URL from '../apiUrl';


function TokenBar() {
  const [status, setStatus] = useState(null);
  const [token, setLocalToken] = useState(getToken());
  const [entry, setEntry] = useState('');
  const [err, setErr] = useState(null);
  const [open, setOpen] = useState(false);

  useEffect(() => onTokenChange(setLocalToken), []);

  useEffect(() => {
    let alive = true;
    fetchAuthStatus(API_URL).then((s) => { if (alive) setStatus(s); });
    return () => { alive = false; };
  }, []);

  // Poll the interceptor's last auth error so a rejected token surfaces here
  // rather than as a failure on whichever panel happened to call first.
  useEffect(() => {
    const t = setInterval(() => setErr(authError()), 2000);
    return () => clearInterval(t);
  }, []);

  const save = useCallback((e) => {
    e.preventDefault();
    setToken(entry.trim());
    setEntry('');
    setOpen(false);
  }, [entry]);

  if (!status) return null;
  // Token switched off on the server (the shipped default for now): show
  // nothing. Access is decided by the control plane's AccessMode: an SSM port
  // forward (IAM), the customer's own load balancer with sign-in, or a CIDR.
  if (status.enabled === false) return null;

  const serverHasNone = status.configured === false;
  const rejected = err && err.kind === 'token';
  const needsToken = !token || rejected;

  if (!needsToken && !serverHasNone) {
    return (
      <div className="tb tb-ok">
        <span className="tb-dot" />
        <span>authenticated for this tab</span>
        <button type="button" onClick={() => setToken('')}>Forget token</button>
      </div>
    );
  }

  return (
    <div className={`tb ${serverHasNone ? 'tb-server' : 'tb-need'}`}>
      {serverHasNone ? (
        <>
          <strong>This control plane has no API token configured.</strong>
          <span>
            Reading works, but anything that changes state is refused. An
            operator needs to set ECDL_API_TOKEN (or
            ECDL_API_TOKEN_SECRET_ID) on the server — a token typed here cannot
            fix it.
          </span>
        </>
      ) : (
        <>
          <strong>
            {rejected ? 'That token was rejected.' : 'A token is required to change anything.'}
          </strong>
          <span>
            Reading is open; deploying, repairing and running loads need the
            control plane&apos;s API token.
          </span>
          {open ? (
            <form onSubmit={save}>
              <input
                type="password"
                autoComplete="off"
                placeholder="API token"
                value={entry}
                onChange={(e) => setEntry(e.target.value)}
              />
              <button type="submit">Use token</button>
              <button type="button" onClick={() => setOpen(false)}>Cancel</button>
            </form>
          ) : (
            <button type="button" onClick={() => setOpen(true)}>Enter token</button>
          )}
          <span className="tb-note">
            Held in this tab only and forgotten when it closes. This connection is
            plain HTTP, so anything on the network path can read it.
          </span>
        </>
      )}
    </div>
  );
}

export default TokenBar;
