import { list, get } from '@vercel/blob';

const HTTP_OK = 200;
const HTTP_FORBIDDEN = 403;
const HTTP_NOT_CONFIGURED = 503;

// Export the list for the weekly send. Guarded by EXPORT_KEY so the GitHub
// Action can read it without holding the Blob token.
export default async function handler(req, res) {
  if (!process.env.BLOB_READ_WRITE_TOKEN || !process.env.EXPORT_KEY) {
    res.status(HTTP_NOT_CONFIGURED).json({ ok: false, reason: 'not_configured' });
    return;
  }
  const key = (req.query && req.query.key) || '';
  if (key !== process.env.EXPORT_KEY) {
    res.status(HTTP_FORBIDDEN).json({ ok: false, reason: 'bad_key' });
    return;
  }
  const emails = [];
  let cursor;
  do {
    const page = await list({ prefix: 'subs/', cursor });
    for (const blob of page.blobs) {
      const result = await get(blob.pathname, { access: 'private' });
      if (!result || !result.stream) {
        continue;
      }
      const text = await new Response(result.stream).text();
      emails.push(JSON.parse(text).email);
    }
    cursor = page.hasMore ? page.cursor : undefined;
  } while (cursor);
  res.status(HTTP_OK).json({ ok: true, emails });
}
