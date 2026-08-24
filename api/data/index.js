/*
 * GET /api/data
 *
 * Streams the ICE cumulative export to the signed-in browser.
 *
 * The SAS URL lives in the ICE_SOURCE application setting on the Static Web
 * App, never in this repository and never in the page. The browser only ever
 * sees this endpoint, so the token cannot be read out of the delivered HTML.
 *
 * Access is enforced by staticwebapp.config.json (allowedRoles: authenticated).
 * The principal check below is defence in depth in case that config is edited.
 */
module.exports = async function (context, req) {
  const deny = (status, message) => {
    context.res = {
      status,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ error: message })
    };
  };

  // Static Web Apps injects this header for authenticated callers
  const principal = req.headers['x-ms-client-principal'];
  if (!principal) {
    context.log.warn('Unauthenticated request to /api/data');
    return deny(401, 'Sign-in required.');
  }

  let user = 'unknown';
  try {
    const p = JSON.parse(Buffer.from(principal, 'base64').toString('utf8'));
    user = p.userDetails || p.userId || 'unknown';
    if (!(p.userRoles || []).includes('authenticated')) {
      return deny(403, 'Not authorised.');
    }
  } catch (e) {
    return deny(401, 'Could not read sign-in details.');
  }

  const src = process.env.ICE_SOURCE;
  if (!src) {
    context.log.error('ICE_SOURCE application setting is not configured');
    return deny(500, 'Data source is not configured. Set the ICE_SOURCE application setting.');
  }

  try {
    const started = Date.now();
    const upstream = await fetch(src, { headers: { 'User-Agent': 'ice-linelist-swa/1.0' } });

    if (!upstream.ok) {
      context.log.error(`Blob fetch failed: ${upstream.status} ${upstream.statusText}`);
      // Deliberately vague to the client: never echo the upstream URL or token
      return deny(502, upstream.status === 403
        ? 'The data source rejected the request. The SAS token may have expired.'
        : `Data source returned ${upstream.status}.`);
    }

    const body = await upstream.text();
    context.log(`served ${body.length} bytes to ${user} in ${Date.now() - started}ms`);

    context.res = {
      status: 200,
      headers: {
        // JSON Lines, parsed by the page
        'Content-Type': 'application/x-ndjson; charset=utf-8',
        'Cache-Control': 'private, max-age=300',
        'X-Content-Type-Options': 'nosniff'
      },
      body
    };
  } catch (err) {
    context.log.error('Upstream error: ' + err.message);
    return deny(502, 'Could not reach the data source.');
  }
};
