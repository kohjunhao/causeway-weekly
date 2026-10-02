import { put } from '@vercel/blob';
import { createHash } from 'node:crypto';

const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const HTTP_OK = 200;
const HTTP_BAD_REQUEST = 400;
const HTTP_METHOD_NOT_ALLOWED = 405;
const HTTP_NOT_CONFIGURED = 503;

// One blob per subscriber, keyed by a hash of the address, so a repeat signup
// overwrites itself and the list never holds duplicates.
export default async function handler(req, res) {
  if (req.method !== 'POST') {
    res.status(HTTP_METHOD_NOT_ALLOWED).json({ ok: false, reason: 'post_only' });
    return;
  }
  if (!process.env.BLOB_READ_WRITE_TOKEN) {
    res.status(HTTP_NOT_CONFIGURED).json({ ok: false, reason: 'not_configured' });
    return;
  }
  const body = typeof req.body === 'string' ? JSON.parse(req.body || '{}') : (req.body || {});
  const email = String(body.email || '').trim().toLowerCase();
  if (!EMAIL.test(email)) {
    res.status(HTTP_BAD_REQUEST).json({ ok: false, reason: 'bad_email' });
    return;
  }
  const key = createHash('sha256').update(email).digest('hex');
  await put(`subs/${key}.json`, JSON.stringify({ email, ts: new Date().toISOString() }), {
    access: 'private',
    addRandomSuffix: false,
    allowOverwrite: true,
    contentType: 'application/json',
  });
  res.status(HTTP_OK).json({ ok: true });
}
